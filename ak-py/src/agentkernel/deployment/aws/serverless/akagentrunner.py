import json
import logging
from typing import Optional

from ....core.chat_service import ChatService
from ....core.config import AKConfig
from ....core.model import BaseRunRequest, ExecutionMode, ScheduledRunMetadata, StreamChunk
from ..core.sqs_handler import SQSHandler
from .core import LambdaSQSConsumer


class ServerlessAgentRunner(LambdaSQSConsumer):
    """
    handle() dispatches to ServerlessStreamAgentRunner when execution.mode is STREAM.
    """

    _log = logging.getLogger("ak.aws.agentrunner")
    _chat_service = None

    @classmethod
    def _get_max_receive_count(cls) -> int:
        return AKConfig.get().execution.queues.input.max_receive_count

    @classmethod
    def handle(cls, event: dict, context) -> dict:
        """Dispatch to ServerlessStreamAgentRunner when execution.mode is STREAM."""
        if AKConfig.get().execution.mode == ExecutionMode.STREAM:
            return ServerlessStreamAgentRunner.handle(event, context)
        return super().handle(event, context)

    @classmethod
    def _get_chat_service(cls) -> ChatService:
        if cls._chat_service is None:
            cls._chat_service = ChatService()
        return cls._chat_service

    @classmethod
    def _get_record_attributes(cls, raw_queue_message: dict) -> dict:
        """Extract message_group_id, request_id, user_id, and (in ASYNC/STREAM) endpoint_url.

        :raises ValueError: request_id is missing.
        """
        attributes = SQSHandler.get_message_system_attributes(raw_queue_message)
        message_attributes = SQSHandler.get_message_custom_attributes(raw_queue_message)
        request_id = message_attributes.get("request_id")
        user_id = message_attributes.get("user_id")
        endpoint_url = (
            message_attributes.get("endpoint_url") if AKConfig.get().execution.mode in (ExecutionMode.ASYNC, ExecutionMode.STREAM) else None
        )

        if not request_id:
            raise ValueError("request_id is required")

        record_attributes = {
            "message_group_id": attributes["MessageGroupId"],
            "message_deduplication_id": attributes.get("MessageDeduplicationId"),
            "request_id": request_id,
            "user_id": user_id,
        }

        if endpoint_url:
            record_attributes["endpoint_url"] = endpoint_url

        cls._log.info(f"Extracted record attributes: {record_attributes}")
        return record_attributes

    @classmethod
    def _construct_error_message_body(cls, error_msg: str) -> dict:
        return {"error": error_msg}

    @classmethod
    def _send_to_output_queue(cls, message_body: dict, record_attributes: dict) -> None:
        """Send a prepared message to the configured response SQS queue."""
        cls._log.info("Sending message to output queue")
        cls._log.debug(f"Message body: {message_body}")
        cls._log.debug(f"Record attributes: {record_attributes}")

        custom_attributes = []
        if record_attributes.get("endpoint_url"):
            custom_attributes.append(
                SQSHandler.CustomAttribute(name="endpoint_url", value=record_attributes["endpoint_url"], datatype=SQSHandler.AttributeDataType.STRING)
            )

        cls._log.debug(f"Custom attributes: {custom_attributes}")

        SQSHandler.send_message_to_output_queue(
            message_body=message_body,
            attributes={
                "message_group_id": record_attributes["message_group_id"],
                "message_deduplication_id": record_attributes["message_deduplication_id"],
            },
            request_id=record_attributes["request_id"],
            user_id=record_attributes["user_id"],
            custom_message_attributes=custom_attributes,
        )

    @classmethod
    def _parse_body(cls, record: dict) -> BaseRunRequest:
        return BaseRunRequest.model_validate(json.loads(record["body"]))

    @classmethod
    def _parse_session_id(cls, record: dict) -> Optional[str]:
        """Read the session id straight off a record's body, tolerating an unparseable one.

        Used only on the permanent-failure path, which has no error channel left.
        """
        try:
            body = json.loads(record.get("body") or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
        return body.get("session_id") if isinstance(body, dict) else None

    @classmethod
    def process_message(cls, record: dict) -> None:
        """Invoke the chat service for a single SQS record and send the response to the output queue."""
        cls._log.info(f"Processing message: {record}")
        body = cls._parse_body(record)
        _, agent_response = cls._get_chat_service().process_chat_request(req=body)
        cls._log.info(f"Chat service response: '{agent_response}'")
        record_attributes = cls._get_record_attributes(raw_queue_message=record)
        cls._send_to_output_queue(message_body=agent_response, record_attributes=record_attributes)
        cls._log.info(f"Sent Response message to Output Queue: '{SQSHandler.get_output_queue_url()}'")

    @classmethod
    def on_permanent_failure(cls, record: dict) -> None:
        """Send an error response to the output queue for a record that exhausted its retries."""
        cls._log.info(f"Permanent failure: {record}: Retried message {cls._get_max_receive_count()} times. Sending error message to Output Queue`")
        try:
            record_attributes = cls._get_record_attributes(raw_queue_message=record)
            error_message_body = cls._construct_error_message_body(
                error_msg=f"Failed to process message. Retried {cls._get_max_receive_count()} times"
            )
            # Echoing the block lets the output consumer record a retry-exhausted run as
            # FAILED, with no DLQ involved. from_raw_body never raises.
            scheduled_run = ScheduledRunMetadata.from_raw_body(record.get("body"))
            if scheduled_run is None:
                error_message_body["session_id"] = record_attributes["message_group_id"]
            else:
                error_message_body["scheduled_run"] = scheduled_run.model_dump(mode="json")
                # A fire's group id is the scheduled_task_id, not a session id, so the session
                # id has to come from the message body.
                session_id = cls._parse_session_id(record)
                if session_id:
                    error_message_body["session_id"] = session_id
            cls._send_to_output_queue(message_body=error_message_body, record_attributes=record_attributes)
            cls._log.info(f"Sent Permanent Failure message to Output Queue: '{SQSHandler.get_output_queue_url()}'")
        except Exception as e:
            # Swallowed so this already-exhausted message isn't returned as a batchItemFailure for another retry.
            cls._log.info(f"Failed sending permanent failure message to Output Queue '{SQSHandler.get_output_queue_url()}' due to error: '{str(e)}'")


class ServerlessStreamAgentRunner(LambdaSQSConsumer):
    """Lambda SQS consumer that processes chat requests in STREAM mode.

    Each chunk is sent as a separate output-queue message; ResponseHandler broadcasts each via
    WebSocket. Scheduled fires are the exception — see ``_is_scheduled_fire``.
    """

    _log = logging.getLogger("ak.aws.streamagentrunner")
    _chat_service = None

    @classmethod
    def _get_max_receive_count(cls) -> int:
        return AKConfig.get().execution.queues.input.max_receive_count

    @classmethod
    def _is_scheduled_fire(cls, record: dict) -> bool:
        """Tell a timer-delivered fire from an interactive streaming request.

        A fire has no endpoint_url to stream to and a StreamChunk has nowhere to carry the
        scheduled_run block the response handler needs, so fires run as ordinary non-stream
        executions instead. Same block the response handler itself fans out on.

        :return: True when this record is one fire of a scheduled task.
        """
        return ScheduledRunMetadata.from_raw_body(record.get("body")) is not None

    @classmethod
    def _get_chat_service(cls) -> ChatService:
        if cls._chat_service is None:
            cls._chat_service = ChatService()
        return cls._chat_service

    @classmethod
    def _get_record_attributes(cls, raw_queue_message: dict) -> dict:
        """Extract message_group_id, request_id, user_id, and endpoint_url.

        :raises ValueError: request_id or endpoint_url is missing.
        """
        attributes = SQSHandler.get_message_system_attributes(raw_queue_message)
        message_attributes = SQSHandler.get_message_custom_attributes(raw_queue_message)
        request_id = message_attributes.get("request_id")
        user_id = message_attributes.get("user_id")
        endpoint_url = message_attributes.get("endpoint_url")

        if not request_id:
            raise ValueError("request_id is required")
        if not endpoint_url:
            raise ValueError("endpoint_url is required for STREAM mode")

        record_attributes = {
            "message_group_id": attributes["MessageGroupId"],
            "message_deduplication_id": attributes.get("MessageDeduplicationId"),
            "request_id": request_id,
            "user_id": user_id,
            "endpoint_url": endpoint_url,
        }

        cls._log.info(f"Extracted record attributes: {record_attributes}")
        return record_attributes

    @classmethod
    def _parse_body(cls, record: dict) -> BaseRunRequest:
        return BaseRunRequest.model_validate(json.loads(record["body"]))

    @classmethod
    def _send_chunk_to_output_queue(cls, chunk_body: dict, record_attributes: dict, chunk_dedup_suffix: str) -> None:
        """Send a single stream chunk to the output SQS queue."""
        cls._log.debug(f"Sending stream chunk to output queue: {chunk_body}")

        dedup_id = record_attributes.get("message_deduplication_id")
        chunk_dedup_id = f"{dedup_id}-{chunk_dedup_suffix}" if dedup_id else None

        custom_attributes = [
            SQSHandler.CustomAttribute(name="endpoint_url", value=record_attributes["endpoint_url"], datatype=SQSHandler.AttributeDataType.STRING)
        ]

        SQSHandler.send_message_to_output_queue(
            message_body=chunk_body,
            attributes={
                "message_group_id": record_attributes["message_group_id"],
                "message_deduplication_id": chunk_dedup_id,
            },
            request_id=record_attributes["request_id"],
            user_id=record_attributes["user_id"],
            custom_message_attributes=custom_attributes,
        )

    @classmethod
    def process_message(cls, record: dict) -> None:
        """Invoke the chat service and stream each yielded chunk to the output queue as a separate message."""
        if cls._is_scheduled_fire(record):
            cls._log.info("Scheduled fire — running as a non-stream execution")
            # A sibling, not a base class — named explicitly for its endpoint_url-optional path.
            return ServerlessAgentRunner.process_message(record)

        cls._log.info(f"Processing stream message: {record}")
        body = cls._parse_body(record)
        record_attributes = cls._get_record_attributes(raw_queue_message=record)
        # Included in the dedup suffix below so a retry's chunks never collide with a prior attempt's.
        receive_count = record.get("attributes", {}).get("ApproximateReceiveCount", "1")

        chunk_count = 0
        for raw_chunk in cls._get_chat_service().process_stream_chat_sync(req=body):
            chunk_dict = json.loads(raw_chunk)
            cls._send_chunk_to_output_queue(
                chunk_body=chunk_dict,
                record_attributes=record_attributes,
                chunk_dedup_suffix=f"{receive_count}-{chunk_count}",
            )
            chunk_count += 1
        cls._log.info(f"Streamed {chunk_count} chunks to output queue for request_id: {record_attributes['request_id']}")

    @classmethod
    def on_permanent_failure(cls, record: dict) -> None:
        """Send an error chunk to the output queue for a record that exhausted its retries."""
        if cls._is_scheduled_fire(record):
            # Delegate so the outcome echoes scheduled_run — otherwise last_run_* stays stale.
            return ServerlessAgentRunner.on_permanent_failure(record)

        cls._log.info(f"Permanent failure: {record}: Retried message {cls._get_max_receive_count()} times. Sending error chunk to Output Queue")
        try:
            record_attributes = cls._get_record_attributes(raw_queue_message=record)
            receive_count = record.get("attributes", {}).get("ApproximateReceiveCount", "1")
            error_chunk = StreamChunk(
                error=f"Failed to process message. Retried {cls._get_max_receive_count()} times",
                done=True,
            )
            error_chunk_body = error_chunk.model_dump(exclude_none=True)
            error_chunk_body["session_id"] = record_attributes["message_group_id"]
            cls._send_chunk_to_output_queue(
                chunk_body=error_chunk_body,
                record_attributes=record_attributes,
                chunk_dedup_suffix=f"{receive_count}-error",
            )
            cls._log.info(f"Sent Permanent Failure chunk to Output Queue: '{SQSHandler.get_output_queue_url()}'")
        except Exception as e:
            # Swallowed so this already-exhausted message isn't returned as a batchItemFailure for another retry.
            cls._log.info(f"Failed sending permanent failure chunk to Output Queue '{SQSHandler.get_output_queue_url()}' due to error: '{str(e)}'")
