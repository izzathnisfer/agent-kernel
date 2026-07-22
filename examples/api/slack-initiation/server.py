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

Two agents ("general" facing the requester, "notifier" facing the recipient)
ground identity explicitly in the composed prompt rather than forwarding raw
first-person phrasing, which would leave the new session unable to tell
requester and recipient apart — see README.md's "How it works" for the full
rationale and a worked example.
"""

from typing import Optional

from agentkernel.api import RESTAPI
from agentkernel.core import AgentRequestAny, PostHook, ToolContext
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


general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general questions",
    instructions=(
        "You provide assistance with general queries. Give short and clear answers.\n"
        "When asked to message, notify, or inform another user, follow these steps exactly:\n"
        "1. Extract the recipient's member id from the Slack mention in the message text (mentions arrive "
        "as <@MEMBERID>, e.g. <@U0AB12CD3>).\n"
        "2. Call get_requester_id exactly once to learn who is actually asking (Slack message text never "
        "self-mentions the sender, so this cannot be read from the text).\n"
        '3. Compose `prompt` as third-person background naming both explicitly — e.g. "<@U0REQUESTER> '
        'asked you to let <@U0RECIPIENT> know they will be late because of traffic." Never phrase it in '
        'first person ("I will be late") — the notifier is not the requester. If get_requester_id '
        "returns nothing, say the requester is unidentified rather than guessing.\n"
        "4. Call initiate_conversation exactly once, with `target` set to the recipient's member id "
        "(never the requester's own id) and agent='notifier' — never call this tool more than once per "
        "request.\n"
        "If the request names a person without a Slack mention or member id, ask the requester to "
        "@-mention them instead of guessing who they mean."
    ),
    tools=OpenAIToolBuilder.bind([get_requester_id]),
)

notifier_agent = OpenAIAgent(
    name="notifier",
    handoff_description="Composes outbound notification messages",
    instructions=(
        "You write messages that are delivered to a recipient verbatim, based on third-person background "
        'like "<@U0REQUESTER> asked you to let <@U0RECIPIENT> know they will be late because of traffic." '
        "Reply with exactly the text the recipient should read: a direct, friendly notification of one or "
        'two sentences, attributed to the requester by their mention (e.g. "Hi! Just a heads up — '
        '<@U0REQUESTER> will be late, held up in traffic."). Never write in first person as if you are the '
        "requester or the one the fact is about — you are relaying it on their behalf. Never reply with "
        'meta commentary like "Sure, I can send that" — your reply IS the message.\n'
        "Remember the requester's identity and the reason from this background: if the recipient later "
        'asks something like "who said that?" or "why?", answer from these facts (e.g. "<@U0REQUESTER> '
        'did." / "Traffic."), not from who is currently asking — the recipient asking the question is '
        "never the requester."
    ),
)


class PrintSessionContextHook(PostHook):
    """
    Demo aid: prints the new session's retained turn count and roles right after
    notifier composes the outbound message, showing the seeding exchange landed
    in the new session's history (see the README's "Try it" section). Prints
    roles only, not message content, to stay a one-line log rather than a dump.
    """

    async def on_run(self, session, requests, agent, agent_reply):
        items = await session.get("openai").get_items()
        roles = [item.get("role", "?") for item in items]
        print(
            f"[SESSION-CONTEXT-DEMO] initiated session context | session_id={session.id} | turns={len(items)} | roles={roles}"
        )
        return agent_reply

    def name(self) -> str:
        return "print-session-context"


OpenAIModule([general_agent, notifier_agent]).post_hook(notifier_agent, [PrintSessionContextHook()])


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
