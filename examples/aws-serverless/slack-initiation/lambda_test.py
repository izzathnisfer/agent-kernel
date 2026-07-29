"""
Local unit tests for the slack-initiation serverless example.

Following examples/api/slack-initiation/server_test.py's pattern (signed
requests, fake platform clients) rather than scalable-openai/lambda_test.py's
pattern (live integration test against a deployed endpoint) — these tests
exercise the handler functions directly, with fakes for SQS and Slack, so they
run locally without any deployed infrastructure.
"""

import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace

import pytest

SIGNING_SECRET = "test-signing-secret"
os.environ["SLACK_SIGNING_SECRET"] = SIGNING_SECRET
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"

from agentkernel.core.initiation import InitiationManager  # noqa: E402
from agentkernel.core.session.in_memory import InMemoryMappingStore  # noqa: E402

# Import the example modules once, at collection time, before any test's monkeypatched
# AKConfig is in effect. agentkernel.aws (imported transitively by lambda_agent_runner
# and lambda_response_handler) eagerly reads execution.queues.*.max_receive_count /
# no_of_consumers at class-definition time (ECSAgentRunner/ECSOutputConsumer are
# imported alongside the serverless classes via agentkernel.deployment.aws's wildcard
# import) — the real AKConfig has Pydantic defaults for these, but the tests' FakeCfg
# below does not, since this example never touches ECS.
import lambda_agent_runner  # noqa: E402,F401
import lambda_request_handler  # noqa: E402,F401
import lambda_response_handler  # noqa: E402,F401


def slack_headers(body: bytes) -> dict:
    """Signs a request body with the app's signing secret, as Slack would."""
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature}


def lambda_event(payload: dict) -> dict:
    body = json.dumps(payload).encode()
    return {
        "headers": slack_headers(body),
        "body": body.decode(),
        "isBase64Encoded": False,
    }


def message_event(
    *,
    ts: str,
    thread_ts: str = None,
    text: str = "hello",
    user: str = "U777",
    channel: str = "C1",
    bot_id: str = None,
) -> dict:
    event = {
        "type": "message",
        "text": text,
        "user": user,
        "channel": channel,
        "ts": ts,
        "client_msg_id": f"msg-{ts}",
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if bot_id is not None:
        event["bot_id"] = bot_id
    return {"type": "event_callback", "event_id": f"ev-{ts}", "event": event}


def make_fake_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class slack:
            agent = "general"

        class execution:
            mode = "rest_async"

            class queues:
                class input:
                    url = "https://sqs.test/input"

                class output:
                    url = "https://sqs.test/output"

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    FakeCfg.thread = None
    return FakeCfg


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg()))
    InitiationManager.reset()
    InMemoryMappingStore().clear()
    yield
    InitiationManager.reset()
    InMemoryMappingStore().clear()


