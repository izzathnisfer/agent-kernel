"""The queue-aware REST chat route's schedule branch.

A body carrying a ``schedule`` block is registered to run later; nothing is enqueued and
the sync response-store wait is skipped entirely.
"""

from typing import Optional
from unittest.mock import MagicMock

import pytest
from conftest_scheduler import enable_scheduler_config, install_scheduler, reset_scheduler_config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkernel.core.thread import Authoriser
from agentkernel.core.util.factory import AKConfigError
from agentkernel.deployment.common.rest_handler import RestHandler
from agentkernel.scheduler.model import ScheduleSpec
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore

OWNER = "u1"


class StaticAuthoriser(Authoriser):
    def authorise(self, token: str) -> Optional[str]:
        return OWNER if token == "good-token" else None


class _FakeRestHandler(RestHandler):
    """RestHandler with stubbed collaborators, so only the chat route is under test."""

    def __init__(self, authoriser: Optional[Authoriser] = None):
        super().__init__(authoriser=authoriser)
        self.queue_handler = MagicMock()
        self.response_store = MagicMock()

    def get_response_store(self):
        return self.response_store

    def get_queue_handler(self):
        return self.queue_handler


@pytest.fixture(autouse=True)
def _scheduler_config():
    enable_scheduler_config()
    install_scheduler(InMemoryScheduledTaskStore())
    yield
    reset_scheduler_config()


@pytest.fixture
def handler() -> _FakeRestHandler:
    return _FakeRestHandler(authoriser=StaticAuthoriser())


@pytest.fixture
def client(handler) -> TestClient:
    app = FastAPI()
    app.include_router(handler.get_router())
    return TestClient(app)


def _auth(token: str = "good-token") -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_a_scheduled_body_is_registered_and_never_enqueued(client, handler):
    response = client.post(
        RestHandler.CHAT_PATH,
        headers=_auth(),
        json={"prompt": "run the report", "schedule": {"rate": "1 hour", "id": "a"}},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "SCHEDULED"
    assert response.json()["scheduled_task_id"] == "a"
    handler.queue_handler.send_message_to_input_queue.assert_not_called()


def test_the_sync_response_store_wait_is_skipped(client, handler):
    """There is no run to wait for — the first message appears when the timer fires."""
    client.post(RestHandler.CHAT_PATH, headers=_auth(), json={"prompt": "hi", "schedule": {"rate": "1 hour"}})
    handler.response_store.get_message_with_retry.assert_not_called()


def test_a_scheduled_body_needs_no_session_id(client):
    """The service derives one; requiring the caller to invent one would be meaningless."""
    response = client.post(RestHandler.CHAT_PATH, headers=_auth(), json={"prompt": "hi", "schedule": {"rate": "1 hour"}})
    assert response.status_code == 201


def test_an_unauthenticated_schedule_request_is_rejected(client, handler):
    response = client.post(RestHandler.CHAT_PATH, json={"prompt": "hi", "schedule": {"rate": "1 hour"}})
    assert response.status_code == 401
    handler.queue_handler.send_message_to_input_queue.assert_not_called()


def test_a_too_fine_schedule_is_rejected(client):
    response = client.post(RestHandler.CHAT_PATH, headers=_auth(), json={"prompt": "hi", "schedule": {"rate": "10 seconds"}})
    assert response.status_code == 400


def test_a_foreign_live_row_is_rejected(client, handler):
    handler._schedule_service.create(spec=ScheduleSpec(rate="1 hour", id="a"), prompt="hi", agent=None, owner_id="someone-else")

    response = client.post(RestHandler.CHAT_PATH, headers=_auth(), json={"prompt": "hi", "schedule": {"rate": "1 hour", "id": "a"}})
    assert response.status_code == 403


def test_a_soft_deleted_id_is_rejected(client, handler):
    handler._schedule_service.create(spec=ScheduleSpec(rate="1 hour", id="a"), prompt="hi", agent=None, owner_id=OWNER)
    handler._schedule_service.delete("a", owner_id=OWNER)

    response = client.post(RestHandler.CHAT_PATH, headers=_auth(), json={"prompt": "hi", "schedule": {"rate": "1 hour", "id": "a"}})
    assert response.status_code == 409


def test_a_scheduled_body_is_rejected_when_scheduling_is_disabled(client, handler):
    handler._schedule_service = None
    response = client.post(RestHandler.CHAT_PATH, headers=_auth(), json={"prompt": "hi", "schedule": {"rate": "1 hour"}})
    assert response.status_code == 400


def test_an_ordinary_body_still_requires_a_session_id(client):
    """Pre-change behaviour, unchanged on the non-schedule path."""
    response = client.post(RestHandler.CHAT_PATH, headers=_auth(), json={"prompt": "hi"})
    assert response.status_code == 400


class TestAuthoriserRequirement:
    def test_construction_without_an_authoriser_fails_loudly(self):
        with pytest.raises(AKConfigError, match="Authoriser"):
            _FakeRestHandler()

    def test_no_authoriser_is_needed_when_scheduling_is_disabled(self):
        reset_scheduler_config()
        _FakeRestHandler()  # must not raise
