"""The provider-agnostic ``Scheduler`` contract."""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

from .model import RunStatus, ScheduledTask, ScheduledTaskPage


class Scheduler(ABC):
    """Owns the scheduled-task table and the timer registrations.

    The ``ScheduledTaskStore`` is a private collaborator held by the implementation: no
    caller — not the service, not the route layers, not the output consumers — resolves or
    calls it. Every row read and write goes through a method here.

    Beyond the method contract, an implementation must guarantee:

    * **At-most-once delivery per (scheduled_task_id, scheduled_time)** — a duplicate
      timer-side fire is suppressed before it reaches the agent runner.
    * **Fires of the same scheduled task are serialized.**
    * **One-time registrations remove themselves after firing** — no cleanup process.
    * **Schedules finer than the provider's minimum granularity are rejected at
      registration**, never silently rounded.
    * **The delivered payload is a valid ordinary agent message** — a fully resolved
      ``session_id`` and a ``scheduled_run`` block, so the runner needs no scheduling
      awareness.
    * **Outcome writes are guarded against stale and mismatched runs.**
    """

    @property
    @abstractmethod
    def minimum_granularity(self) -> timedelta:
        """The finest interval this provider's timer supports.

        Exposed so callers above the ABC can reject a too-fine schedule without knowing
        which provider is in use.
        """

    @abstractmethod
    def upsert(self, task: ScheduledTask) -> ScheduledTask:
        """Persist the row and register (or replace) its timer registration.

        The row is written first, then registered; a registration failure rolls the row
        back to its prior state, because a row without a registration would silently never
        fire.

        :param task: The scheduled task to persist and register.
        :return: The persisted scheduled task.
        :raises ScheduleValidationError: Expression invalid or finer than the provider's granularity.
        """

    @abstractmethod
    def delete(self, scheduled_task_id: str) -> None:
        """Remove the timer registration, then soft-delete the row. Idempotent.

        Ordering is deliberate: stopping future fires is the safe half, so it happens
        first.

        :param scheduled_task_id: Identity of the scheduled task to delete.
        """

    @abstractmethod
    def get(self, scheduled_task_id: str, *, include_deleted: bool = False) -> Optional[ScheduledTask]:
        """Read one row.

        :param scheduled_task_id: Identity of the scheduled task.
        :param include_deleted: When True, soft-deleted rows are returned too.
        :return: The scheduled task, or None when absent (or hidden by the filter).
        """

    @abstractmethod
    def list(self, owner_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None) -> ScheduledTaskPage:
        """List an owner's live rows. Soft-deleted rows are never returned.

        :param owner_id: The authenticated owner whose tasks to list.
        :param limit: Maximum number of rows in the page.
        :param cursor: Opaque continuation token from a previous page.
        :return: One page of scheduled tasks.
        """

    @abstractmethod
    def mark_run_completed(
        self,
        scheduled_task_id: str,
        scheduled_task_version: str,
        scheduled_time: datetime,
        status: RunStatus,
        last_error: Optional[str] = None,
    ) -> bool:
        """Record a terminal run outcome on the row.

        There is deliberately no ``mark_run_started``: only terminal outcomes are
        recorded, so a stuck run is visible from queue metrics instead of costing a write
        on every fire.

        :param scheduled_task_id: Identity of the scheduled task the run belongs to.
        :param scheduled_task_version: Incarnation token carried by the fire.
        :param scheduled_time: The time the fire was scheduled for.
        :param status: The run's terminal status.
        :param last_error: Error detail when the run failed.
        :return: False (a logged no-op) when a guard rejects the write; True when recorded.
        :raises SchedulerError: Only on store or infrastructure failure — a guard rejection
            and an infrastructure failure are deliberately different outcomes.
        """
