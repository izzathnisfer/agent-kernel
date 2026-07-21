"""
Local unit tests for the slack-initiation containerized example.

Following examples/api/slack-initiation/server_test.py's pattern (fake Slack
client, no deployed infrastructure) rather than
openai-dynamodb-scalable/app_test.py's pattern (live integration test against
a deployed endpoint).
"""

import json
import os
from types import SimpleNamespace

import pytest

os.environ["SLACK_SIGNING_SECRET"] = "test-signing-secret"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"

from agentkernel.core.initiation import InitiationManager  # noqa: E402
from agentkernel.core.initiation.mapping.in_memory import InMemorySessionIdMappingStore  # noqa: E402

# Import the example modules once, at collection time, before any test's monkeypatched
# AKConfig is in effect — see lambda_test.py's identical note for why this matters
# (agentkernel.aws eagerly reads execution.queues.*.max_receive_count/no_of_consumers
# at class-definition time; the tests' FakeCfg below does not define those, since this
# example never uses the generic queue-mode chat endpoint).
import app_agent_runner  # noqa: E402,F401
import slack_output_consumer  # noqa: E402,F401
import slack_request_handler  # noqa: E402,F401


class FakeMappingTableCfg:
    table_name = "test-mapping"
    collection_name = "test-mapping"
    prefix = "ak:test-map:"
    ttl = 0


def make_fake_cfg(mapping_table=FakeMappingTableCfg):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class slack:
            agent = "general"

        class api:
            max_file_size = 10 * 1024 * 1024

        class execution:
            mode = "rest_async"

            class queues:
                class input:
                    url = "https://sqs.test/input"

                class output:
                    url = "https://sqs.test/output"

    FakeCfg.mapping_table = mapping_table
    FakeCfg.thread = None
    return FakeCfg


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg()))
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()
    yield
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()


class FakeAuthClient:
    def __init__(self, bot_id="BOTID"):
        self._bot_id = bot_id

    async def auth_test(self):
        return {"user_id": self._bot_id}


def slack_message(
    *, ts: str, thread_ts: str = None, text: str = "hello", user: str = "U777", channel: str = "C1"
) -> dict:
    body = {"user": user, "text": text, "channel": channel, "ts": ts}
    if thread_ts is not None:
        body["thread_ts"] = thread_ts
    return body


@pytest.fixture
def handler():
    from slack_request_handler import SlackECSRequestHandler

    h = SlackECSRequestHandler()
    h._slack_app = SimpleNamespace(client=FakeAuthClient())
    return h


