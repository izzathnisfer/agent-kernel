from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from agentkernel.core.config import AKConfig

from .core.queue_consumer import LocalQueueConsumer
from .core.queue_handler import LocalQueueHandler
from .core.response_store import LocalResponseStore


class LocalOutputConsumer(LocalQueueConsumer):
    """
    Local Output Consumer — polls the output queue and writes results into LocalResponseStore.

    The local-mode equivalent of ECSOutputConsumer, minus the WebSocket branch — ASYNC/STREAM
    execution modes are out of scope for local queue mode v1, so every message is written to
    the response store for RestHandler.poll_response/enqueue_and_wait to pick up.
    """

    _log = logging.getLogger("ak.local.outputconsumer")
    _config = AKConfig.get()
    max_receive_count = _config.execution.queues.output.max_receive_count
    num_consumers = _config.execution.queues.output.no_of_consumers

    _response_store: Optional[LocalResponseStore] = None

    @classmethod
    def get_queue_name(cls) -> str:
        return "output"

    @classmethod
    def _get_response_store(cls) -> LocalResponseStore:
        if cls._response_store is None:
            cls._response_store = LocalResponseStore(cls._config.execution.queues.output.url)
        return cls._response_store

    @classmethod
    def _construct_message_for_store(cls, record: Dict[str, Any], body: Any = None) -> Dict[str, Any]:
        """
        Build the dict to write into the Response Store.

        :param record: Raw LocalQueue.receive() record
        :param body: Override body (string or dict). Defaults to record["Body"].
        :raises ValueError: If request_id is missing from message attributes.
        """
        message_body = body if body is not None else record.get("Body", "{}")
        if isinstance(message_body, str):
            message_body = json.loads(message_body)

        message_attributes = LocalQueueHandler.get_message_custom_attributes(record)
        request_id = message_attributes.get("request_id")
        if not request_id:
            raise ValueError("request_id is required in message attributes")

        return {
            "session_id": message_body.get("session_id"),
            "request_id": request_id,
            "body": message_body,
        }

    @classmethod
    def process_message(cls, record: Dict[str, Any]) -> None:
        """Implements LocalQueueConsumer.process_message: write to the Response Store."""
        message_id = record.get("MessageId")
        cls._log.info(f"[OUTPUT START] Processing output message {message_id}")

        message = cls._construct_message_for_store(record)
        cls._get_response_store().add_message(message)

        cls._log.info(f"[OUTPUT DONE] Stored response — session_id={message['session_id']} request_id={message['request_id']}")

    @classmethod
    def on_permanent_failure(cls, record: Dict[str, Any]) -> None:
        """
        Implements LocalQueueConsumer.on_permanent_failure: write an error entry to the
        Response Store so the waiting HTTP caller gets a response instead of hanging.
        """
        max_retries = cls._config.execution.queues.output.max_receive_count
        cls._log.error(f"Permanent failure for output message {record.get('MessageId')} after {max_retries} retries")

        try:
            message_attributes = LocalQueueHandler.get_message_custom_attributes(record)
            request_id = message_attributes.get("request_id")
            session_id = LocalQueueHandler.get_message_system_attributes(record).get("MessageGroupId")
            error_payload = {"error": f"Failed to process message after {max_retries} retries", "request_id": request_id}
            if session_id:
                error_payload["session_id"] = session_id

            message = cls._construct_message_for_store(record, body=json.dumps(error_payload))
            cls._get_response_store().add_message(message)
            cls._log.info(f"Stored permanent-failure error — session_id={message['session_id']} request_id={message['request_id']}")
        except Exception:
            cls._log.exception("Failed to handle permanent-failure output message")
