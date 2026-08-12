"""Public testing helpers for scheduler providers and stores.

Two things live here, both importable by bring-your-own-backend authors:

* ``InMemoryScheduledTaskStore`` — a dependency-free reference store used across the test
  suite in place of DynamoDB or Redis.
* ``SchedulerContract`` — a reusable pytest suite asserting the semantics every
  ``Scheduler`` implementation must honor. Subclass it in a test module and override the
  ``scheduler`` fixture. It is deliberately not named ``Test*`` so pytest does not collect
  it on its own.

This module imports ``pytest``, so it is left out of ``agentkernel.scheduler``'s exports to
keep importing the capability free of a pytest dependency.
"""

import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from .base import Scheduler
from .model import RunStatus, ScheduledTask, ScheduledTaskPage, ScheduleMode, ScheduleSpec, TaskStatus
from .store.base import PageCursor, ScheduledTaskStore, TaskSerializer


class InMemoryScheduledTaskStore(ScheduledTaskStore):
    """Dependency-free reference store for tests.

    Stores JSON-safe records exactly as the real backends do, so a test that round-trips a
    row here exercises the same serialization the durable stores use.
    """

    def __init__(self) -> None:
        """Create an empty store."""
        self._records: dict[str, dict[str, Any]] = {}

    def put(self, task: ScheduledTask) -> None:
        self._records[task.scheduled_task_id] = TaskSerializer.to_record(task)

    def update_fields(self, scheduled_task_id: str, fields: dict[str, Any], *, expected_version: Optional[str] = None) -> bool:
        record = self._records.get(scheduled_task_id)
        if record is None:
            return False
        if expected_version is not None and record.get("scheduled_task_version") != expected_version:
            return False
        record.update(TaskSerializer.encode_fields(fields))
        return True

    def get(self, scheduled_task_id: str) -> Optional[ScheduledTask]:
        record = self._records.get(scheduled_task_id)
        return TaskSerializer.from_record(copy.deepcopy(record)) if record is not None else None

    def list_by_owner(self, owner_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None) -> ScheduledTaskPage:
        live = [
            TaskSerializer.from_record(copy.deepcopy(record))
            for record in sorted(self._records.values(), key=lambda item: item["scheduled_task_id"])
            if record.get("owner_id") == owner_id and not record.get("deleted")
        ]
        offset = PageCursor.decode(cursor) or 0
        window = live[offset : offset + limit] if limit is not None else live[offset:]
        next_offset = offset + len(window)
        return ScheduledTaskPage(items=window, next_cursor=PageCursor.encode(next_offset) if next_offset < len(live) else None)

    def remove(self, scheduled_task_id: str) -> None:
        self._records.pop(scheduled_task_id, None)

    def soft_delete(self, scheduled_task_id: str, deleted_at: datetime, ttl_seconds: int) -> None:
        record = self._records.get(scheduled_task_id)
        if record is None:
            return
        record["deleted"] = True
        record["deleted_at"] = deleted_at.isoformat()


def build_task(
    scheduled_task_id: str = "schedule_test",
    *,
    owner_id: str = "user-1",
    version: Optional[str] = None,
    spec: Optional[ScheduleSpec] = None,
) -> ScheduledTask:
    """Build a valid scheduled task for tests.

    :param scheduled_task_id: Identity of the task.
    :param owner_id: The task's owner.
    :param version: Incarnation token; generated when omitted.
    :param spec: The schedule; a one-minute rate when omitted.
    :return: The scheduled task.
    """
    now = datetime.now(timezone.utc)
    return ScheduledTask(
        scheduled_task_id=scheduled_task_id,
        scheduled_task_version=version or uuid.uuid4().hex,
        owner_id=owner_id,
        schedule=spec or ScheduleSpec(rate="1 hour", mode=ScheduleMode.PER_RUN),
        message={"prompt": "run the report", "user_id": owner_id},
        created_at=now,
        updated_at=now,
    )


