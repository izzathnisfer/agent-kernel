import json
from unittest.mock import MagicMock

import pytest

from agentkernel.core.initiation import INITIATION_MESSAGE_TYPE, InitiationManager, InitiationMessage
from agentkernel.core.initiation.mapping.in_memory import InMemorySessionIdMappingStore
from agentkernel.core.model import ExecutionMode
from agentkernel.core.thread.manager import ConversationThreadManager
from agentkernel.core.thread.naming import ThreadNamingStrategy
from agentkernel.core.thread.store.in_memory import InMemoryThreadStore
from agentkernel.deployment.aws.containerized.akoutputconsumer import ECSOutputConsumer
from agentkernel.deployment.aws.core.initiation_dispatch import InitiationQueueDispatcher
from agentkernel.deployment.aws.core.sqs_handler import SQSHandler
from agentkernel.deployment.aws.serverless.akresponsehandler import ResponseHandler


class FakeThreadCfg:
    type = "memory"


def make_fake_cfg(conversation_initiation_enabled=True, thread=None, mode=ExecutionMode.REST_SYNC):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class execution:
            class queues:
                class output:
                    max_receive_count = 3

        class conversation_initiation:
            enabled = conversation_initiation_enabled
            store = None

    FakeCfg.execution.mode = mode
    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.thread = thread
    return FakeCfg


class StubNaming(ThreadNamingStrategy):
    def generate_name(self, first_prompt: str) -> str:
        return "stub-name"


def make_initiation(session_id="session-1"):
    return InitiationMessage(
        session_id=session_id,
        message="Hi Monroe, your laptop is ready",
        target="U123",
        user_id="monroe",
        request_id="req-1",
    )


def lambda_record(body: dict, message_type: str = None, request_id: str = "req-1") -> dict:
    attrs = {"request_id": {"stringValue": request_id}}
    if message_type:
        attrs["message_type"] = {"stringValue": message_type}
    return {"body": json.dumps(body), "messageAttributes": attrs}


def boto3_record(body: dict, message_type: str = None, request_id: str = "req-1") -> dict:
    attrs = {"request_id": {"StringValue": request_id}}
    if message_type:
        attrs["message_type"] = {"StringValue": message_type}
    return {"MessageId": "m-1", "Body": json.dumps(body), "MessageAttributes": attrs}


@pytest.fixture(autouse=True)
def reset_state():
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()
    ConversationThreadManager.reset()
    InMemoryThreadStore._threads.clear()
    InMemoryThreadStore._messages.clear()
    ResponseHandler._response_store = None
    ResponseHandler._base_ws_handler = None
    ECSOutputConsumer._response_store = None
    yield
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()
    ConversationThreadManager.reset()
    InMemoryThreadStore._threads.clear()
    InMemoryThreadStore._messages.clear()
    ResponseHandler._response_store = None
    ResponseHandler._base_ws_handler = None
    ECSOutputConsumer._response_store = None


@pytest.fixture
def enabled_cfg(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg()))


class TestServerlessStockGuard:
    def test_initiation_message_is_dropped_not_stored(self, enabled_cfg):
        store = MagicMock()
        ResponseHandler._response_store = store
        ResponseHandler._base_ws_handler = MagicMock()

        ResponseHandler.process_message(lambda_record(make_initiation().model_dump(), message_type=INITIATION_MESSAGE_TYPE))

        store.add_message.assert_not_called()
        ResponseHandler._base_ws_handler.broadcast_message.assert_not_called()

    def test_ordinary_message_still_stored(self, enabled_cfg):
        store = MagicMock()
        ResponseHandler._response_store = store

        ResponseHandler.process_message(lambda_record({"result": "hi", "session_id": "s-1"}))

        stored = store.add_message.call_args.args[0]
        assert stored == {"session_id": "s-1", "request_id": "req-1", "body": {"result": "hi", "session_id": "s-1"}}

    def test_permanent_failure_of_initiation_logs_only(self, enabled_cfg):
        store = MagicMock()
        ResponseHandler._response_store = store

        ResponseHandler.on_permanent_failure(lambda_record(make_initiation().model_dump(), message_type=INITIATION_MESSAGE_TYPE))

        store.add_message.assert_not_called()


