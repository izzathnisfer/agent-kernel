"""
Tests for the telegram-initiation example.

Telegram itself cannot call back into a test run, so the suite covers the two
halves that are verifiable locally:

- The server starts and serves /health with the handler constructed under a
  fake (non-real) bot token.
- The outbound half: TelegramInitiationHandler.send_initiation_message
  against a monkeypatched _send_message (no real Bot API call).
"""

import asyncio
import os
import subprocess
import sys

import httpx
import pytest
import pytest_asyncio

# Fixed fake credential so the handler constructs without hitting the real
# Telegram Bot API. Set before server.py is imported/spawned; the subprocess
# inherits it.
os.environ["AK_TELEGRAM__BOT_TOKEN"] = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def http_client():
    proc = subprocess.Popen(
        ["python3", "server.py"],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    await asyncio.sleep(5)
    try:
        yield "http://localhost:8000"
    finally:
        proc.terminate()
        proc.wait()


@pytest.mark.asyncio(loop_scope="session")
async def test_health(http_client):
    print("test_health")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{http_client}/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_send_initiation_message_returns_target_and_sends(monkeypatch):
    print("test_send_initiation_message_returns_target_and_sends")
    from server import TelegramInitiationHandler

    handler = TelegramInitiationHandler()
    sent = []

    async def fake_send_message(chat_id, text, parse_mode=None, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(handler, "_send_message", fake_send_message)

    thread_id = handler.send_initiation_message("555555", "Your laptop is ready")

    assert sent == [(555555, "Your laptop is ready")]  # cast to int for the Bot API call
    # No per-message thread concept on Telegram — the chat id itself is the
    # identifier resolve_session_id() maps inbound replies through.
    assert thread_id == "555555"
