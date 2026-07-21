"""
Agent-initiated conversations over Slack (AWS Lambda serverless deployment).

Three Lambdas connected by SQS, the same scalable shape as
examples/aws-serverless/scalable-openai/: this request handler receives the
Slack webhook and enqueues, lambda_agent_runner.py executes the agent, and
lambda_response_handler.py delivers the reply back to Slack.

Unlike examples/api/slack-initiation/ (single-process REST, synchronous), this
handler must not block on the agent round trip — it acks Slack and enqueues
only; the reply reaches Slack later, out of band, via the response handler.

Session id resolution reuses the exact same rule as the REST example
(AgentSlackRequestHandler.handle(), ak-py/src/agentkernel/integration/slack/slack_chat.py):
a reply must be threaded to continue an agent-initiated conversation — an
un-threaded reply's own ts never matches a bound mapping, so it starts a new
session rather than guessing which prior conversation it's answering. The
resolution rule is inlined below (rather than importing the SessionIdResolver
mixin) because this is a plain function handler registered via Lambda.register,
not a RESTRequestHandler subclass — Lambda's REST routing here is a hand-rolled
(event, context) -> response dispatch, not ASGI.

channel and thread_ts are attached as custom SQS attributes so the agent runner
and response handler can carry them through to delivery (see their module
docstrings) — the Agent Runner's response body has no room for platform
routing data, so it has to ride along on the message itself.
"""

import base64
import json
import logging
import os
from typing import Any, Optional

from agentkernel.aws import Lambda, SQSHandler
from agentkernel.core.config import AKConfig
from agentkernel.core.initiation import InitiationManager
from slack_sdk.signature import SignatureVerifier

logger = logging.getLogger("ak.example.slack_initiation.request_handler")


def _build_signature_verifier() -> Optional[SignatureVerifier]:
    secret = os.getenv("SLACK_SIGNING_SECRET")
    if not secret:
        logger.error("SLACK_SIGNING_SECRET missing — all requests will be rejected")
        return None
    return SignatureVerifier(signing_secret=secret)


_signature_verifier = _build_signature_verifier()


def _get_header(headers: dict, name: str) -> Optional[str]:
    """Case-insensitive header lookup — API Gateway does not normalize header case."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _is_valid_slack_request(event: dict, body: str) -> bool:
    if _signature_verifier is None:
        return False
    headers = event.get("headers") or {}
    return _signature_verifier.is_valid(
        body=body,
        timestamp=_get_header(headers, "X-Slack-Request-Timestamp"),
        signature=_get_header(headers, "X-Slack-Signature"),
    )


def resolve_session_id(thread_id: str) -> str:
    """
    Mirrors SessionIdResolver.resolve_session_id (core/initiation/manager.py):
    a mapping hit resolves to its initiated session, a miss keeps the
    platform-derived id unchanged (identity fallback — feature-disabled and
    reactive-conversation cases behave identically, no branching needed here).
    """
    manager = InitiationManager.get()
    return manager.resolve_session_id(thread_id) if manager is not None else thread_id


@Lambda.register("/slack/events", method="POST")
def handle_slack_events(event: dict, _context: Any) -> tuple[int, dict]:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    if not _is_valid_slack_request(event, body):
        return 401, {"error": "Invalid Slack request signature"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 400, {"error": "Invalid JSON payload"}

    if payload.get("type") == "url_verification":
        return 200, {"challenge": payload.get("challenge", "")}

    if payload.get("type") != "event_callback":
        return 200, {"ok": True}

    slack_event = payload.get("event", {})
    if slack_event.get("type") != "message":
        return 200, {"ok": True}

    # Avoid the bot responding to its own messages.
    if slack_event.get("subtype") == "bot_message" or slack_event.get("bot_id") is not None:
        return 200, {"ok": True}

    text = (slack_event.get("text") or "").strip()
    if not text:
        return 200, {"ok": True}

    request_id = payload.get("event_id") or slack_event.get("client_msg_id")
    if not request_id:
        return 400, {"error": "Missing Slack event identity"}

    queue_url = AKConfig.get().execution.queues.input.url
    if not queue_url:
        logger.error("Input queue URL is not configured")
        return 500, {"error": "Input queue URL is not configured"}

    channel = slack_event.get("channel")
    user = slack_event.get("user")
    # thread_ts is populated for a threaded reply; otherwise fall back to the
    # message's own ts — a fresh id every time, so an un-threaded reply always
    # starts a new session (see the module docstring).
    thread_ts = slack_event.get("thread_ts") or slack_event.get("ts")
    session_id = resolve_session_id(thread_ts)

    slack_agent = AKConfig.get().slack.agent or None
    custom_attributes = [
        SQSHandler.CustomAttribute(name="channel", value=channel, datatype=SQSHandler.AttributeDataType.STRING),
        SQSHandler.CustomAttribute(name="thread_ts", value=thread_ts, datatype=SQSHandler.AttributeDataType.STRING),
    ]

    # The raw Slack event rides along as an extra "body" field: ChatService turns
    # unknown body fields into AgentRequestAny entries on the agent's ToolContext,
    # which is how the get_requester_id tool (lambda_agent_runner.py) learns who
    # sent the message — same contract as the REST example, where the handler
    # appends AgentRequestAny(name="body") before running the agent in-process.
    SQSHandler.send_message_to_input_queue(
        message_body={"prompt": text, "agent": slack_agent, "session_id": session_id, "body": slack_event},
        attributes={"message_group_id": session_id, "message_deduplication_id": request_id},
        request_id=request_id,
        user_id=user,
        custom_message_attributes=custom_attributes,
    )

    logger.info(f"Enqueued Slack message — session_id={session_id} request_id={request_id} channel={channel}")
    return 200, {"ok": True, "queued": True, "request_id": request_id}


handler = Lambda.handler
