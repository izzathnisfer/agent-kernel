"""
Tests for the gmail-initiation example.

Gmail is a polling process with no HTTP surface (see the plain gmail
example's own server_test.py), so there's no subprocess/health check to run
here either. This suite covers the two halves that are verifiable locally:

- The outbound half: GmailInitiationHandler.send_initiation_message against
  a fake Gmail API service double (no real Gmail API call).
- The manual dispatcher wiring: register_initiation_dispatcher() sends via
  the handler and then binds the mapping through InitiationManager.complete().
"""

import base64
import os

import pytest

os.environ["AK_GMAIL__CLIENT_ID"] = "test-client-id"
os.environ["AK_GMAIL__CLIENT_SECRET"] = "test-client-secret"

from agentkernel.core.initiation import InitiationManager, InitiationMessage  # noqa: E402
from agentkernel.core.initiation.mapping.in_memory import InMemorySessionIdMappingStore  # noqa: E402

from server import GmailInitiationHandler, register_initiation_dispatcher  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state():
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()
    yield
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()


class FakeGmailUsersMessages:
    def __init__(self):
        self.sent = []

    def send(self, userId, body):
        self.sent.append(body)

        class _Exec:
            def execute(_self):
                return {"id": "msg-1", "threadId": "thread-999"}

        return _Exec()


class FakeGmailUsers:
    def __init__(self):
        self.messages_client = FakeGmailUsersMessages()

    def messages(self):
        return self.messages_client


class FakeGmailService:
    def __init__(self):
        self.users_client = FakeGmailUsers()

    def users(self):
        return self.users_client


def test_send_initiation_message_returns_thread_id():
    print("test_send_initiation_message_returns_thread_id")
    handler = GmailInitiationHandler()
    handler._service = FakeGmailService()

    thread_id = handler.send_initiation_message("monroe@example.com", "The report is ready")

    assert thread_id == "thread-999"
    sent = handler._service.users_client.messages_client.sent
    assert len(sent) == 1
    raw = base64.urlsafe_b64decode(sent[0]["raw"]).decode("utf-8")
    assert "monroe@example.com" in raw
    assert "The report is ready" in raw


def test_register_initiation_dispatcher_sends_and_binds():
    print("test_register_initiation_dispatcher_sends_and_binds")
    handler = GmailInitiationHandler()
    handler._service = FakeGmailService()
    register_initiation_dispatcher(handler)

    initiation = InitiationMessage(
        session_id="session-1",
        message="The report is ready",
        target="monroe@example.com",
        user_id="monroe@example.com",
        request_id="req-1",
    )
    InitiationManager.get().dispatch(initiation)

    manager = InitiationManager.get()
    assert manager.resolve_session_id("thread-999") == "session-1"
    assert manager.get_messaging_integration_thread_id("session-1") == "thread-999"
