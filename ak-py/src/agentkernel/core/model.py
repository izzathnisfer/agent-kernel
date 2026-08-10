import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Callable, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# Session ids derived for scheduled runs carry this prefix. scheduled_task_id is
# caller-choosable and shares a namespace with user-supplied session ids, so without
# the prefix a user session whose id equals a scheduled task's id would share
# conversation state with that task's runs.
SCHEDULED_SESSION_PREFIX = "schedule:"

# Volatile-cache key under which ChatService binds the request's authenticated user id to
# the session. Tool code needs the caller's identity (a scheduled task must have an
# unforgeable human owner) but user_id is deliberately kept out of the agent's request
# context, so it travels on the session instead.
REQUEST_USER_ID_KEY = "ak.request.user_id"


class AgentRequestText(BaseModel):
    """
    AgentRequestText encapsulates a text request to an agent.

    prompt: str  : This is the user input text
    type: Literal["text"]
    """

    prompt: str
    type: Literal["text"] = "text"

    def __str__(self) -> str:
        return self.prompt


class AgentRequestFile(BaseModel):
    """
    AgentRequestFile encapsulates a file attachment request to an agent

    file_data: str  : This could be base64 encoded string or url
    name: str : name of the file
    type: Literal["file"]
    mime_type: str | None = None : Optional. The IANA standard MIME type of the file
    """

    file_data: str  # This could be base64 encoded string or url
    name: str
    type: Literal["file"] = "file"
    mime_type: str | None = None  # Optional. The IANA standard MIME type of the source data


class AgentRequestImage(BaseModel):
    """
    AgentRequestImage encapsulates an image request to an agent

    image_data: str  : This should be base64 encoded string
    name: str : name of the image
    type: Literal["image"]
    mime_type: str | None = None : Optional. The IANA standard MIME type of the image
    """

    prompt: str = ""
    image_data: str
    name: str
    type: Literal["image"] = "image"
    mime_type: str | None = None


class AgentRequestAny(BaseModel):
    """
    AgentRequestAny encapsulates passing any type of request to be handled by the pre-execution hooks. These are not directly handled by the agent kernel runtime.

    content: Any : This could be base64 encoded string or bytes or url
    name: str : name of the data
    type: Literal["other"]
    """

    content: Any
    name: str
    type: Literal["other"] = "other"


class AgentRequestAttachmentRef(BaseModel):
    """
    AgentRequestAttachmentRef references an attachment whose bytes are already
    persisted in the AttachmentStore, carrying only its identifier — no raw data.

    Used on the thread-enabled path: ChatService stores an uploaded attachment's
    bytes up front and replaces the raw image/file request with this reference,
    so no raw bytes travel past storage. MultimodalPreHook reads the id, loads the
    bytes from the AttachmentStore to generate a description, then strips it before
    the agent runs. Handled only by pre-hooks, never passed to the agent itself.

    attachment_id: str : Identifier of the stored attachment.
    type: Literal["attachment_ref"]
    """

    attachment_id: str
    type: Literal["attachment_ref"] = "attachment_ref"


class AgentReplyText(AgentRequestText):
    """
    AgentReplyText encapsulates a text reply from an agent.

    response: str : This is the agent output text
    prompt: str : The text prompt sent to the agent

    Inherits `prompt` (input) and `type` from AgentRequestText, and `response` holds the agent output.
    """

    response: str = ""
    prompt: str = ""

    def __str__(self) -> str:
        return self.response


class AgentReplyImage(AgentRequestImage):
    """
    AgentReplyImage encapsulates a text & image reply from an agent.

    response: str : This is the agent output text

    Inherits `prompt` (input), `image_data`, `name`, `type`, and `mime_type` from
    AgentRequestImage, and `response` holds the agent output text.
    """

    response: str

    def __str__(self) -> str:
        return f"{self.response}. Image {self.name} is attached."


type AgentRequest = Union[AgentRequestText, AgentRequestFile, AgentRequestImage, AgentRequestAny, AgentRequestAttachmentRef]
type AgentReply = Union[AgentReplyText, AgentReplyImage, AgentReplyAny]


class AgentReplyAny(BaseModel):
    """
    AgentReplyAny encapsulates a structured (JSON) reply from an agent.

    content: dict : The structured agent output as a JSON-compatible dict
    prompt: str   : The text prompt sent to the agent
    type: Literal["other"]
    """

    content: dict
    prompt: str = ""
    type: Literal["other"] = "other"

    def __str__(self) -> str:
        return json.dumps(self.content, default=str)

    @classmethod
    def from_output(cls, value: Any, prompt: str = "") -> "AgentReplyAny | None":
        """
        Builds an AgentReplyAny from a framework output value if it is structured.
        Pydantic instances are converted with model_dump(mode="json") so the content
        dict is JSON-compatible; plain dicts are used as content directly.

        :param value: The framework output value to inspect.
        :param prompt: The text prompt sent to the agent.
        :return: An AgentReplyAny, or None when the value is not structured
        (the caller falls back to a text reply).
        """
        if isinstance(value, BaseModel):
            return cls(content=value.model_dump(mode="json"), prompt=prompt)
        if isinstance(value, dict):
            return cls(content=value, prompt=prompt)
        return None


class ExecutionMode(str, Enum):
    """
    Execution mode enumeration for Lambda function behavior.
    """

    REST_SYNC = "rest_sync"
    REST_ASYNC = "rest_async"
    STREAM = "stream"
    ASYNC = "async"


class StreamChunk(BaseModel):
    delta: str | None = None
    done: bool = False
    error: str | None = None


