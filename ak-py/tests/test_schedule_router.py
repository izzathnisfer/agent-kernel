"""FastAPI route layer for scheduled-task management."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from conftest_scheduler import enable_scheduler_config, make_scheduler, reset_scheduler_config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkernel.api.schedule import ScheduleRESTRequestHandler
from agentkernel.auth import Authoriser
from agentkernel.core.util.factory import AKConfigError
from agentkernel.scheduler.model import ScheduleSpec
from agentkernel.scheduler.service import ScheduledTaskService
from agentkernel.scheduler.store.base import PageCursor
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore

OWNER = "u1"
OTHER = "u2"


class StaticAuthoriser(Authoriser):
    """Token 'good-token' resolves to 'u1', 'other-token' to 'u2', anything else is rejected."""

    def authorise(self, token: str) -> Optional[str]:
        return {"good-token": OWNER, "other-token": OTHER}.get(token)


@pytest.fixture(autouse=True)
def _scheduler_config():
    enable_scheduler_config()
    yield
    reset_scheduler_config()


@pytest.fixture
def service() -> ScheduledTaskService:
    return ScheduledTaskService(make_scheduler(InMemoryScheduledTaskStore()))


@pytest.fixture
def client(service) -> TestClient:
    app = FastAPI()
    app.include_router(ScheduleRESTRequestHandler(authoriser=StaticAuthoriser(), service=service).get_router())
    return TestClient(app)


def _auth(token: str = "good-token") -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(service: ScheduledTaskService, scheduled_task_id: str, owner_id: str = OWNER):
    return service.create(
        spec=ScheduleSpec(rate="1 hour", id=scheduled_task_id),
        prompt="run the report",
        agent=None,
        owner_id=owner_id,
    )


def test_construction_without_an_authoriser_fails_loudly():
    """Every scheduled task must have an unforgeable owner."""
    with pytest.raises(AKConfigError, match="Authoriser"):
        ScheduleRESTRequestHandler()


class TestAuthentication:
    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic xyz"}, {"Authorization": "Bearer "}])
    def test_a_missing_or_malformed_token_is_rejected(self, client, headers):
        assert client.get("/api/v1/schedule", headers=headers).status_code == 401

    def test_an_unknown_token_is_rejected(self, client):
        assert client.get("/api/v1/schedule", headers=_auth("nope")).status_code == 401


class TestList:
    def test_only_the_callers_tasks_are_returned(self, client, service):
        _create(service, "mine")
        _create(service, "theirs", owner_id=OTHER)

        body = client.get("/api/v1/schedule", headers=_auth()).json()
        assert [task["scheduled_task_id"] for task in body["scheduled_tasks"]] == ["mine"]

    def test_soft_deleted_tasks_are_never_listed(self, client, service):
        _create(service, "gone")
        service.delete("gone", owner_id=OWNER)

        assert client.get("/api/v1/schedule", headers=_auth()).json()["scheduled_tasks"] == []


class TestPagination:
    """A cursor is client input, so a bad one is a 400 like any other bad input."""

    def test_a_page_can_be_walked_with_the_cursor_it_returns(self, client, service):
        _create(service, "first")
        _create(service, "second")

        page = client.get("/api/v1/schedule?limit=1", headers=_auth()).json()
        assert page["next_cursor"] is not None

        following = client.get(f"/api/v1/schedule?limit=1&cursor={page['next_cursor']}", headers=_auth())
        assert following.status_code == 200

    def test_a_malformed_cursor_is_400_not_500(self, client):
        assert client.get("/api/v1/schedule?cursor=not-a-cursor", headers=_auth()).status_code == 400

    def test_a_well_formed_cursor_of_the_wrong_shape_is_400_not_500(self, client):
        """Base64 alone is not enough: the shape the backend paginates on is part of the contract."""
        forged = PageCursor.encode({"scheduled_task_id": "a"})
        assert client.get(f"/api/v1/schedule?cursor={forged}", headers=_auth()).status_code == 400


class TestRead:
    def test_definition_and_last_run_state_are_returned(self, client, service):
        _create(service, "a")
        body = client.get("/api/v1/schedule/a", headers=_auth()).json()
        assert body["scheduled_task_id"] == "a"
        assert body["last_run_at"] is None

    def test_an_unknown_id_is_404(self, client):
        assert client.get("/api/v1/schedule/missing", headers=_auth()).status_code == 404

    def test_a_soft_deleted_id_is_404(self, client, service):
        """A tombstone is an internal grace-period artefact, not a user-visible state."""
        _create(service, "a")
        service.delete("a", owner_id=OWNER)
        assert client.get("/api/v1/schedule/a", headers=_auth()).status_code == 404

    def test_another_owners_task_is_403(self, client, service):
        _create(service, "a", owner_id=OTHER)
        assert client.get("/api/v1/schedule/a", headers=_auth()).status_code == 403


class TestUpdate:
    def test_the_schedule_and_message_can_be_changed(self, client, service):
        _create(service, "a")
        response = client.put(
            "/api/v1/schedule/a",
            headers=_auth(),
            json={"schedule": {"rate": "2 hours"}, "prompt": "new prompt"},
        )
        assert response.status_code == 200
        assert response.json()["schedule"]["rate"] == "2 hours"
        assert response.json()["message"]["prompt"] == "new prompt"

    def test_update_never_creates(self, client):
        assert client.put("/api/v1/schedule/missing", headers=_auth(), json={"prompt": "x"}).status_code == 404

    def test_another_owners_task_is_403(self, client, service):
        _create(service, "a", owner_id=OTHER)
        assert client.put("/api/v1/schedule/a", headers=_auth(), json={"prompt": "x"}).status_code == 403

    def test_a_soft_deleted_task_is_409(self, client, service):
        _create(service, "a")
        service.delete("a", owner_id=OWNER)
        assert client.put("/api/v1/schedule/a", headers=_auth(), json={"prompt": "x"}).status_code == 409

    def test_a_too_fine_schedule_is_400(self, client, service):
        _create(service, "a")
        response = client.put("/api/v1/schedule/a", headers=_auth(), json={"schedule": {"rate": "10 seconds"}})
        assert response.status_code == 400


class TestDelete:
    def test_deleting_stops_the_task(self, client, service):
        _create(service, "a")
        assert client.delete("/api/v1/schedule/a", headers=_auth()).status_code == 200
        assert client.get("/api/v1/schedule/a", headers=_auth()).status_code == 404

    def test_delete_is_idempotent(self, client, service):
        _create(service, "a")
        client.delete("/api/v1/schedule/a", headers=_auth())
        assert client.delete("/api/v1/schedule/a", headers=_auth()).status_code == 200

    def test_another_owners_task_is_403(self, client, service):
        _create(service, "a", owner_id=OTHER)
        assert client.delete("/api/v1/schedule/a", headers=_auth()).status_code == 403
