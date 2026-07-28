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

import logging
from typing import Optional

from agentkernel import Agent, PostHook, PreHook, Session
from agentkernel.api import RESTAPI
from agentkernel.core import AgentRequestAny, AgentRequestText, ToolContext
from agentkernel.core.initiation import InitiationSender
from agentkernel.openai import OpenAIModule, OpenAIToolBuilder
from agentkernel.slack import AgentSlackRequestHandler
from agents import Agent as OpenAIAgent

_log = logging.getLogger("ak.example.slack_initiation")


def _trace(step: str, detail: str) -> None:
    """
    Emits one aligned, prefixed line marking a step of the initiation round trip.

    The logger sits under the ``ak.`` namespace on purpose: AKLogger configures a handler,
    formatter and level on the ``ak`` logger and sets ``propagate = False``, so a child of it
    is formatted and filtered like the library's own output. A logger named after this module
    instead (``logging.getLogger(__name__)``) would reach the root logger, which nothing
    configures unless ``logging.system.level`` is set — Python's lastResort handler would then
    drop every line below WARNING.

    :param step: Short step label, padded so the lines align in a busy console.
    :param detail: The step's specifics.
    """
    _log.info(f"┃ {step:<8} {detail}")


def _prompt_text(requests: list) -> str:
    """
    Extracts the first text prompt from a request list, shortened for one-line logging.

    :param requests: The request list handed to the agent.
    :return: The prompt text truncated to 110 characters, or "" when there is none.
    """
    for req in requests:
        if isinstance(req, AgentRequestText):
            text = req.prompt.replace("\n", " ")
            return text if len(text) <= 110 else f"{text[:107]}..."
    return ""


class TraceAskPreHook(PreHook):
    """Logs the inbound prompt and which agent and session are about to handle it."""

    async def on_run(self, session: Session, agent: Agent, requests: list) -> list:
        _trace("ASK", f"agent={agent.name} session={session.id} prompt={_prompt_text(requests)!r}")
        return requests

    def name(self) -> str:
        return "trace-ask"


class TraceReplyPostHook(PostHook):
    """
    Logs the agent's reply — for the notifier this is the text the recipient will read.

    The reply is read with ``str()`` rather than a named field, which covers every reply type:
    ``AgentReplyText`` returns its ``response``, ``AgentReplyImage`` appends the attachment
    note, ``AgentReplyAny`` its structured content. That is how ``core/initiation/tool.py``
    reads the composed message too.
    """

    async def on_run(self, session: Session, requests: list, agent: Agent, agent_reply) -> object:
        reply = str(agent_reply).replace("\n", " ")
        _trace("REPLY", f"agent={agent.name} session={session.id} text={reply[:110]!r}")
        return agent_reply

    def name(self) -> str:
        return "trace-reply"


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


_module = OpenAIModule([general_agent, notifier_agent])
for _agent in (general_agent, notifier_agent):
    _module.pre_hook(_agent, [TraceAskPreHook()])
    _module.post_hook(_agent, [TraceReplyPostHook()])


class SlackInitiationHandler(AgentSlackRequestHandler, InitiationSender):
    """Slack handler that can also deliver agent-initiated messages."""

    def resolve_session_id(self, messaging_integration_thread_id: str) -> str:
        """
        Resolves an inbound thread id, logging whether it hit the Session ID Mapping.

        Overriding only to trace the decision — it defers to the SessionIdResolver mixin for
        the actual lookup. A "mapped" line means the reply was threaded under a message the
        agent initiated, so it continues that session; "unmapped" means it starts a new one.

        :param messaging_integration_thread_id: The thread_ts Slack derived for this message.
        :return: The mapped session id, or the given id when no mapping applies.
        """
        session_id = super().resolve_session_id(messaging_integration_thread_id)
        if session_id == messaging_integration_thread_id:
            _trace("INBOUND", f"thread_ts={messaging_integration_thread_id} unmapped -> new session")
        else:
            _trace("INBOUND", f"thread_ts={messaging_integration_thread_id} mapped -> session={session_id}")
        return session_id

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
            _trace("SEND", f"target={target} channel={channel}")
            response = await client.chat_postMessage(channel=channel, text=message)
            _trace("SENT", f"ts={response['ts']} — a threaded reply under this ts continues the initiated session")
            return response["ts"]

        return asyncio.run(_send())


if __name__ == "__main__":
    RESTAPI.run([SlackInitiationHandler()])