class TestSlackEventsHandler:
    """lambda_request_handler.handle_slack_events"""

    def test_url_verification_returns_challenge(self):
        from lambda_request_handler import handle_slack_events

        status, body = handle_slack_events(lambda_event({"type": "url_verification", "challenge": "abc123"}), None)
        assert status == 200
        assert body == {"challenge": "abc123"}

    def test_invalid_signature_rejected(self):
        from lambda_request_handler import handle_slack_events

        event = lambda_event({"type": "url_verification", "challenge": "abc123"})
        event["headers"]["X-Slack-Signature"] = "v0=deadbeef"
        status, body = handle_slack_events(event, None)
        assert status == 401

    def test_bot_message_skipped(self, monkeypatch):
        from lambda_request_handler import handle_slack_events

        calls = []
        monkeypatch.setattr(
            "agentkernel.aws.SQSHandler.send_message_to_input_queue",
            lambda **kwargs: calls.append(kwargs),
        )

        status, body = handle_slack_events(lambda_event(message_event(ts="1.1", bot_id="B1")), None)
        assert status == 200
        assert calls == []

    def test_empty_text_skipped(self, monkeypatch):
        from lambda_request_handler import handle_slack_events

        calls = []
        monkeypatch.setattr(
            "agentkernel.aws.SQSHandler.send_message_to_input_queue",
            lambda **kwargs: calls.append(kwargs),
        )

        status, body = handle_slack_events(lambda_event(message_event(ts="1.1", text="   ")), None)
        assert status == 200
        assert calls == []

    def test_threaded_reply_resolves_to_mapped_session(self, monkeypatch):
        from lambda_request_handler import handle_slack_events

        InitiationManager.get()._store.save("session-1", "1111.2222")
        calls = []
        monkeypatch.setattr(
            "agentkernel.aws.SQSHandler.send_message_to_input_queue",
            lambda **kwargs: calls.append(kwargs),
        )

        status, _ = handle_slack_events(lambda_event(message_event(ts="5555.6666", thread_ts="1111.2222")), None)

        assert status == 200
        assert len(calls) == 1
        assert calls[0]["message_body"]["session_id"] == "session-1"

    def test_unthreaded_reply_uses_own_ts(self, monkeypatch):
        from lambda_request_handler import handle_slack_events

        InitiationManager.get()._store.save("session-1", "1111.2222")
        calls = []
        monkeypatch.setattr(
            "agentkernel.aws.SQSHandler.send_message_to_input_queue",
            lambda **kwargs: calls.append(kwargs),
        )

        status, _ = handle_slack_events(lambda_event(message_event(ts="3333.4444")), None)

        assert status == 200
        assert calls[0]["message_body"]["session_id"] == "3333.4444"

    def test_channel_and_thread_ts_attached_as_custom_attributes(self, monkeypatch):
        from lambda_request_handler import handle_slack_events

        calls = []
        monkeypatch.setattr(
            "agentkernel.aws.SQSHandler.send_message_to_input_queue",
            lambda **kwargs: calls.append(kwargs),
        )

        handle_slack_events(lambda_event(message_event(ts="7.7", channel="C42")), None)

        attrs = {a.name: a.value for a in calls[0]["custom_message_attributes"]}
        assert attrs["channel"] == "C42"
        assert attrs["thread_ts"] == "7.7"

    def test_slack_event_rides_in_body_for_requester_identification(self, monkeypatch):
        """The raw Slack event must be enqueued as an extra 'body' field —
        ChatService turns it into AgentRequestAny(name='body'), which is the
        only way get_requester_id can learn the sender in the queue flow
        (without it, the agent reports the requester as unidentified)."""
        from agentkernel.core import AgentRequestAny, ToolContext
        from agentkernel.core.chat_service import RequestBuilder
        from agentkernel.core.model import BaseRunRequest

        from lambda_agent_runner import get_requester_id
        from lambda_request_handler import handle_slack_events

        calls = []
        monkeypatch.setattr(
            "agentkernel.aws.SQSHandler.send_message_to_input_queue",
            lambda **kwargs: calls.append(kwargs),
        )

        handle_slack_events(lambda_event(message_event(ts="7.7", user="U0REQUESTER")), None)

        assert calls[0]["message_body"]["body"]["user"] == "U0REQUESTER"

        # End to end: the enqueued body, parsed the way the agent runner parses it,
        # must yield the AgentRequestAny that get_requester_id reads from ToolContext.
        req = BaseRunRequest.model_validate(calls[0]["message_body"])
        requests = RequestBuilder.from_base_request_sync(req)
        assert any(isinstance(r, AgentRequestAny) and r.name == "body" for r in requests)

        context = ToolContext(runtime=None, agent=None, session=None, requests=requests)
        context.set()
        try:
            assert get_requester_id() == "U0REQUESTER"
        finally:
            context.reset()


