"""
Agent-initiated conversations over Gmail (single-process polling deployment).

Gmail has no webhook/REST surface in this integration — AgentGmailRequestHandler
polls for new messages — so unlike the other -initiation examples, this one
does not go through RESTAPI.run()'s automatic InitiationSender detection.
main() wires the dispatcher manually with the same send-then-bind contract
RESTAPI._register_initiation_sender() applies for REST deployments.

GmailInitiationHandler's send_initiation_message() composes a fresh email via
the authenticated Gmail API client and returns the new message's threadId —
inbound replies in that thread resolve back to the initiated session through
the Session ID Mapping (see AgentGmailRequestHandler._process_email).

Try it: email the bot's Gmail address asking it to email someone else, naming
yourself since the recipient has no way to see who asked unless the agent
restates it explicitly: "please email alice@example.com and tell her the
report is ready — James". A reply to that email (in the same Gmail thread)
continues the new conversation with full context — see README.md for the
full walkthrough.
"""

import asyncio
import base64
import logging
from email.mime.text import MIMEText
from typing import Optional

from agentkernel.core.initiation import InitiationManager, InitiationMessage, InitiationSender
from agentkernel.gmail import AgentGmailRequestHandler
from agentkernel.openai import OpenAIModule
from agents import Agent as OpenAIAgent

general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general email queries",
    instructions=(
        "You are an AI email assistant. Read incoming emails carefully and reply helpfully, concisely, and "
        "professionally.\n"
        "When asked to email, notify, or inform another person, follow these steps exactly:\n"
        "1. Extract the recipient's email address from the request.\n"
        "2. Extract the requester's own name from the request text, or from the email's own \"From:\" "
        "line if the request doesn't state it. If neither identifies them, say the requester is "
        "unidentified rather than guessing.\n"
        '3. Compose `prompt` as third-person background naming both explicitly — e.g. "James asked you to '
        'be told the report is ready." Never phrase it in first person — the notifier is not the '
        "requester.\n"
        "4. Call initiate_conversation exactly once, with `target` set to the recipient's email address "
        "and agent='notifier' — never call this tool more than once per request.\n"
        "For all other emails, answer the sender's question directly instead."
    ),
)

notifier_agent = OpenAIAgent(
    name="notifier",
    handoff_description="Composes outbound notification emails",
    instructions=(
        "You write emails that are delivered to a recipient verbatim, based on third-person background "
        'like "James asked you to be told the report is ready." Reply with exactly the body text the '
        "recipient should read: a direct, friendly notification of a few sentences, attributed to the "
        'requester by name (e.g. "Hi! Just a heads up from James — the report is ready."). Never write in '
        "first person as if you are the requester — you are relaying it on their behalf. Never add a "
        'subject line or meta commentary like "Sure, I can send that" — your reply IS the email body.'
    ),
)


OpenAIModule([general_agent, notifier_agent])


class GmailInitiationHandler(AgentGmailRequestHandler, InitiationSender):
    """Gmail handler that can also deliver agent-initiated emails."""

    def send_initiation_message(self, target: str, message: str, target_details: Optional[dict] = None) -> str:
        """
        Send the initiation message as a new email via the Gmail API.

        :param target: Recipient email address.
        :param message: The agent-composed outbound text (the email body).
        :param target_details: Unused here; available for platform extras.
        :return: The new email's Gmail thread id — resolve_session_id() maps inbound
                 replies by thread id, so this is what InitiationManager.complete()
                 must bind.
        """
        mime_message = MIMEText(message)
        mime_message["to"] = target
        mime_message["subject"] = "Message from your agent"
        raw_message = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("utf-8")
        result = self._service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        return result["threadId"]


def register_initiation_dispatcher(handler: GmailInitiationHandler) -> None:
    """
    Gmail has no RESTAPI.run() to auto-detect InitiationSender handlers (it's a
    polling process, not a REST server), so wire the dispatcher manually — the same
    send-then-bind contract RESTAPI._register_initiation_sender() applies for REST
    deployments.

    :param handler: The GmailInitiationHandler instance to dispatch through.
    """

    def _dispatch(initiation: InitiationMessage) -> None:
        messaging_integration_thread_id = handler.send_initiation_message(
            initiation.target, initiation.message, initiation.target_details
        )
        manager = InitiationManager.get()
        if manager is not None:
            manager.complete(initiation, messaging_integration_thread_id)

    InitiationManager.register_dispatcher(_dispatch)


async def main():

    handler = GmailInitiationHandler()

    # Authenticate with Gmail
    handler.authenticate()
    register_initiation_dispatcher(handler)

    logging.info("Gmail bot started! Polling for new emails...")

    try:
        # Start polling loop
        await handler.start_polling()
    except KeyboardInterrupt:
        logging.info("Stopping Gmail bot...")
        handler.stop_polling()


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
            asyncio.run(main())
        else:
            loop.run_until_complete(main())

    except RuntimeError:
        asyncio.run(main())
