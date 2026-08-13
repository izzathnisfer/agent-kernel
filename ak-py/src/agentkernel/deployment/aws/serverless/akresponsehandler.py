import json
import logging
from typing import Any, Dict, Optional

from ....core.config import AKConfig
from ....core.model import ExecutionMode, StreamChunk
from ....scheduler import SchedulerFactory
from ...common.scheduled_run_recorder import ScheduledRunRecorder
from ..core.response_store import ResponseDBHandler
from ..core.sqs_handler import SQSHandler
from .core import LambdaSQSConsumer
from .core.router.ws_lambda import LambdaWSHandler


class ResponseHandler(LambdaSQSConsumer):
    """
    Lambda SQS consumer that processes response messages and stores them in the configured response store.
    """

    _log = logging.getLogger("ak.aws.responsehandler")
    _response_store = None
    _base_ws_handler = None

    @classmethod
    def handle(cls, event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        """Validate the scheduler wiring, then process the batch.

        Checked here, not in the class body, since ``agentkernel.aws`` re-exports every
        deployment class — validating at import time would assert on wiring an unrelated
        entry point never uses.

        :return: A dict with "batchItemFailures" per AWS format.
        :raises AKConfigError: Scheduling is enabled but the deployment wiring is missing.
        """
        SchedulerFactory.validate_config()
        return super().handle(event, context)

    @classmethod
    def _get_max_receive_count(cls) -> int:
        return AKConfig.get().execution.queues.output.max_receive_count

    @classmethod
    def _get_response_store(cls):
        if cls._response_store is None:
            cls._response_store = ResponseDBHandler().get_store()
        return cls._response_store

    @classmethod
    def _get_base_ws_handler(cls):
        if cls._base_ws_handler is None:
            cls._base_ws_handler = LambdaWSHandler()
        return cls._base_ws_handler

    @classmethod
    def _construct_message_for_store(cls, record: Dict[str, Any], body: Optional[Any] = None) -> Dict[str, Any]:
        """Build the message stored in the response store, using ``body`` in place of record["body"] when given.

        :raises ValueError: request_id is missing in SQS message attributes.
        """
        message_body = body if body is not None else record.get("body")
        if isinstance(message_body, str):
            message_body = json.loads(message_body)
        session_id = message_body.get("session_id")

        message_attributes = SQSHandler.get_message_custom_attributes(record)
        request_id = message_attributes.get("request_id")
        if not request_id:
            raise ValueError("request_id is required in SQS message attributes")
        message = {"session_id": session_id, "request_id": request_id, "body": message_body}
        return message

    @classmethod
    def _broadcast_via_websocket(cls, record: Dict[str, Any], message_type: Optional[LambdaWSHandler.MessageType] = None) -> None:
        """Broadcast a message via WebSocket, wrapped in ``message_type``'s envelope when given.

        :raises ValueError: endpoint_url or user_id is missing in message attributes.
        """
        message_attributes = SQSHandler.get_message_custom_attributes(record)
        endpoint_url = message_attributes.get("endpoint_url")
        user_id = message_attributes.get("user_id")

        if not endpoint_url:
            raise ValueError("endpoint_url is required in SQS message attributes")
        if not user_id:
            raise ValueError("user_id is required in SQS message attributes")

        message_body = record.get("body")
        if isinstance(message_body, str):
            message_body = json.loads(message_body)
        if not isinstance(message_body, dict):
            raise ValueError("SQS record body must be a JSON object")

        base_ws = cls._get_base_ws_handler()
        cls._log.info(f"Broadcasting message via WebSocket for user_id: {user_id}, endpoint_url: {endpoint_url}")
        base_ws.broadcast(endpoint_url=endpoint_url, message=message_body, user_id=user_id, message_type=message_type)
        cls._log.info(f"Successfully broadcasted message for user_id: {user_id}")

    @classmethod
    def process_message(cls, record: Dict[str, Any]) -> None:
        """Process one SQS record: broadcast via WebSocket in ASYNC/STREAM mode, else store it."""
        cls._log.info(f"Processing message: {record}")

        # Checked first: a fire carries no endpoint_url, so the WebSocket branches would raise,
        # and no REST caller is polling for it.
        if ScheduledRunRecorder.record(record.get("body")):
            cls._log.info("Recorded scheduled run outcome; not broadcast and not stored")
            return

        if AKConfig.get().execution.mode == ExecutionMode.ASYNC:
            cls._broadcast_via_websocket(record, message_type=LambdaWSHandler.MessageType.CHAT_RESPONSE)
        elif AKConfig.get().execution.mode == ExecutionMode.STREAM:
            cls._broadcast_via_websocket(record, message_type=LambdaWSHandler.MessageType.STREAM_CHUNK)
        else:
            message = cls._construct_message_for_store(record)
            cls._get_response_store().add_message(message)
            cls._log.info(f"Stored message for session_id: {message['session_id']}, request_id: {message['request_id']}")

    @classmethod
    def on_permanent_failure(cls, record: Dict[str, Any]) -> None:
        """Handle a record that exhausted its retries: broadcast the error via WebSocket in ASYNC/STREAM mode, else store it."""
        cls._log.error(f"Permanent failure: {record}: Retried message {cls._get_max_receive_count()} times")

        # Same reasons as process_message, plus: a fire's group id is the scheduled_task_id, so
        # an error entry here would be filed under a missing session.
        if ScheduledRunRecorder.record_before_discard(record.get("body")):
            cls._log.info("Scheduled run: not broadcast and not stored")
            return

        try:
            message_attributes = SQSHandler.get_message_custom_attributes(record)
            # The session id travels as the FIFO group id, not as a custom attribute.
            session_id = SQSHandler.get_message_system_attributes(record).get("MessageGroupId")
            error_message = {
                "error": f"Failed to process message after {cls._get_max_receive_count()} retries",
                "request_id": message_attributes.get("request_id"),
            }
            if session_id:
                error_message["session_id"] = session_id

            if AKConfig.get().execution.mode == ExecutionMode.ASYNC:
                endpoint_url = message_attributes.get("endpoint_url")
                user_id = message_attributes.get("user_id")

                if endpoint_url and user_id:
                    base_ws = cls._get_base_ws_handler()
                    cls._log.info(f"Broadcasting permanent failure error via WebSocket for user_id: {user_id}")
                    base_ws.broadcast(
                        endpoint_url=endpoint_url,
                        message=error_message,
                        user_id=user_id,
                        message_type=LambdaWSHandler.MessageType.SYSTEM_RESPONSE,
                    )
                    cls._log.info(f"Successfully broadcasted permanent failure error for user_id: {user_id}")
                else:
                    cls._log.warning("Cannot broadcast permanent failure error: endpoint_url or user_id missing in message attributes")
            elif AKConfig.get().execution.mode == ExecutionMode.STREAM:
                endpoint_url = message_attributes.get("endpoint_url")
                user_id = message_attributes.get("user_id")

                if endpoint_url and user_id:
                    error_chunk = StreamChunk(
                        error=f"Failed to process message after {cls._get_max_receive_count()} retries",
                        done=True,
                    )
                    error_chunk_body = error_chunk.model_dump(exclude_none=True)
                    if session_id:
                        error_chunk_body["session_id"] = session_id
                    base_ws = cls._get_base_ws_handler()
                    cls._log.info(f"Broadcasting permanent failure stream chunk via WebSocket for user_id: {user_id}")
                    base_ws.broadcast(
                        endpoint_url=endpoint_url,
                        message=error_chunk_body,
                        user_id=user_id,
                        message_type=LambdaWSHandler.MessageType.STREAM_CHUNK,
                    )
                    cls._log.info(f"Successfully broadcasted permanent failure stream chunk for user_id: {user_id}")
                else:
                    cls._log.warning("Cannot broadcast permanent failure stream chunk: endpoint_url or user_id missing in message attributes")
            else:
                message = cls._construct_message_for_store(record, body=error_message)
                cls._get_response_store().add_message(message)
                cls._log.info(f"Stored permanent failure message for session_id: {message['session_id']}, request_id: {message['request_id']}")
        except Exception as e:
            # Swallowed so this already-exhausted message isn't returned as a batchItemFailure for another retry.
            cls._log.error(f"Failed to handle permanent failure message due to error: {str(e)}")