class TestAgentRunnerAttributePassthrough:
    """lambda_agent_runner.AgentRunner — channel/thread_ts survive input -> output."""

    def test_get_record_attributes_reads_channel_and_thread_ts(self):
        from lambda_agent_runner import AgentRunner

        record = {
            "messageAttributes": {
                "request_id": {"stringValue": "req-1"},
                "channel": {"stringValue": "C42"},
                "thread_ts": {"stringValue": "1.1"},
            },
            "attributes": {"MessageGroupId": "session-1"},
        }

        attrs = AgentRunner._get_record_attributes(record)

        assert attrs["channel"] == "C42"
        assert attrs["thread_ts"] == "1.1"

    def test_send_to_output_queue_forwards_channel_and_thread_ts(self, monkeypatch):
        from lambda_agent_runner import AgentRunner

        calls = []
        monkeypatch.setattr(
            "agentkernel.aws.SQSHandler.send_message_to_output_queue",
            lambda **kwargs: calls.append(kwargs),
        )

        AgentRunner._send_to_output_queue(
            message_body={"result": "ok", "session_id": "session-1"},
            record_attributes={
                "message_group_id": "session-1",
                "message_deduplication_id": "req-1",
                "request_id": "req-1",
                "user_id": "U1",
                "channel": "C42",
                "thread_ts": "1.1",
            },
        )

        assert len(calls) == 1
        assert calls[0]["request_id"] == "req-1"
        assert calls[0]["user_id"] == "U1"
        attrs = {a.name: a.value for a in calls[0]["custom_message_attributes"]}
        assert attrs == {"channel": "C42", "thread_ts": "1.1"}
        # request_id/user_id must not also appear in custom_message_attributes
        # (SQSHandler rejects duplicate attribute names).
        assert "request_id" not in attrs
        assert "user_id" not in attrs


class FakeSlackClient:
    def __init__(self):
        self.opened = []
        self.posted = []

    async def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": "D999"}}

    async def chat_postMessage(self, channel, text, thread_ts=None):
        self.posted.append((channel, text, thread_ts))
        return {"ts": "9999.0000"}


def output_record(
    *,
    body: dict,
    message_type: str = None,
    channel: str = None,
    thread_ts: str = None,
    request_id: str = "req-1",
) -> dict:
    attrs = {"request_id": {"stringValue": request_id}}
    if message_type is not None:
        attrs["message_type"] = {"stringValue": message_type}
    if channel is not None:
        attrs["channel"] = {"stringValue": channel}
    if thread_ts is not None:
        attrs["thread_ts"] = {"stringValue": thread_ts}
    return {"body": json.dumps(body), "messageAttributes": attrs}


class TestSlackResponseHandler:
    """lambda_response_handler.SlackResponseHandler.process_message"""

    def test_initiation_message_delivers_and_binds_mapping(self, monkeypatch):
        import lambda_response_handler as mod

        fake_client = FakeSlackClient()
        monkeypatch.setattr(mod, "_slack_client", fake_client)

        initiation_body = {
            "session_id": "session-new",
            "message": "Hi! Just a heads up.",
            "target": "U0MONROE",
            "user_id": "U0MONROE",
            "request_id": "req-init",
            "type": "initiation",
        }

        mod.SlackResponseHandler.process_message(output_record(body=initiation_body, message_type="INITIATION"))

        assert fake_client.opened == ["U0MONROE"]
        assert fake_client.posted == [("D999", "Hi! Just a heads up.", None)]
        assert InitiationManager.get().get_messaging_integration_thread_id("session-new") == "9999.0000"

    def test_ordinary_reply_delivers_to_channel_and_thread(self, monkeypatch):
        import lambda_response_handler as mod

        fake_client = FakeSlackClient()
        monkeypatch.setattr(mod, "_slack_client", fake_client)
        monkeypatch.setattr(
            mod.SlackResponseHandler,
            "_get_response_store",
            classmethod(lambda cls: FakeStore()),
        )

        body = {"result": "Traffic.", "session_id": "session-1"}
        mod.SlackResponseHandler.process_message(output_record(body=body, channel="C1", thread_ts="1.1"))

        assert fake_client.posted == [("C1", "Traffic.", "1.1")]

    def test_missing_channel_skips_delivery(self, monkeypatch):
        import lambda_response_handler as mod

        fake_client = FakeSlackClient()
        monkeypatch.setattr(mod, "_slack_client", fake_client)
        monkeypatch.setattr(
            mod.SlackResponseHandler,
            "_get_response_store",
            classmethod(lambda cls: FakeStore()),
        )

        body = {"result": "Traffic.", "session_id": "session-1"}
        mod.SlackResponseHandler.process_message(output_record(body=body))

        assert fake_client.posted == []


class FakeStore:
    def add_message(self, message):
        pass
