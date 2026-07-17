"""
Agent-initiated conversations over Slack (single-process REST deployment).

The handler below plays both request-handler and response-handler roles:

- Inbound: AgentSlackRequestHandler already resolves each reply's thread_ts
  through the Session ID Mapping (RESTRequestHandler.resolve_session_id), so a
  reply to an agent-initiated message continues the initiated session.
- Outbound: implementing InitiationSender makes this handler the send point for
  initiation messages — RESTAPI.run() detects it and registers the in-process
  dispatcher, which sends via send_initiation_message() and then binds the
  session_id <-> thread_ts mapping (and the AK conversation thread, when
  enabled) automatically.

Try it: ask the agent in one Slack thread to "inform @someone that ..." — the
initiate_conversation system tool composes and sends a new message to that
user, and their reply continues the new conversation with full context.

Two agents demonstrate a clean split of roles:
- "general" faces the requester: it extracts the recipient's member id from the
  Slack mention (mentions arrive as <@U...>) and calls initiate_conversation.
- "notifier" faces the recipient: its reply to the tool's prompt is delivered
  verbatim as the outbound message, so its instructions make it write the
  notification itself — no "Sure, I can help draft that" meta-chatter.

When the recipient replies, the handler resolves the initiated session and the
default agent ("general", the first registered) continues it — the composed
exchange is already in that session's history, so the context carries over.
"""

from typing import Optional

from agentkernel.api import RESTAPI
from agentkernel.core.initiation import InitiationSender
from agentkernel.openai import OpenAIModule
from agentkernel.slack import AgentSlackRequestHandler
from agents import Agent as OpenAIAgent

general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general questions",
    instructions=(
        "You provide assistance with general queries. Give short and clear answers.\n"
        "Slack renders user mentions in message text as <@MEMBERID>, e.g. <@U0AB12CD3>. When asked to "
        "message, notify, or inform another user, extract the member id from the mention and call "
        "initiate_conversation with it as `target` and with agent='notifier'; phrase `prompt` as a clear "
        'instruction such as "Inform them that the deployment finished successfully". If the request '
        "names a person without a Slack mention or member id, ask the requester to @-mention them."
    ),
)

notifier_agent = OpenAIAgent(
    name="notifier",
    handoff_description="Composes outbound notification messages",
    instructions=(
        "You write messages that are delivered to a user verbatim. Given an instruction like "
        '"Inform them that ...", reply with exactly the text the recipient should read: a direct, '
        'friendly notification of one or two sentences, addressed to them (e.g. "Hi! The deployment '
        'finished successfully."). Never reply with meta commentary like "Sure, I can send that" — '
        "your reply IS the message."
    ),
)

OpenAIModule([general_agent, notifier_agent])


class SlackInitiationHandler(AgentSlackRequestHandler, InitiationSender):
    """Slack handler that can also deliver agent-initiated messages."""

    def send_initiation_message(self, target: str, message: str, target_details: Optional[dict] = None) -> str:
        """
        Send the initiation message via Slack and return its thread id.

        :param target: Slack channel id or member id (a member id opens a DM).
        :param message: The agent-composed outbound text.
        :param target_details: Unused here; available for platform extras.
        :return: The posted message's ts — the thread root a reply arrives under.
        """
        import asyncio

        async def _send() -> str:
            client = self._slack_app.client
            channel = target
            # A Slack member id (U.../W...) needs a DM channel opened first.
            if target.startswith(("U", "W")):
                opened = await client.conversations_open(users=target)
                channel = opened["channel"]["id"]
            response = await client.chat_postMessage(channel=channel, text=message)
            # DM replies arrive as top-level messages keyed by the DM channel id;
            # channel replies arrive threaded under the posted message's ts.
            return channel if channel != target else response["ts"]

        return asyncio.run(_send())


if __name__ == "__main__":
    RESTAPI.run([SlackInitiationHandler()])
