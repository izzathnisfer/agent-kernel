import asyncio
import base64
import logging
import os
import threading

from agentkernel.api import RESTAPI
from agentkernel.openai import OpenAIModule
from agentkernel.slack import AgentSlackRequestHandler
from agentkernel.telegram import AgentTelegramRequestHandler
from agents import Agent as OpenAIAgent

general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general questions",
    instructions="You are an integration test agent. Reply to every message with a short, one-sentence answer.",
    model="openai/gpt-4.1-mini",
)

OpenAIModule([general_agent])

_log = logging.getLogger("ak.e2e")


def _maybe_start_gmail():
    """Start the Gmail polling handler in a background thread, if configured.

    Gmail's OAuth flow is interactive, so the container cannot authenticate from
    scratch: a pre-generated token.pickle is injected base64-encoded via
    AK_GMAIL__TOKEN_B64 (see e2e/tests/scripts/gmail_login.py) and written to the
    configured token file before the handler starts. When the Gmail env vars are
    absent the app simply runs without the Gmail integration.
    """
    token_b64 = os.environ.get("AK_GMAIL__TOKEN_B64")
    if not (os.environ.get("AK_GMAIL__CLIENT_ID") and os.environ.get("AK_GMAIL__CLIENT_SECRET") and token_b64):
        _log.info("Gmail credentials not configured - Gmail integration disabled")
        return

    from agentkernel.core import Config
    from agentkernel.gmail import AgentGmailRequestHandler

    token_file = Config.get().gmail.token_file
    with open(token_file, "wb") as f:
        f.write(base64.b64decode(token_b64))

    handler = AgentGmailRequestHandler()
    handler.authenticate()

    def _run():
        asyncio.run(handler.start_polling())

    threading.Thread(target=_run, name="gmail-polling", daemon=True).start()
    _log.info("Gmail polling started in background thread")


def _handlers():
    handlers = [AgentSlackRequestHandler(), AgentTelegramRequestHandler()]
    if os.environ.get("AK_WHATSAPP__ACCESS_TOKEN"):
        from agentkernel.whatsapp import AgentWhatsAppRequestHandler

        handlers.append(AgentWhatsAppRequestHandler())
    else:
        _log.info("WhatsApp credentials not configured - WhatsApp integration disabled")
    return handlers


def main():
    _maybe_start_gmail()
    RESTAPI.run(_handlers())


if __name__ == "__main__":
    main()
