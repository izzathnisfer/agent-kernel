from __future__ import annotations

import json
import logging

from agentkernel.core.chat_service import ChatService
from agentkernel.core.config import AKConfig
from agentkernel.core.model import BaseRunRequest

from .core.queue_consumer import LocalQueueConsumer
from .core.queue_handler import LocalQueueHandler


class LocalAgentRunner(LocalQueueConsumer):
    """
    Local Agent Runner — polls the input queue, runs the agent, and puts the result on the
    output queue.

    The local-mode equivalent of ECSAgentRunner, minus the STREAM/ASYNC (WebSocket) branches —
    those execution modes are out of scope for local queue mode v1.

    Usage::

        if __name__ == "__main__":
            LocalAgentRunner.run()
    """

    _log = logging.getLogger("ak.local.agentrunner")
    _chat_service: ChatService | None = None
    _config = AKConfig.get()
    max_receive_count = _config.execution.queues.input.max_receive_count
    num_consumers = _config.execution.queues.input.no_of_consumers

    @classmethod
    def get_queue_name(cls) -> str:
        return "input"

    @classmethod
    def _get_chat_service(cls) -> ChatService:
        if cls._chat_service is None:
            cls._chat_service = ChatService()
        return cls._chat_service

    @classmethod
    def _get_record_attributes(cls, record: dict) -> dict:
        """
        Extract routing attributes from a raw local queue message.

        :param record: Raw LocalQueue.receive() record
        :return: Extracted attributes dict
        :raises ValueError: If request_id is missing
        """
        attributes = LocalQueueHandler.get_message_system_attributes(record)
        message_attributes = LocalQueueHandler.get_message_custom_attributes(record)

        request_id = message_attributes.get("request_id")
        if not request_id:
            raise ValueError("request_id is required in message attributes")

        return {
            "message_group_id": attributes.get("MessageGroupId"),
            "message_deduplication_id": attributes.get("MessageDeduplicationId"),
            "request_id": request_id,
            "user_id": message_attributes.get("user_id"),
        }

    @classmethod
    def _send_to_output_queue(cls, message_body: dict, record_attributes: dict) -> None:
        LocalQueueHandler.send_message_to_output_queue(
            message_body=message_body,
            attributes={
                "message_group_id": record_attributes["message_group_id"],
                "message_deduplication_id": record_attributes["message_deduplication_id"],
            },
            request_id=record_attributes["request_id"],
            user_id=record_attributes["user_id"],
        )

    @classmethod
    def process_message(cls, record: dict) -> None:
        """Implements LocalQueueConsumer.process_message."""
        message_id = record.get("MessageId")
        cls._log.info(f"[AGENT START] Processing message {message_id}")

        body = BaseRunRequest.model_validate(json.loads(record["Body"]))
        record_attributes = cls._get_record_attributes(record)

        cls._log.info(
            f"[AGENT PROCESSING] request_id={record_attributes['request_id']}, "
            f"session_id={body.session_id}, agent={body.agent}, prompt={body.prompt[:50] if body.prompt else 'N/A'}"
        )

        _, agent_response = cls._get_chat_service().process_chat_request(req=body)

        cls._log.info(
            f"[AGENT RESPONSE] request_id={record_attributes['request_id']}, "
            f"response_keys={list(agent_response.keys()) if isinstance(agent_response, dict) else 'N/A'}"
        )

        cls._send_to_output_queue(message_body=agent_response, record_attributes=record_attributes)

        cls._log.info(f"[AGENT DONE] Sent to output queue, request_id={record_attributes['request_id']}")

    @classmethod
    def on_permanent_failure(cls, record: dict) -> None:
        """Implements LocalQueueConsumer.on_permanent_failure. Catches own exceptions."""
        cls._log.error(f"Permanent failure for message {record.get('MessageId')}")
        try:
            record_attributes = cls._get_record_attributes(record)
            error_body = {"error": f"Failed to process message after {cls._config.execution.queues.input.max_receive_count} retries"}
            cls._send_to_output_queue(message_body=error_body, record_attributes=record_attributes)
        except Exception:
            cls._log.exception("Failed to send permanent-failure error to output queue")
