"""
Tests for the slack-initiation example.

Slack itself cannot call back into a test run, so the suite covers the two
halves that are verifiable locally:

- The events endpoint, with requests signed like Slack signs them: the
  url_verification challenge Slack sends when the Request URL is registered
  must be echoed back, and unsigned requests must be rejected.
- The outbound half: SlackInitiationHandler.send_initiation_message against a
  fake Slack client (member ids open a DM; channel posts return the thread ts).
"""

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

# Fixed credentials so the tests can sign requests the way Slack does. Set
# before server.py is imported/spawned; the subprocess inherits them.
SIGNING_SECRET = "test-signing-secret"
os.environ["SLACK_SIGNING_SECRET"] = SIGNING_SECRET
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"


def slack_headers(body: bytes) -> dict:
    """Signs a request body with the app's signing secret, as Slack would."""
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }


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


@pytest.mark.asyncio(loop_scope="session")  # all async tests share the server fixture's loop
async def test_health(http_client):
    print("test_health")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{http_client}/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio(loop_scope="session")
async def test_url_verification_challenge(http_client):
    print("test_url_verification_challenge")
    challenge = "A3iJatJqh40dIMltX7VbAYVooc4M8vCEUJH5BGpKPSQUwl3WhYnX"
    body = json.dumps({"type": "url_verification", "challenge": challenge}).encode()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{http_client}/slack/events", content=body, headers=slack_headers(body))
    assert resp.status_code == 200
    assert resp.json()["challenge"] == challenge


@pytest.mark.asyncio(loop_scope="session")
async def test_unsigned_event_is_rejected(http_client):
    print("test_unsigned_event_is_rejected")
    body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{http_client}/slack/events", content=body, headers={"content-type": "application/json"}
        )
    assert resp.status_code == 401


class FakeSlackClient:
    """Records outbound Slack calls instead of hitting the Slack API."""

    def __init__(self):
        self.opened = []
        self.posted = []

    async def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": "D999"}}

    async def chat_postMessage(self, channel, text):
        self.posted.append((channel, text))
        return {"ts": "1111.2222"}


@pytest.fixture
def initiation_handler():
    from server import SlackInitiationHandler  # imported here so the env above is set first

    handler = SlackInitiationHandler()
    fake = FakeSlackClient()
    handler._slack_app = SimpleNamespace(client=fake)
    return handler, fake


def test_send_initiation_message_member_id_opens_dm(initiation_handler):
    print("test_send_initiation_message_member_id_opens_dm")
    handler, fake = initiation_handler
    thread_id = handler.send_initiation_message("U123", "Your laptop is ready")
    assert fake.opened == ["U123"]
    assert fake.posted == [("D999", "Your laptop is ready")]
    assert thread_id == "D999"  # DM replies arrive keyed by the DM channel id


def test_send_initiation_message_channel_returns_thread_root(initiation_handler):
    print("test_send_initiation_message_channel_returns_thread_root")
    handler, fake = initiation_handler
    thread_id = handler.send_initiation_message("C42", "Deploy finished")
    assert fake.opened == []
    assert fake.posted == [("C42", "Deploy finished")]
    assert thread_id == "1111.2222"  # channel replies arrive threaded under the posted ts
