"""
Agent-initiated conversations over Slack (single-process REST deployment).

SlackInitiationHandler below plays both request-handler and response-handler
roles: inbound, it resolves each reply's thread_ts through the Session ID
Mapping (an un-threaded reply starts a fresh session instead of guessing which
prior conversation it continues); outbound, implementing InitiationSender
makes it the send point RESTAPI.run() wires up automatically, binding the
session_id <-> thread_ts mapping after each send.

Try it: ask the agent in a Slack channel to "tell @someone that ..." — the
initiate_conversation system tool composes and sends a new message to that
user, and their reply continues the new conversation with full context.

Two agents split the outbound work: "general" reads the requester's ask and
composes a third-person background, "notifier" turns that background into the
message the recipient reads. Grounding identity in the background rather than
forwarding raw first-person phrasing is what stops the new session confusing
requester and recipient.

Replies are answered by "general", not "notifier" — reply-side agent selection
is request-based (the agent named by ``slack.agent``), so the composing agent
never sees them. "general" therefore has two modes and picks between them by
looking for BACKGROUND_MARKER at the top of the history. The background also
carries facts the requester asked to keep out of the first message but which the
conversation may still need: it is the only context an initiated session ever
gets, so a fact omitted there is gone for good.

See README.md's "How it works" for the full rationale and a worked example.
"""

from typing import Optional

from agentkernel.api import RESTAPI
from agentkernel.core import AgentRequestAny, ToolContext
from agentkernel.core.initiation import InitiationSender
from agentkernel.openai import OpenAIModule, OpenAIToolBuilder
from agentkernel.slack import AgentSlackRequestHandler
from agents import Agent as OpenAIAgent


def get_requester_id() -> str:
    """Returns the Slack member id of whoever sent the current message, or "" if unknown."""
    for req in ToolContext.get().requests:
        if isinstance(req, AgentRequestAny) and req.name == "body":
            return req.content.get("user", "")
    return ""


BACKGROUND_MARKER = "NOTIFICATION BACKGROUND"
"""
First words of the background prompt handed to the notifier when a conversation is initiated.

It is a literal marker so both agents can tell a background apart from a real user message with a
string check rather than an inference. That distinction is what lets one agent both start outbound
conversations and answer the replies to them: seeing this marker at the top of the history means the
current message came from the recipient, not the requester.
"""

general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general questions",
    instructions=(
        "You provide assistance with general queries. Give short and clear answers.\n"
        "\n"
        "FIRST, decide which situation you are in by looking at the conversation history.\n"
        f"If the history begins with '{BACKGROUND_MARKER}', you are talking to the RECIPIENT of a "
        "notification this conversation already sent. Follow the RECIPIENT rules. Otherwise follow the "
        "REQUESTER rules.\n"
        "\n"
        "REQUESTER rules — someone is asking you to message, notify or inform another user:\n"
        "1. Extract the recipient's member id from the Slack mention in the message text (mentions arrive "
        "as <@MEMBERID>, e.g. <@U0AB12CD3>).\n"
        "2. Call get_requester_id exactly once to learn who is actually asking (Slack message text never "
        "self-mentions the sender, so this cannot be read from the text).\n"
        "3. Compose `prompt` in exactly this shape, in the third person:\n"
        f"   {BACKGROUND_MARKER} — requester <@U0REQUESTER>, recipient <@U0RECIPIENT>.\n"
        "   Share: <the facts that belong in the message>\n"
        "   Withhold unless asked: <facts the requester gave but asked you not to volunteer>\n"
        "   Include EVERY fact the requester gave you. If they asked you not to tell the recipient "
        "something, that fact still goes under 'Withhold unless asked' — withholding is about the "
        "message, not about the record. Dropping it means it is gone for good: this prompt is the only "
        "context the new conversation will ever have. Omit the 'Withhold unless asked' line entirely "
        "when there is nothing to withhold.\n"
        '   Never phrase any of it in first person ("I will be late") — the notifier is not the '
        "requester. If get_requester_id returns nothing, say the requester is unidentified rather than "
        "guessing.\n"
        "4. Call initiate_conversation exactly once, with `target` set to the recipient's member id "
        "(never the requester's own id) and agent='notifier' — never call this tool more than once per "
        "request.\n"
        "If the request names a person without a Slack mention or member id, ask the requester to "
        "@-mention them instead of guessing who they mean.\n"
        "\n"
        "RECIPIENT rules — you are answering the person who received the notification:\n"
        "- Answer from the background at the top of the history. It names who asked "
        '(the requester) and why, so "who said that?" and "why?" are answerable from it.\n'
        "- If they ask about something listed under 'Withhold unless asked', tell them now. That is "
        'what "unless asked" means — the fact was kept out of the first message, not kept secret.\n'
        "- Never volunteer a withheld fact before they ask for it.\n"
        "- Never call initiate_conversation here. You are continuing an existing conversation, not "
        "starting a new one, and the person you are talking to is the recipient — not the requester.\n"
        "- If the background genuinely does not contain what they asked for, say you do not have that "
        "detail rather than guessing."
    ),
    tools=OpenAIToolBuilder.bind([get_requester_id]),
)

notifier_agent = OpenAIAgent(
    name="notifier",
    handoff_description="Composes outbound notification messages",
    instructions=(
        "You write messages that are delivered to a recipient verbatim, based on a third-person "
        f"background that starts with '{BACKGROUND_MARKER}' and names the requester and recipient.\n"
        "Reply with exactly the text the recipient should read: a direct, friendly notification of one or "
        'two sentences, attributed to the requester by their mention (e.g. "Hi! Just a heads up — '
        '<@U0REQUESTER> will be late, held up in traffic."). Never write in first person as if you are the '
        "requester or the one the fact is about — you are relaying it on their behalf. Never reply with "
        'meta commentary like "Sure, I can send that" — your reply IS the message.\n'
        "Compose the message from the 'Share:' facts ONLY. Anything under 'Withhold unless asked' must not "
        "appear in your reply, in any form — do not hint at it, and do not say that something is being "
        "withheld. It stays in the background as the record, so it can be given to the recipient later if "
        "they ask for it; that answer is not yours to write.\n"
        "You compose the opening message and nothing else — you will not see the recipient's replies."
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
        :return: The posted message's ts — the thread root a reply must be threaded under
                 to continue this conversation (DMs and channels behave identically: an
                 un-threaded reply starts a new session rather than guessing).
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
            return response["ts"]

        return asyncio.run(_send())


if __name__ == "__main__":
    RESTAPI.run([SlackInitiationHandler()])
