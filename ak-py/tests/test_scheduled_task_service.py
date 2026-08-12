"""ScheduledTaskService: identity, ownership, incarnation and the creation acknowledgement."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest_scheduler import enable_scheduler_config, make_scheduler, reset_scheduler_config

from agentkernel.core.model import ScheduleMode
from agentkernel.scheduler.errors import (
    SchedulerConflictError,
    SchedulerNotFoundError,
    SchedulerPermissionError,
    ScheduleValidationError,
)
from agentkernel.scheduler.model import RunStatus, ScheduleSpec, TaskStatus
from agentkernel.scheduler.service import ScheduledTaskService
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore

OWNER = "user-1"
OTHER_OWNER = "user-2"


@pytest.fixture(autouse=True)
def _scheduler_config():
    enable_scheduler_config()
    yield
    reset_scheduler_config()


@pytest.fixture
def store() -> InMemoryScheduledTaskStore:
    return InMemoryScheduledTaskStore()


@pytest.fixture
def service(store) -> ScheduledTaskService:
    return ScheduledTaskService(make_scheduler(store))


def _create(service: ScheduledTaskService, **overrides):
    kwargs = {"spec": ScheduleSpec(rate="1 hour"), "prompt": "run the report", "agent": None, "owner_id": OWNER}
    kwargs.update(overrides)
    return service.create(**kwargs)


class TestIdentity:
    def test_an_id_is_generated_when_the_caller_supplies_none(self, service):
        ack = _create(service)
        assert ack.scheduled_task_id.startswith("schedule_")

    def test_a_caller_supplied_id_is_used_verbatim(self, service):
        ack = _create(service, spec=ScheduleSpec(rate="1 hour", id="nightly-report"))
        assert ack.scheduled_task_id == "nightly-report"

    def test_a_fresh_id_gets_a_new_incarnation(self, service):
        first = _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        second = _create(service, spec=ScheduleSpec(rate="1 hour", id="b"))
        assert first.scheduled_task_version != second.scheduled_task_version

    def test_recreating_a_live_id_retains_its_incarnation(self, service):
        """In-flight runs must still be able to record their outcomes."""
        first = _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        second = _create(service, spec=ScheduleSpec(rate="2 hours", id="a"))
        assert second.scheduled_task_version == first.scheduled_task_version

    def test_update_retains_the_incarnation(self, service):
        ack = _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        task = service.update("a", owner_id=OWNER, prompt="new prompt")
        assert task.scheduled_task_version == ack.scheduled_task_version


class TestOwnership:
    def test_the_owner_is_stamped_from_the_resolved_parameter(self, service, store):
        ack = _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        task = store.get(ack.scheduled_task_id)
        assert task.owner_id == OWNER
        assert task.message["user_id"] == OWNER

    def test_creating_over_someone_elses_live_row_is_rejected(self, service):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        with pytest.raises(SchedulerPermissionError):
            _create(service, spec=ScheduleSpec(rate="1 hour", id="a"), owner_id=OTHER_OWNER)

    @pytest.mark.parametrize("operation", ["get", "update", "delete"])
    def test_operations_on_someone_elses_row_are_rejected(self, service, operation):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        with pytest.raises(SchedulerPermissionError):
            getattr(service, operation)("a", owner_id=OTHER_OWNER)

    def test_list_is_scoped_to_the_caller(self, service):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="mine"))
        _create(service, spec=ScheduleSpec(rate="1 hour", id="theirs"), owner_id=OTHER_OWNER)
        assert [task.scheduled_task_id for task in service.list(owner_id=OWNER).items] == ["mine"]


class TestSessionIds:
    def test_per_run_sessions_are_resolved_at_fire_time(self, service, store):
        ack = _create(service, spec=ScheduleSpec(rate="1 hour", id="a", mode=ScheduleMode.PER_RUN))
        # A per-run task has no stable session, so the ack omits the field rather than
        # returning a template that looks like a usable session id.
        assert ack.session_id is None

    def test_continuous_sessions_are_stable_and_prefixed(self, service):
        ack = _create(service, spec=ScheduleSpec(rate="1 hour", id="a", mode=ScheduleMode.CONTINUOUS))
        assert ack.session_id == "schedule:a"


class TestLifecycle:
    def test_a_deleted_id_cannot_be_recreated_during_its_grace_window(self, service):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        service.delete("a", owner_id=OWNER)
        with pytest.raises(SchedulerConflictError):
            _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))

    def test_a_deleted_row_is_not_user_visible(self, service):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        service.delete("a", owner_id=OWNER)
        with pytest.raises(SchedulerNotFoundError):
            service.get("a", owner_id=OWNER)

    def test_delete_is_idempotent_for_an_unknown_id(self, service):
        service.delete("never-existed", owner_id=OWNER)

    def test_update_never_creates(self, service):
        with pytest.raises(SchedulerNotFoundError):
            service.update("missing", owner_id=OWNER, prompt="x")

    def test_update_on_a_deleted_row_is_rejected(self, service):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a"))
        service.delete("a", owner_id=OWNER)
        with pytest.raises(SchedulerConflictError):
            service.update("a", owner_id=OWNER, prompt="x")

    def test_a_completed_one_time_task_is_re_armed_by_an_update(self, service):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        ack = _create(service, spec=ScheduleSpec(at=future, id="once"))
        service._scheduler.mark_run_completed("once", ack.scheduled_task_version, datetime.now(timezone.utc), RunStatus.COMPLETED)
        assert service.get("once", owner_id=OWNER).status == TaskStatus.COMPLETED

        task = service.update("once", owner_id=OWNER, spec=ScheduleSpec(at=future + timedelta(days=1)))
        assert task.status == TaskStatus.ACTIVE
        assert task.completed_at is None
        assert task.scheduled_task_version == ack.scheduled_task_version

    def test_re_arming_a_completed_one_time_task_needs_a_new_instant(self, service):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        ack = _create(service, spec=ScheduleSpec(at=future, id="once"))
        service._scheduler.mark_run_completed("once", ack.scheduled_task_version, datetime.now(timezone.utc), RunStatus.COMPLETED)

        with pytest.raises(ScheduleValidationError, match="already run"):
            service.update("once", owner_id=OWNER, prompt="changed")

    def test_a_one_time_task_whose_instant_has_passed_needs_a_new_one(self, store, service):
        """Its outcome may never have been recorded, so COMPLETED is not the only signal."""
        _create(service, spec=ScheduleSpec(at=datetime.now(timezone.utc) + timedelta(seconds=30), id="once"))
        elapsed = service._scheduler.get("once")
        elapsed.schedule.at = datetime.now(timezone.utc) - timedelta(minutes=1)
        store.put(elapsed)

        with pytest.raises(ScheduleValidationError, match="already run"):
            service.update("once", owner_id=OWNER, prompt="changed")

    def test_a_pending_one_time_task_can_still_be_updated_in_place(self, service):
        _create(service, spec=ScheduleSpec(at=datetime.now(timezone.utc) + timedelta(days=1), id="once"))
        assert service.update("once", owner_id=OWNER, prompt="changed").message["prompt"] == "changed"

    def test_a_replacement_schedule_that_names_no_mode_keeps_the_current_one(self, service):
        """Retiming a continuous task must not move it to a per-run session id."""
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a", mode=ScheduleMode.CONTINUOUS))

        task = service.update("a", owner_id=OWNER, spec=ScheduleSpec(rate="2 hours"))

        assert task.schedule.mode == ScheduleMode.CONTINUOUS
        assert task.schedule.rate == "2 hours"

    def test_a_replacement_schedule_may_still_change_the_mode_explicitly(self, service):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a", mode=ScheduleMode.CONTINUOUS))

        task = service.update("a", owner_id=OWNER, spec=ScheduleSpec(rate="1 hour", mode=ScheduleMode.PER_RUN))

        assert task.schedule.mode == ScheduleMode.PER_RUN

    def test_update_keeps_fields_the_caller_omitted(self, service):
        _create(service, spec=ScheduleSpec(rate="1 hour", id="a"), agent="reporter")
        task = service.update("a", owner_id=OWNER, prompt="new prompt")
        assert task.message["agent"] == "reporter"
        assert task.schedule.rate == "1 hour"


class TestCreateAck:
    def test_an_invalid_schedule_is_rejected(self, service):
        with pytest.raises(ScheduleValidationError):
            _create(service, spec=ScheduleSpec(rate="10 seconds"))

    def test_next_run_at_is_the_instant_for_a_one_time_schedule(self, service):
        moment = datetime.now(timezone.utc) + timedelta(days=1)
        ack = _create(service, spec=ScheduleSpec(at=moment))
        assert ack.next_run_at == moment

    def test_next_run_at_is_the_registration_time_plus_the_interval_for_a_rate(self, service, store):
        ack = _create(service, spec=ScheduleSpec(rate="30 minutes", id="a"))
        assert ack.next_run_at == store.get("a").updated_at + timedelta(minutes=30)

    def test_next_run_at_re_bases_when_a_create_replaces_a_live_definition(self, service, store):
        """EventBridge re-bases a rate at registration, so an ack from created_at would report a
        next run that has already gone by."""
        _create(service, spec=ScheduleSpec(rate="30 minutes", id="a"))
        original_created_at = store.get("a").created_at

        ack = _create(service, spec=ScheduleSpec(rate="30 minutes", id="a"))

        assert store.get("a").created_at == original_created_at
        assert ack.next_run_at == store.get("a").updated_at + timedelta(minutes=30)
        assert ack.next_run_at >= original_created_at + timedelta(minutes=30)

    def test_next_run_at_is_absent_for_a_cron(self, service):
        """No AWS API supplies it and this adds no cron-evaluator dependency."""
        ack = _create(service, spec=ScheduleSpec(cron="0 9 * * ? *"))
        assert ack.next_run_at is None

    def test_request_id_echoes_the_callers_when_supplied(self, service):
        ack = _create(service, request_id="caller-supplied")
        assert ack.request_id == "caller-supplied"

    def test_request_id_is_generated_when_the_surface_supplies_none(self, service):
        assert _create(service).request_id
