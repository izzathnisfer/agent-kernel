"""
Agent-initiated conversations over Telegram (single-process REST deployment).

TelegramInitiationHandler below plays both request-handler and response-
handler roles: inbound, it resolves the chat id through the Session ID
Mapping (Telegram has no per-message "thread" concept — the chat id itself
is the mapped identifier, so any message from that chat continues the
conversation); outbound, implementing InitiationSender makes it the send
point RESTAPI.run() wires up automatically, binding the session_id <-> chat
id mapping after each send.

Try it: ask the agent to notify a chat id, naming yourself in the message
since Telegram has no requester-identity signal to read automatically
(unlike Slack's sender mentions): "tell chat 555555 that James will be late
because of traffic." The recipient's next reply from that chat continues the
new conversation with full context — see README.md for the full walkthrough
and the "recipient must have messaged the bot first" caveat.
"""

import asyncio
from typing import Optional

from agentkernel.api import RESTAPI
from agentkernel.core.initiation import InitiationSender
from agentkernel.openai import OpenAIModule
from agentkernel.telegram import AgentTelegramRequestHandler
from agents import Agent as OpenAIAgent

general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general questions",
    instructions=(
        "You provide assistance with general queries. Give short and clear answers suitable for Telegram "
        "messaging.\n"
        "When asked to message, notify, or inform another user, follow these steps exactly:\n"
        "1. Extract the recipient's Telegram chat id (a number) from the request.\n"
        "2. Extract the requester's own name from the request text — they must state it themselves (e.g. "
        '"tell chat 555555 that James will be late"), since Telegram has no reliable way to read who is '
        "chatting with you. If no name is given, say the requester is unidentified rather than guessing.\n"
        '3. Compose `prompt` as third-person background naming both explicitly — e.g. "James asked you to '
        'be told he will be late because of traffic." Never phrase it in first person ("I will be late") — '
        "the notifier is not the requester.\n"
        "4. Call initiate_conversation exactly once, with `target` set to the recipient's chat id and "
        "agent='notifier' — never call this tool more than once per request."
    ),
)

notifier_agent = OpenAIAgent(
    name="notifier",
    handoff_description="Composes outbound notification messages",
    instructions=(
        "You write messages that are delivered to a recipient verbatim, based on third-person background "
        'like "James asked you to be told he will be late because of traffic." Reply with exactly the text '
        "the recipient should read: a direct, friendly notification of one or two sentences, attributed to "
        'the requester by name (e.g. "Hi! Just a heads up from James — he will be late, held up in '
        'traffic."). Never write in first person as if you are the requester — you are relaying it on '
        'their behalf. Never reply with meta commentary like "Sure, I can send that" — your reply IS the '
        "message."
    ),
)


OpenAIModule([general_agent, notifier_agent])


class TelegramInitiationHandler(AgentTelegramRequestHandler, InitiationSender):
    """Telegram handler that can also deliver agent-initiated messages."""

    def send_initiation_message(self, target: str, message: str, target_details: Optional[dict] = None) -> str:
        """
        Send the initiation message via Telegram.

        :param target: Recipient's Telegram chat id, as a string.
        :param message: The agent-composed outbound text.
        :param target_details: Unused here; available for platform extras.
        :return: The target chat id itself — resolve_session_id() maps inbound messages
                 by their chat id (Telegram has no per-message thread id), so the chat id
                 IS the identifier to bind.
        """
        asyncio.run(self._send_message(int(target), message))
        return target


if __name__ == "__main__":
    RESTAPI.run([TelegramInitiationHandler()])