class TestECSStockGuard:
    def test_initiation_message_is_dropped_not_stored(self, enabled_cfg):
        store = MagicMock()
        ECSOutputConsumer._response_store = store

        ECSOutputConsumer.process_message(boto3_record(make_initiation().model_dump(), message_type=INITIATION_MESSAGE_TYPE))

        store.add_message.assert_not_called()

    def test_ordinary_message_still_stored(self, enabled_cfg):
        store = MagicMock()
        ECSOutputConsumer._response_store = store

        ECSOutputConsumer.process_message(boto3_record({"result": "hi", "session_id": "s-1"}))

        stored = store.add_message.call_args.args[0]
        assert stored["session_id"] == "s-1"
        assert stored["request_id"] == "req-1"

    def test_permanent_failure_of_initiation_logs_only(self, enabled_cfg):
        store = MagicMock()
        ECSOutputConsumer._response_store = store

        ECSOutputConsumer.on_permanent_failure(boto3_record(make_initiation().model_dump(), message_type=INITIATION_MESSAGE_TYPE))

        store.add_message.assert_not_called()


class DeliveringECSOutputConsumer(ECSOutputConsumer):
    """The documented user contract: parse, send, complete()."""

    sent: list = []

    @classmethod
    def process_message(cls, record):
        attributes = SQSHandler.get_message_custom_attributes(record)
        if attributes.get("message_type") == INITIATION_MESSAGE_TYPE:
            initiation = InitiationMessage.model_validate_json(record["Body"])
            messaging_integration_thread_id = cls.send_to_platform(initiation)
            InitiationManager.get().complete(initiation, messaging_integration_thread_id)
            return
        super().process_message(record)

    @classmethod
    def send_to_platform(cls, initiation) -> str:
        cls.sent.append(initiation)
        return "thread-99"


class TestSubclassContract:
    @pytest.fixture
    def thread_enabled_cfg(self, monkeypatch):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get",
            classmethod(lambda cls: make_fake_cfg(thread=FakeThreadCfg)),
        )
        ConversationThreadManager.set_naming_strategy(StubNaming())
        DeliveringECSOutputConsumer.sent = []

    def test_override_sends_binds_and_initializes_thread(self, thread_enabled_cfg):
        initiation = make_initiation()

        DeliveringECSOutputConsumer.process_message(boto3_record(initiation.model_dump(), message_type=INITIATION_MESSAGE_TYPE))

        # Sent exactly once via the override
        assert [i.session_id for i in DeliveringECSOutputConsumer.sent] == ["session-1"]
        # Mapping bound in both directions
        manager = InitiationManager.get()
        assert manager.resolve_session_id("thread-99") == "session-1"
        assert manager.get_messaging_integration_thread_id("session-1") == "thread-99"
        # AK thread created for the recipient and seeded with the outbound message
        thread_manager = ConversationThreadManager.get()
        thread = thread_manager._store.load_metadata("session-1")
        assert thread.user_id == "monroe"
        assert thread.name == "stub-name"
        assert thread.group_id is None
        messages, _ = thread_manager._store.get_messages("session-1", limit=10)
        assert [(m.role, m.content) for m in messages] == [("assistant", "Hi Monroe, your laptop is ready")]

    def test_complete_swallows_mapping_store_failure(self, thread_enabled_cfg):
        manager = InitiationManager.get()
        manager._store = MagicMock()
        manager._store.get_session_id.side_effect = RuntimeError("backend down")

        # Must not raise — raising would redeliver the SQS message and resend to the user
        DeliveringECSOutputConsumer.process_message(boto3_record(make_initiation().model_dump(), message_type=INITIATION_MESSAGE_TYPE))

        assert len(DeliveringECSOutputConsumer.sent) == 1


class TestQueueDispatcher:
    def test_dispatch_enqueues_to_output_queue_with_initiation_attribute(self, enabled_cfg, monkeypatch):
        sent = {}

        def fake_send(**kwargs):
            sent.update(kwargs)

        monkeypatch.setattr(SQSHandler, "send_message_to_output_queue", staticmethod(fake_send))
        InitiationQueueDispatcher.register()

        initiation = make_initiation()
        InitiationManager.get().dispatch(initiation)

        assert sent["message_body"]["session_id"] == "session-1"
        assert sent["attributes"] == {"message_group_id": "session-1", "message_deduplication_id": "req-1"}
        assert sent["request_id"] == "req-1"
        assert sent["user_id"] == "monroe"
        custom = sent["custom_message_attributes"]
        assert len(custom) == 1
        assert custom[0].name == "message_type"
        assert custom[0].value == INITIATION_MESSAGE_TYPE
