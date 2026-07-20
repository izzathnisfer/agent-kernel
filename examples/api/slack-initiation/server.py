"""
Agent-initiated conversations over Slack (single-process REST deployment).

The handler below plays both request-handler and response-handler roles:

- Inbound: AgentSlackRequestHandler already resolves each reply's thread_ts
  through the Session ID Mapping (RESTRequestHandler.resolve_session_id), so a
  reply threaded under an agent-initiated message continues that session. An
  un-threaded reply's own ts never matches a bound mapping, so it starts a new
  session instead — deliberately: for a DM there's no way to tell which of
  several possible prior conversations an un-threaded reply is answering, so
  guessing (e.g. "assume the most recent") would sometimes attribute a reply to
  the wrong conversation. Requiring an explicit thread is unambiguous, at the
  cost of the recipient needing to use Slack's "reply in thread" for the agent
  to remember what it told them.
- Outbound: implementing InitiationSender makes this handler the send point for
  initiation messages — RESTAPI.run() detects it and registers the in-process
  dispatcher, which sends via send_initiation_message() and then binds the
  session_id <-> thread_ts mapping (and the AK conversation thread, when
  enabled) automatically.

Try it: ask the agent in one Slack thread to "tell @someone that ..." — the
initiate_conversation system tool composes and sends a new message to that
user, and their reply continues the new conversation with full context.

initiate_conversation's `prompt` argument is both the context that seeds the
new session and the instruction that produces the outbound message (there is
no separate injection path — the prompt/reply exchange IS the new session's
history, by design). That means the prompt has to be self-disambiguating: if
it's a raw first-person forward of the requester's words ("Inform them that
I'll be late"), nothing in the session says who "I" refers to, and once the
recipient starts replying (also a "user" turn, same session), the model has no
way to tell the original requester and the recipient apart. Asked "who said
that?", it can just as easily answer "you did".

Two agents avoid this by grounding identity explicitly instead of forwarding
raw phrasing:
- "general" faces the requester: it extracts the recipient's member id from
  the Slack mention (mentions arrive as <@U...>), calls the get_requester_id
  tool to learn who is actually asking (Slack doesn't self-mention, so this
  can't be read from the message text), and composes a third-person prompt
  that names the requester explicitly.
- "notifier" faces the recipient: its reply to that prompt is delivered
  verbatim as the outbound message, so its instructions make it write the
  notification itself, in third person, attributed to the requester by name —
  never as if it were speaking for them.

When the recipient replies, the handler resolves the initiated session and the
default agent ("general", the first registered) continues it. Because the
seeding prompt named the requester explicitly, that identity is retained in
the session and follow-ups like "who said that?" or "why?" resolve correctly
instead of being misattributed to the recipient.
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
