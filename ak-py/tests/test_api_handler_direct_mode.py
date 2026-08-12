"""The direct (non-queue) chat route's refusal to silently run a request that asked to be scheduled.

Scheduling is a queue-mode capability, so without this guard a ``schedule`` block would be
silently dropped and the prompt run immediately instead.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkernel.api.handler import AgentRESTRequestHandler
from agentkernel.core.model import ExecutionMode


@pytest.fixture
def client_and_runs():
    """Mount the direct-mode router, recording every request that reaches ChatService."""
    runs = []

    config = Mock()
    config.api.max_file_size = 10_000_000
    config.execution.mode = ExecutionMode.REST_SYNC

    chat_service = Mock()
    chat_service.process_async_chat_request = AsyncMock(side_effect=lambda req: runs.append(req) or {"reply": "ok"})

    with patch("agentkernel.api.handler.Config.get", return_value=config), patch("agentkernel.api.handler.ChatService", return_value=chat_service):
        app = FastAPI()
        app.include_router(AgentRESTRequestHandler().get_router())
        yield TestClient(app), runs


def test_a_body_carrying_a_schedule_is_rejected(client_and_runs):
    client, runs = client_and_runs

    response = client.post(AgentRESTRequestHandler.CHAT_PATH, json={"prompt": "run the report", "session_id": "s1", "schedule": {"rate": "1 hour"}})

    assert response.status_code == 400
    assert "queue mode" in response.json()["detail"]
    # The point of the guard: never a silent immediate execution.
    assert runs == []


def test_an_ordinary_body_still_runs(client_and_runs):
    client, runs = client_and_runs

    assert client.post(AgentRESTRequestHandler.CHAT_PATH, json={"prompt": "hi", "session_id": "s1"}).status_code == 200
    assert len(runs) == 1


def test_an_invalid_schedule_block_is_still_rejected(client_and_runs):
    """Rejected by the body model before the route is entered — either way, nothing runs."""
    client, runs = client_and_runs

    response = client.post(AgentRESTRequestHandler.CHAT_PATH, json={"prompt": "hi", "session_id": "s1", "schedule": {}})

    assert response.status_code in (400, 422)
    assert runs == []