class SystemTool(BaseModel):
    name: str
    description: str
    func: Callable


class FileData(BaseModel):
    """Represents a file attachment"""

    file_data: str  # base64 encoded string or URL
    name: str
    mime_type: Optional[str] = None


class ImageData(BaseModel):
    """Represents an image attachment"""

    image_data: str  # base64 encoded string
    name: str
    mime_type: Optional[str] = None


class ScheduleMode(str, Enum):
    """Conversation mode of a scheduled task.

    PER_RUN gives every fire its own session (``schedule:<id>:<scheduled_time>``);
    CONTINUOUS keeps all fires in one long-running session (``schedule:<id>``).
    """

    PER_RUN = "per_run"
    CONTINUOUS = "continuous"


class ScheduleSpec(BaseModel):
    """The timing expression plus conversation mode — the ``schedule`` block on a chat body.

    Exactly one of ``cron``, ``rate`` or ``at`` must be supplied. ``id`` is the optional
    caller-supplied scheduled_task_id, which makes creation idempotent.
    """

    id: Optional[str] = None
    cron: Optional[str] = None
    rate: Optional[str] = None
    at: Optional[datetime] = None
    mode: ScheduleMode = ScheduleMode.PER_RUN
    timezone: str = "UTC"

    @model_validator(mode="after")
    def _exactly_one_expression(self) -> "ScheduleSpec":
        """Reject a schedule that names no timing expression or more than one."""
        supplied = [name for name in ("cron", "rate", "at") if getattr(self, name) is not None]
        if len(supplied) != 1:
            raise ValueError(f"schedule requires exactly one of cron, rate or at; got {supplied or 'none'}")
        return self


class ScheduledRunMetadata(BaseModel):
    """Correlation metadata for one fire of a scheduled task.

    Stamped by the timer at fire time, echoed through the response verbatim, and read
    only by the output consumer — its presence is how a consumer tells a scheduled run
    from an ordinary one.
    """

    scheduled_task_id: str
    scheduled_task_version: str
    scheduled_time: datetime
    run_id: str

    @classmethod
    def from_body(cls, body: dict) -> "ScheduledRunMetadata | None":
        """Extract the block from an already-parsed body.

        The output consumers call this on every output-queue message, so the miss costs
        one ``dict.get`` and nothing else. A malformed block raises ``ValidationError``:
        on the ordinary path that is a real bug worth surfacing.

        :param body: The parsed response/request body.
        :return: The parsed metadata, or None when the body carries no block.
        """
        if not isinstance(body, dict):
            return None
        raw = body.get("scheduled_run")
        if raw is None:
            return None
        return cls.model_validate(raw)

    @classmethod
    def from_raw_body(cls, raw_body: "str | bytes | dict | None") -> "ScheduledRunMetadata | None":
        """Extract the block from a raw, possibly-unparseable queue body.

        Called only from the runners' ``on_permanent_failure``, which has no error channel
        left and must never raise — so every parse and validation failure returns None.

        :param raw_body: The raw SQS record body.
        :return: The parsed metadata, or None when it cannot be extracted.
        """
        try:
            body = json.loads(raw_body) if isinstance(raw_body, (str, bytes)) else raw_body
            return cls.from_body(body)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            return None


class BaseChatRequest(BaseModel):
    """Base model for chat requests with common fields.

    user_id is required when Conversation Thread Support is enabled (a 'thread'
    block is present in config.yaml); group_id and thread_name are optional and
    applied only when the thread is auto-created on the session's first request.
    """

    prompt: str
    agent: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    thread_name: Optional[str] = None


class BaseRunRequest(BaseChatRequest):
    """Chat request with file and image attachments (base64/URL format)."""

    files: Optional[List[FileData]] = None
    images: Optional[List[ImageData]] = None
    # schedule is create-time only (registers the message to run later); scheduled_run is
    # fire-time only (correlates one run). They never appear on the same request.
    schedule: Optional[ScheduleSpec] = None
    scheduled_run: Optional[ScheduledRunMetadata] = None
    model_config = ConfigDict(extra="allow")


class BaseRequest(BaseModel):
    request_id: Optional[str] = None
    route: Optional[str] = None  # RouteKey of the Websocket, needed for WS implementation
    body: Optional[BaseRunRequest] = None
    model_config = ConfigDict(extra="allow")

    @classmethod
    def from_payload(cls, payload: "BaseRequest | BaseRunRequest | dict[str, Any]") -> "BaseRequest":
        if isinstance(payload, cls):
            return payload

        if isinstance(payload, BaseRunRequest):
            return cls(request_id=str(uuid.uuid4()), body=payload)

        if isinstance(payload, dict):
            request_id = payload.get("request_id") or str(uuid.uuid4())
            user_id = payload.get("user_id")
            route = payload.get("route")

            if "body" in payload and payload["body"] is not None:
                body = payload["body"]
                if isinstance(body, dict):
                    body = {key: value for key, value in body.items() if key not in {"request_id", "user_id", "route"}}
            else:
                body = {key: value for key, value in payload.items() if key not in {"request_id", "user_id", "route", "body"}}

            if not body:
                return cls(request_id=request_id, user_id=user_id, route=route)

            if not isinstance(body, BaseRunRequest):
                body = BaseRunRequest.model_validate(body)

            # The envelope user_id is authoritative — propagate it into the body so
            # body-level consumers (e.g. Conversation Thread Support) can read it.
            if user_id is not None:
                body.user_id = user_id

            return cls(request_id=request_id, user_id=user_id, route=route, body=body)

        raise TypeError(f"Unsupported payload type for BaseRequest: {repr(type(payload))}")
