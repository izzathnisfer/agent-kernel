from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkernel.core.initiation import InitiationManager
from agentkernel.core.model import ExecutionMode
from agentkernel.core.session.in_memory import InMemoryMappingStore
from agentkernel.deployment.common.queue_request_handler import QueueRequestHandler


def make_fake_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class execution:
            mode = ExecutionMode.REST_ASYNC

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    return FakeCfg


class FakeQueueRequestHandler(QueueRequestHandler):
    def __init__(self):
        super().__init__(logger_name="ak.test.queue_handler")
        self.queue_handler = MagicMock()
        self.queue_handler.send_message_to_input_queue.return_value = {"MessageId": "m-1"}
        self.response_store = MagicMock()

    def get_queue_handler(self):
        return self.queue_handler

    def get_response_store(self):
        return self.response_store


@pytest.fixture(autouse=True)
def reset_state():
    InitiationManager.reset()
    InMemoryMappingStore().clear()
    yield
    InitiationManager.reset()
    InMemoryMappingStore().clear()


def make_client(handler) -> TestClient:
    app = FastAPI()
    app.include_router(handler.get_router())
    return TestClient(app)


@pytest.fixture
def enabled_cfg(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg()))


class TestQueueRequestHandlerResolve:
    def test_mapped_session_id_is_rewritten_and_returned(self, enabled_cfg):
        InitiationManager.get()._store.save("session-1", "thread-1")
        handler = FakeQueueRequestHandler()

        response = make_client(handler).post("/api/v1/chat", json={"session_id": "thread-1", "prompt": "hi"})

        assert response.status_code == 200
        assert response.json()["session_id"] == "session-1"
        send_kwargs = handler.queue_handler.send_message_to_input_queue.call_args.kwargs
        assert send_kwargs["message_body"]["session_id"] == "session-1"
        assert send_kwargs["attributes"]["message_group_id"] == "session-1"

    def test_unmapped_session_id_passes_through(self, enabled_cfg):
        handler = FakeQueueRequestHandler()

        response = make_client(handler).post("/api/v1/chat", json={"session_id": "thread-x", "prompt": "hi"})

        assert response.status_code == 200
        assert response.json()["session_id"] == "thread-x"

    def test_disabled_feature_passes_through(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg(conversation_initiation_enabled=False)))
        handler = FakeQueueRequestHandler()

        response = make_client(handler).post("/api/v1/chat", json={"session_id": "thread-1", "prompt": "hi"})

        assert response.status_code == 200
        assert response.json()["session_id"] == "thread-1"

    def test_user_override_of_resolve_is_honored(self, enabled_cfg):
        class CustomResolveHandler(FakeQueueRequestHandler):
            def resolve_session_id(self, messaging_integration_thread_id: str) -> str:
                return f"custom-{messaging_integration_thread_id}"

        handler = CustomResolveHandler()

        response = make_client(handler).post("/api/v1/chat", json={"session_id": "thread-1", "prompt": "hi"})

        assert response.json()["session_id"] == "custom-thread-1"
