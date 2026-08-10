"""
End-to-end test for local queue mode: a real HTTP request travels through
LocalQueueRequestHandler -> LocalQueueHandler -> LocalAgentRunner -> ChatService ->
LocalQueueHandler -> LocalOutputConsumer -> LocalResponseStore -> back to the HTTP
response, with nothing mocked. Fills the gap the design doc calls out: the existing
ECS queue-mode tests all mock boto3 directly, so nothing exercises this round trip.
"""

import socket
import uuid

import httpx
import pytest

from agentkernel.core.base import Agent, Runner
from agentkernel.core.config import AKConfig
from agentkernel.core.model import AgentReplyText, AgentRequestText, ExecutionMode
from agentkernel.core.runtime import GlobalRuntime
from testharness import LocalQueueMode
from testharness.core.queue_handler import LocalQueueHandler
from testharness.local_agent_runner import LocalAgentRunner
from testharness.local_output_consumer import LocalOutputConsumer


class _EchoRunner(Runner):
    async def run(self, agent, session, requests):
        prompt = requests[0].prompt if requests and isinstance(requests[0], AgentRequestText) else ""
        return AgentReplyText(response=f"echo:{prompt}")

    async def stream(self, agent, session, requests):
        raise NotImplementedError()
        yield


class _EchoAgent(Agent):
    def __init__(self, name):
        super().__init__(name, _EchoRunner("EchoRunner"))

    def get_description(self):
        return "test echo agent"

    def get_a2a_card(self):
        return None

    def override_system_prompt(self, prompt):
        pass

    def attach_tool(self, tool):
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def echo_agent():
    agent = _EchoAgent(f"echo-agent-{uuid.uuid4()}")
    GlobalRuntime.instance().register(agent)
    yield agent
    GlobalRuntime.instance().deregister(agent)


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    """Point the already-cached AKConfig singleton at a scratch SQLite file and free port.

    LocalAgentRunner/LocalOutputConsumer cache `_config = AKConfig.get()` once at import
    time (mirroring ECSAgentRunner/ECSOutputConsumer), so this fixture mutates that exact
    object in place rather than swapping in a freshly fetched AKConfig.get() — other test
    modules in this suite (test_config.py, test_sandbox.py, ...) call AKConfig._reset(),
    which would otherwise make a "fresh" AKConfig.get() here return a different object than
    the one already baked into LocalAgentRunner/LocalOutputConsumer, silently decoupling them.
    AKConfig.get is itself pinned to return this exact object for the same reason: every other
    local-mode class (LocalQueueRequestHandler, LocalQueueHandler) fetches it fresh.
    """
    config = LocalAgentRunner._config
    db_path = str(tmp_path / "local_queue_mode.db")
    port = _free_port()

    monkeypatch.setattr(config.execution, "mode", ExecutionMode.REST_SYNC)
    monkeypatch.setattr(config.execution.queues.input, "url", db_path)
    monkeypatch.setattr(config.execution.queues.output, "url", db_path)
    monkeypatch.setattr(config.api, "host", "127.0.0.1")
    monkeypatch.setattr(config.api, "port", port)

    monkeypatch.setattr(AKConfig, "get", classmethod(lambda cls: config))
    monkeypatch.setattr(LocalOutputConsumer, "_config", config)

    LocalQueueHandler._queue = None
    LocalQueueHandler._config = None
    LocalOutputConsumer._response_store = None
    yield config
    LocalQueueHandler._queue = None
    LocalQueueHandler._config = None
    LocalOutputConsumer._response_store = None


def _base_url(config) -> str:
    return f"http://{config.api.host}:{config.api.port}"


class TestRestSync:
    def test_round_trip_returns_agent_reply(self, local_config, echo_agent):
        with LocalQueueMode():
            response = httpx.post(
                f"{_base_url(local_config)}/api/v1/chat",
                json={"prompt": "hello there", "session_id": str(uuid.uuid4()), "agent": echo_agent.name},
                timeout=15,
            )

        assert response.status_code == 200
        assert response.json()["result"] == "echo:hello there"

    def test_two_sequential_requests_both_resolve(self, local_config, echo_agent):
        with LocalQueueMode():
            session_id = str(uuid.uuid4())
            first = httpx.post(
                f"{_base_url(local_config)}/api/v1/chat",
                json={"prompt": "one", "session_id": session_id, "agent": echo_agent.name},
                timeout=15,
            )
            second = httpx.post(
                f"{_base_url(local_config)}/api/v1/chat",
                json={"prompt": "two", "session_id": session_id, "agent": echo_agent.name},
                timeout=15,
            )

        assert first.json()["result"] == "echo:one"
        assert second.json()["result"] == "echo:two"


class TestRestAsync:
    def test_enqueue_then_poll_resolves(self, local_config, echo_agent, monkeypatch):
        monkeypatch.setattr(local_config.execution, "mode", ExecutionMode.REST_ASYNC)

        with LocalQueueMode():
            session_id = str(uuid.uuid4())
            accepted = httpx.post(
                f"{_base_url(local_config)}/api/v1/chat",
                json={"prompt": "async hello", "session_id": session_id, "agent": echo_agent.name},
                timeout=15,
            )
            assert accepted.status_code == 200
            request_id = accepted.json()["request_id"]

            # poll_response's own get_message_with_retry already retries internally
            # (execution.response_store is unset in local mode, so it falls back to the
            # ResponseStore ABC's default retry_count=5/delay=5s) — one call is enough.
            polled = httpx.get(
                f"{_base_url(local_config)}/api/v1/chat",
                params={"request_id": request_id, "session_id": session_id},
                timeout=30,
            )

        assert polled.status_code == 200
        assert polled.json()["result"] == "echo:async hello"


class TestUnknownAgent:
    def test_missing_agent_returns_error_body_not_a_hang(self, local_config):
        with LocalQueueMode():
            response = httpx.post(
                f"{_base_url(local_config)}/api/v1/chat",
                json={"prompt": "hi", "session_id": str(uuid.uuid4()), "agent": "does-not-exist"},
                timeout=15,
            )

        assert response.status_code == 200
        assert "error" in response.json()