class TestSlackECSRequestHandler:
    @pytest.mark.asyncio
    async def test_bot_message_skipped(self, handler, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "slack_request_handler.SQSHandler.send_message_to_input_queue", lambda **kwargs: calls.append(kwargs)
        )

        await handler.handle(slack_message(ts="1.1", user="BOTID"), say=None)

        assert calls == []

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self, handler, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "slack_request_handler.SQSHandler.send_message_to_input_queue", lambda **kwargs: calls.append(kwargs)
        )

        await handler.handle(slack_message(ts="1.1", text="   "), say=None)

        assert calls == []

    @pytest.mark.asyncio
    async def test_threaded_reply_resolves_to_mapped_session(self, handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "1111.2222")
        calls = []
        monkeypatch.setattr(
            "slack_request_handler.SQSHandler.send_message_to_input_queue", lambda **kwargs: calls.append(kwargs)
        )

        await handler.handle(slack_message(ts="5555.6666", thread_ts="1111.2222"), say=None)

        assert len(calls) == 1
        assert calls[0]["message_body"]["session_id"] == "session-1"

    @pytest.mark.asyncio
    async def test_unthreaded_reply_uses_own_ts(self, handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "1111.2222")
        calls = []
        monkeypatch.setattr(
            "slack_request_handler.SQSHandler.send_message_to_input_queue", lambda **kwargs: calls.append(kwargs)
        )

        await handler.handle(slack_message(ts="3333.4444"), say=None)

        assert calls[0]["message_body"]["session_id"] == "3333.4444"

    @pytest.mark.asyncio
    async def test_channel_and_thread_ts_attached_as_custom_attributes(self, handler, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "slack_request_handler.SQSHandler.send_message_to_input_queue", lambda **kwargs: calls.append(kwargs)
        )

        await handler.handle(slack_message(ts="7.7", channel="C42"), say=None)

        attrs = {a.name: a.value for a in calls[0]["custom_message_attributes"]}
        assert attrs["channel"] == "C42"
        assert attrs["thread_ts"] == "7.7"

    @pytest.mark.asyncio
    async def test_slack_event_rides_in_body_for_requester_identification(self, handler, monkeypatch):
        """The raw Slack event must be enqueued as an extra 'body' field —
        ChatService turns it into AgentRequestAny(name='body'), which is the
        only way get_requester_id can learn the sender in the queue flow
        (without it, the agent reports the requester as unidentified)."""
        from agentkernel.core import AgentRequestAny, ToolContext
        from agentkernel.core.chat_service import RequestBuilder
        from agentkernel.core.model import BaseRunRequest

        from app_agent_runner import get_requester_id

        calls = []
        monkeypatch.setattr(
            "slack_request_handler.SQSHandler.send_message_to_input_queue", lambda **kwargs: calls.append(kwargs)
        )

        await handler.handle(slack_message(ts="7.7", user="U0REQUESTER"), say=None)

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
    """app_agent_runner.AgentRunner — channel/thread_ts survive input -> output (boto3 record shape)."""

    def test_get_record_attributes_reads_channel_and_thread_ts(self):
        from app_agent_runner import AgentRunner

        record = {
            "MessageAttributes": {
                "request_id": {"StringValue": "req-1"},
                "channel": {"StringValue": "C42"},
                "thread_ts": {"StringValue": "1.1"},
            },
            "Attributes": {"MessageGroupId": "session-1"},
        }

        attrs = AgentRunner._get_record_attributes(record)

        assert attrs["channel"] == "C42"
        assert attrs["thread_ts"] == "1.1"

    def test_send_to_output_queue_forwards_channel_and_thread_ts(self, monkeypatch):
        from app_agent_runner import AgentRunner

        calls = []
        monkeypatch.setattr(
            "app_agent_runner.SQSHandler.send_message_to_output_queue", lambda **kwargs: calls.append(kwargs)
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


class FakeStore:
    def add_message(self, message):
        pass


def output_record(
    *, body: dict, message_type: str = None, channel: str = None, thread_ts: str = None, request_id: str = "req-1"
) -> dict:
    attrs = {"request_id": {"StringValue": request_id}}
    if message_type is not None:
        attrs["message_type"] = {"StringValue": message_type}
    if channel is not None:
        attrs["channel"] = {"StringValue": channel}
    if thread_ts is not None:
        attrs["thread_ts"] = {"StringValue": thread_ts}
    return {"Body": json.dumps(body), "MessageAttributes": attrs}


class TestSlackECSOutputConsumer:
    """slack_output_consumer.SlackECSOutputConsumer.process_message"""

    def test_initiation_message_delivers_and_binds_mapping(self, monkeypatch):
        import slack_output_consumer as mod

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

        mod.SlackECSOutputConsumer.process_message(output_record(body=initiation_body, message_type="INITIATION"))

        assert fake_client.opened == ["U0MONROE"]
        assert fake_client.posted == [("D999", "Hi! Just a heads up.", None)]
        assert InitiationManager.get().get_messaging_integration_thread_id("session-new") == "9999.0000"

    def test_ordinary_reply_delivers_to_channel_and_thread(self, monkeypatch):
        import slack_output_consumer as mod

        fake_client = FakeSlackClient()
        monkeypatch.setattr(mod, "_slack_client", fake_client)
        monkeypatch.setattr(mod.SlackECSOutputConsumer, "_get_response_store", classmethod(lambda cls: FakeStore()))

        body = {"result": "Traffic.", "session_id": "session-1"}
        mod.SlackECSOutputConsumer.process_message(output_record(body=body, channel="C1", thread_ts="1.1"))

        assert fake_client.posted == [("C1", "Traffic.", "1.1")]

    def test_missing_channel_skips_delivery(self, monkeypatch):
        import slack_output_consumer as mod

        fake_client = FakeSlackClient()
        monkeypatch.setattr(mod, "_slack_client", fake_client)
        monkeypatch.setattr(mod.SlackECSOutputConsumer, "_get_response_store", classmethod(lambda cls: FakeStore()))

        body = {"result": "Traffic.", "session_id": "session-1"}
        mod.SlackECSOutputConsumer.process_message(output_record(body=body))

        assert fake_client.posted == []
