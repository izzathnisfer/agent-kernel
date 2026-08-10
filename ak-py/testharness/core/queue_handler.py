from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel

from agentkernel.core.config import AKConfig
from agentkernel.deployment.common.queue_handler import QueueHandler

from .queue_store import LocalQueue


class LocalQueueHandler(QueueHandler):
    """
    SQLite-backed QueueHandler. Owns the entire storage-engine boundary — every read or
    write against LocalQueue goes through here, on both the send side and the receive side.

    Mirrors SQSHandler's role for the AWS backend, but talks to a LocalQueue instead of
    boto3, and requires no AWS SDK.
    """

    _queue: Optional[LocalQueue] = None
    _config = None

    class AttributeDataType(str, Enum):
        STRING = "String"
        NUMBER = "Number"
        BINARY = "Binary"

    class CustomAttribute(BaseModel):
        """User-facing local-queue attribute definition, mirrors SQSHandler.CustomAttribute."""

        name: str
        value: Any
        datatype: "LocalQueueHandler.AttributeDataType"

    @classmethod
    def _get_config(cls):
        if cls._config is None:
            cls._config = AKConfig.get()
        return cls._config

    @classmethod
    def _get_queue(cls) -> LocalQueue:
        if cls._queue is None:
            queues = cls._get_config().execution.queues
            db_path = queues.input.url or queues.output.url
            if not db_path:
                raise ValueError("execution.queues.input.url (or output.url) must be set to the local SQLite queue file path")
            cls._queue = LocalQueue(db_path)
        return cls._queue

    @classmethod
    def _build_message_attribute(cls, custom_attribute: "LocalQueueHandler.CustomAttribute") -> Dict[str, Any]:
        return {"DataType": custom_attribute.datatype.value, "StringValue": str(custom_attribute.value)}

    @classmethod
    def _build_message_attributes(cls, message_attributes: Optional[List["LocalQueueHandler.CustomAttribute"]]) -> Dict[str, Any]:
        built: Dict[str, Any] = {}
        for custom_attribute in message_attributes or []:
            if custom_attribute.name in built:
                raise ValueError(f"Duplicate message attribute name: {custom_attribute.name}")
            built[custom_attribute.name] = cls._build_message_attribute(custom_attribute)
        return built

    @classmethod
    def _build_standard_message_attributes(
        cls,
        request_id: Optional[str],
        user_id: Optional[str],
        custom_message_attributes: Optional[List["LocalQueueHandler.CustomAttribute"]],
    ) -> List["LocalQueueHandler.CustomAttribute"]:
        message_attributes = []
        if request_id is not None:
            message_attributes.append(cls.CustomAttribute(name="request_id", value=request_id, datatype=cls.AttributeDataType.STRING))
        if user_id is not None:
            message_attributes.append(cls.CustomAttribute(name="user_id", value=user_id, datatype=cls.AttributeDataType.STRING))
        message_attributes.extend(custom_message_attributes or [])
        return message_attributes

    @staticmethod
    def _get_session_id(message_body: Any) -> Optional[str]:
        if isinstance(message_body, Mapping):
            return message_body.get("session_id")
        return getattr(message_body, "session_id", None)

    @classmethod
    def _send(
        cls,
        queue_name: str,
        message_body: Any,
        message_group_id: Optional[str],
        message_deduplication_id: Optional[str],
        message_attributes: List["LocalQueueHandler.CustomAttribute"],
    ) -> Dict[str, Any]:
        body = message_body.model_dump(exclude_none=True) if hasattr(message_body, "model_dump") else message_body
        return cls._get_queue().enqueue(
            queue_name=queue_name,
            body=body,
            attributes=cls._build_message_attributes(message_attributes),
            message_group_id=message_group_id,
            message_deduplication_id=message_deduplication_id,
        )

    @classmethod
    def send_message_to_input_queue(
        cls,
        message_body: "LocalQueueHandler.QueueMessageBody | Dict[str, Any]",
        attributes: "LocalQueueHandler.SendMessageAttributes | Dict[str, Any] | None" = None,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
        custom_message_attributes: Optional[List[Any]] = None,
        **extra_kwargs: Any,
    ) -> Dict[str, Any]:
        body = cls.QueueMessageBody.model_validate(message_body)
        send_attributes = cls.SendMessageAttributes.model_validate(attributes or {})
        return cls._send(
            queue_name="input",
            message_body=body,
            message_group_id=send_attributes.message_group_id or body.session_id,
            message_deduplication_id=send_attributes.message_deduplication_id,
            message_attributes=cls._build_standard_message_attributes(request_id, user_id, custom_message_attributes),
        )

    @classmethod
    def send_message_to_output_queue(
        cls,
        message_body: Any,
        attributes: "LocalQueueHandler.SendMessageAttributes | Dict[str, Any] | None" = None,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
        custom_message_attributes: Optional[List[Any]] = None,
        **extra_kwargs: Any,
    ) -> Dict[str, Any]:
        send_attributes = cls.SendMessageAttributes.model_validate(attributes or {})
        return cls._send(
            queue_name="output",
            message_body=message_body,
            message_group_id=send_attributes.message_group_id or cls._get_session_id(message_body),
            message_deduplication_id=send_attributes.message_deduplication_id,
            message_attributes=cls._build_standard_message_attributes(request_id, user_id, custom_message_attributes),
        )

    @classmethod
    def receive_messages(cls, queue_name: str, batch_size: int, visibility_timeout: float) -> List[Dict[str, Any]]:
        """Not on the shared QueueHandler ABC — the local equivalent of ECSSQSConsumer talking to boto3 directly."""
        return cls._get_queue().receive(queue_name=queue_name, batch_size=batch_size, visibility_timeout=visibility_timeout)

    @classmethod
    def delete_message(cls, queue_name: str, message_id: str) -> None:
        """Not on the shared QueueHandler ABC — see receive_messages."""
        cls._get_queue().delete_by_id(int(message_id))

    @staticmethod
    def get_message_system_attributes(raw_queue_message: Mapping[str, Any]) -> Dict[str, Any]:
        """Return a raw local queue message's system attributes (MessageGroupId, MessageDeduplicationId, ApproximateReceiveCount)."""
        return dict(raw_queue_message.get("Attributes") or {})

    @staticmethod
    def get_message_custom_attributes(raw_queue_message: Mapping[str, Any]) -> Dict[str, Any]:
        """Return a raw local queue message's custom MessageAttributes, flattened to name -> value."""
        message_attributes = raw_queue_message.get("MessageAttributes") or {}
        flattened: Dict[str, Any] = {}
        for name, attribute in message_attributes.items():
            value = attribute.get("StringValue") if isinstance(attribute, Mapping) else attribute
            if value is not None:
                flattened[name] = value
        return flattened


# Tell Pydantic to resolve the string annotation for CustomAttribute.datatype after the class is fully defined.
LocalQueueHandler.CustomAttribute.model_rebuild()
