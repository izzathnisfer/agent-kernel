"""
Output Queue consumer that delivers agent replies to Slack (ECS containerized
deployment).

Two message types reach this handler on the Output Queue:
- Ordinary chat replies: channel/thread_ts arrive as custom SQS attributes,
  carried through by app_agent_runner.py from the original inbound Slack
  event (see its module docstring) — that's already the correct thread to
  reply into, whether or not this is part of an agent-initiated conversation.
- INITIATION messages: the agent proactively contacting someone via the
  initiate_conversation tool (see docs/docs/advanced/conversation-initiation.md).
  The stock ECSOutputConsumer logs a warning and drops these by design — this
  override is the documented extension point (spec.md "Consumer changes") that
  implements delivery and then calls InitiationManager.complete(), which binds
  the session_id <-> thread_ts mapping so the recipient's threaded reply
  resolves back to this session (see slack_request_handler.py's
  resolve_session_id).
"""

import asyncio
import json
import logging
import os
from typing import Optional

from agentkernel.aws import ECSOutputConsumer, SQSHandler
from agentkernel.core.initiation import INITIATION_MESSAGE_TYPE, InitiationManager, InitiationMessage
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger("ak.example.slack_initiation.output_consumer")


def _build_slack_client() -> Optional[AsyncWebClient]:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — replies cannot be delivered")
        return None
    return AsyncWebClient(token=token)


_slack_client = _build_slack_client()


class SlackECSOutputConsumer(ECSOutputConsumer):
    @classmethod
    async def _deliver_initiation(cls, initiation: InitiationMessage) -> str:
        """
        Mirrors SlackInitiationHandler.send_initiation_message in
        examples/api/slack-initiation/server.py: opens a DM if the target
        looks like a Slack member id, else posts directly to it as a channel
        id. Returns the posted message's ts — the thread root a reply must be
        threaded under to continue this conversation.
        """
        channel = initiation.target
        if initiation.target.startswith(("U", "W")):
            opened = await _slack_client.conversations_open(users=initiation.target)
            channel = opened["channel"]["id"]
        response = await _slack_client.chat_postMessage(channel=channel, text=initiation.message)
        return response["ts"]

    @classmethod
    async def _deliver_reply(cls, channel: str, thread_ts: Optional[str], text: str) -> None:
        await _slack_client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)

    @classmethod
    def process_message(cls, record: dict) -> None:
        if _slack_client is None:
            logger.error("Slack client unavailable — dropping message")
            return

        message_attributes = SQSHandler.get_message_custom_attributes(record)

        if message_attributes.get("message_type") == INITIATION_MESSAGE_TYPE:
            initiation = InitiationMessage.model_validate_json(record["Body"])
            thread_ts = asyncio.run(cls._deliver_initiation(initiation))
            manager = InitiationManager.get()
            if manager is not None:
                # Required after a successful send — skipping it means the
                # recipient's reply can't resolve back to this session.
                manager.complete(initiation, thread_ts)
            else:
                logger.error(
                    "InitiationManager unavailable (agent-initiated conversations not enabled) — mapping was not bound"
                )
            return

        # Ordinary reply: store as usual (debug/poll parity), then deliver to Slack.
        super().process_message(record)

        channel = message_attributes.get("channel")
        if not channel:
            logger.warning("Output message has no 'channel' custom attribute — Slack delivery skipped")
            return

        body = record.get("Body", "{}")
        if isinstance(body, str):
            body = json.loads(body)
        text = body.get("result") or body.get("error") or "Request processed."

        asyncio.run(cls._deliver_reply(channel, message_attributes.get("thread_ts"), text))