class SchedulerContract:
    """Reusable suite asserting the semantics every ``Scheduler`` must honor.

    Subclass it and override the ``scheduler`` fixture with the implementation under test.
    """

    @pytest.fixture
    def scheduler(self) -> Scheduler:
        """Return the implementation under test."""
        raise NotImplementedError("override the scheduler fixture")

    def test_upsert_then_get_round_trips(self, scheduler: Scheduler):
        task = build_task("schedule_round_trip")
        scheduler.upsert(task)
        loaded = scheduler.get("schedule_round_trip")
        assert loaded is not None
        assert loaded.owner_id == task.owner_id
        assert loaded.schedule.rate == task.schedule.rate

    def test_get_hides_soft_deleted_rows_unless_asked(self, scheduler: Scheduler):
        scheduler.upsert(build_task("schedule_deleted"))
        scheduler.delete("schedule_deleted")
        assert scheduler.get("schedule_deleted") is None
        assert scheduler.get("schedule_deleted", include_deleted=True) is not None

    def test_delete_is_idempotent(self, scheduler: Scheduler):
        scheduler.upsert(build_task("schedule_twice"))
        scheduler.delete("schedule_twice")
        scheduler.delete("schedule_twice")

    def test_list_returns_only_the_owners_live_rows(self, scheduler: Scheduler):
        scheduler.upsert(build_task("schedule_mine", owner_id="user-1"))
        scheduler.upsert(build_task("schedule_theirs", owner_id="user-2"))
        scheduler.upsert(build_task("schedule_gone", owner_id="user-1"))
        scheduler.delete("schedule_gone")

        listed = {task.scheduled_task_id for task in scheduler.list("user-1").items}
        assert listed == {"schedule_mine"}

    def test_sub_minute_schedule_is_rejected(self, scheduler: Scheduler):
        from .errors import ScheduleValidationError

        task = build_task("schedule_too_fine", spec=ScheduleSpec(rate="30 seconds"))
        with pytest.raises(ScheduleValidationError):
            scheduler.upsert(task)

    def test_outcome_is_recorded_for_a_matching_run(self, scheduler: Scheduler):
        task = build_task("schedule_outcome")
        scheduler.upsert(task)
        scheduled_time = datetime.now(timezone.utc)

        assert scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, scheduled_time, RunStatus.COMPLETED) is True
        loaded = scheduler.get(task.scheduled_task_id)
        assert loaded.last_run_status == RunStatus.COMPLETED
        assert loaded.last_run_at is not None

    def test_outcome_is_discarded_for_an_absent_row(self, scheduler: Scheduler):
        assert scheduler.mark_run_completed("schedule_absent", "v1", datetime.now(timezone.utc), RunStatus.COMPLETED) is False

    def test_outcome_is_discarded_for_a_deleted_row(self, scheduler: Scheduler):
        task = build_task("schedule_deleted_outcome")
        scheduler.upsert(task)
        scheduler.delete(task.scheduled_task_id)

        assert (
            scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, datetime.now(timezone.utc), RunStatus.COMPLETED)
            is False
        )

    def test_outcome_is_discarded_for_a_different_incarnation(self, scheduler: Scheduler):
        task = build_task("schedule_incarnation", version="v-current")
        scheduler.upsert(task)

        assert scheduler.mark_run_completed(task.scheduled_task_id, "v-previous", datetime.now(timezone.utc), RunStatus.COMPLETED) is False

    def test_outcome_is_discarded_when_older_than_the_recorded_run(self, scheduler: Scheduler):
        task = build_task("schedule_stale")
        scheduler.upsert(task)
        newer = datetime.now(timezone.utc)
        older = newer - timedelta(hours=1)

        scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, newer, RunStatus.COMPLETED)
        assert scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, older, RunStatus.FAILED) is False
        assert scheduler.get(task.scheduled_task_id).last_run_status == RunStatus.COMPLETED

    def test_one_time_task_completes_on_its_outcome(self, scheduler: Scheduler):
        spec = ScheduleSpec(at=datetime.now(timezone.utc) + timedelta(days=1))
        task = build_task("schedule_one_time", spec=spec)
        scheduler.upsert(task)

        scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, datetime.now(timezone.utc), RunStatus.COMPLETED)
        loaded = scheduler.get(task.scheduled_task_id)
        assert loaded.status == TaskStatus.COMPLETED
        assert loaded.completed_at is not None

    # ------------------------------------------------- definition writes vs run history

    def test_upsert_over_a_live_row_preserves_its_run_history(self, scheduler: Scheduler):
        """A re-create carries no last_run_* fields, so a whole-row write would reset them. An
        idempotent re-create must not erase the history the retained incarnation exists to keep."""
        task = build_task("schedule_history")
        scheduler.upsert(task)
        scheduled_time = datetime.now(timezone.utc)
        scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, scheduled_time, RunStatus.FAILED, "it blew up")

        scheduler.upsert(task.model_copy(update={"schedule": ScheduleSpec(rate="2 hours")}))

        loaded = scheduler.get(task.scheduled_task_id)
        assert loaded.schedule.rate == "2 hours"
        assert loaded.last_run_status == RunStatus.FAILED
        assert loaded.last_run_at is not None
        assert loaded.last_run_scheduled_time is not None
        assert loaded.last_error == "it blew up"

    def test_upsert_over_a_live_row_keeps_the_stale_outcome_guard_armed(self, scheduler: Scheduler):
        """Resetting last_run_scheduled_time would disarm the guard that rejects an outcome for
        an older fire, letting an in-flight run overwrite a newer one's result."""
        task = build_task("schedule_guard")
        scheduler.upsert(task)
        newer = datetime.now(timezone.utc)
        older = newer - timedelta(hours=1)
        scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, newer, RunStatus.COMPLETED)

        scheduler.upsert(task.model_copy(update={"schedule": ScheduleSpec(rate="3 hours")}))

        assert scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, older, RunStatus.FAILED) is False
        assert scheduler.get(task.scheduled_task_id).last_run_status == RunStatus.COMPLETED

    def test_upsert_on_a_soft_deleted_row_is_rejected(self, scheduler: Scheduler):
        """Deletion is terminal. A whole-row write would silently un-delete the row and register
        a timer for it, which is reachable when a delete lands mid-request."""
        from .errors import SchedulerConflictError

        task = build_task("schedule_tombstone")
        scheduler.upsert(task)
        scheduler.delete(task.scheduled_task_id)

        with pytest.raises(SchedulerConflictError):
            scheduler.upsert(task)
        assert scheduler.get(task.scheduled_task_id, include_deleted=True).deleted is True

    def test_upsert_from_a_superseded_incarnation_is_rejected(self, scheduler: Scheduler):
        """The row was deleted and recreated under a new incarnation since the caller read it,
        so writing its definition would land on a successor that is not the same scheduled task."""
        from .errors import SchedulerConflictError

        scheduler.upsert(build_task("schedule_superseded", version="v-current"))
        stale = build_task("schedule_superseded", version="v-previous", spec=ScheduleSpec(rate="6 hours"))

        with pytest.raises(SchedulerConflictError):
            scheduler.upsert(stale)
        assert scheduler.get("schedule_superseded").schedule.rate == "1 hour"
