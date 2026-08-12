"""The single place all scheduling logic lives.

Its three callers — the chat create path, the ``/api/v1/schedule`` routes, and the
agent-callable tools — all go through it, so there are no parallel code paths. Unlike
``ChatService`` it never runs an agent; it only registers and manages schedules.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.model import SCHEDULED_SESSION_PREFIX
from .base import Scheduler
from .errors import SchedulerConflictError, SchedulerNotFoundError, SchedulerPermissionError, ScheduleValidationError
from .expression import ScheduleExpression
from .model import CreateAck, ScheduledTask, ScheduledTaskPage, ScheduleMode, ScheduleSpec, TaskStatus

# Server-generated scheduled_task_ids carry this prefix so they are recognisable next to
# caller-supplied ones.
GENERATED_ID_PREFIX = "schedule_"


class ScheduledTaskService:
    """Validates, owns and manages scheduled-task definitions."""

    _log = logging.getLogger("ak.scheduler.service")

    def __init__(self, scheduler: Scheduler):
        """
        :param scheduler: The provider that owns the table and the timer registrations.
        """
        self._scheduler = scheduler

    def create(
        self,
        *,
        spec: ScheduleSpec,
        prompt: str,
        agent: Optional[str],
        owner_id: str,
        request_id: Optional[str] = None,
    ) -> CreateAck:
        """Register a message to run later, and acknowledge the registration.

        Nothing is enqueued: the first message on the input queue appears when the timer
        fires.

        :param spec: The timing expression plus conversation mode.
        :param prompt: The prompt the agent receives on every fire.
        :param agent: The agent to run; None selects the deployment's default.
        :param owner_id: The caller's authenticated identity, resolved by the caller.
        :param request_id: Correlation id for this create call; generated when absent.
        :return: The acknowledgement, confirming registration and never execution.
        :raises ScheduleValidationError: The schedule is invalid or too fine.
        :raises SchedulerPermissionError: A live row at that id belongs to someone else.
        :raises SchedulerConflictError: The id is soft-deleted; deletion is terminal.
        """
        ScheduleExpression.validate(spec, self._scheduler.minimum_granularity)

        scheduled_task_id = spec.id or f"{GENERATED_ID_PREFIX}{uuid.uuid4().hex}"
        existing = self._resolve_existing(scheduled_task_id, owner_id)
        now = datetime.now(timezone.utc)

        task = ScheduledTask(
            scheduled_task_id=scheduled_task_id,
            # A live row keeps its version so in-flight runs still record their outcomes;
            # a fresh id gets a new incarnation.
            scheduled_task_version=existing.scheduled_task_version if existing else uuid.uuid4().hex,
            owner_id=owner_id,
            schedule=spec,
            message=self._build_message(prompt=prompt, agent=agent, owner_id=owner_id),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._scheduler.upsert(task)
        self._log.info("Registered scheduled task %s for owner %s", scheduled_task_id, owner_id)

        return self._acknowledge(task, request_id)

    def update(
        self,
        scheduled_task_id: str,
        *,
        owner_id: str,
        spec: Optional[ScheduleSpec] = None,
        prompt: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> ScheduledTask:
        """Change an existing scheduled task's schedule or message.

        Update never creates, and updates affect future executions only: a fire already
        enqueued continues with the definition it was enqueued with.

        :param scheduled_task_id: Identity of the scheduled task to change.
        :param owner_id: The caller's authenticated identity.
        :param spec: Replacement schedule; the existing one is kept when omitted.
        :param prompt: Replacement prompt; the existing one is kept when omitted.
        :param agent: Replacement agent; the existing one is kept when omitted.
        :return: The updated scheduled task.
        :raises SchedulerNotFoundError: There is no live row at that id.
        :raises SchedulerPermissionError: The caller does not own it.
        :raises SchedulerConflictError: The id is soft-deleted.
        :raises ScheduleValidationError: The schedule is invalid or too fine, or the task is a
            one-time task that has already run and no new instant was supplied.
        """
        existing = self._require_live(scheduled_task_id, owner_id)
        self._require_instant_to_rearm(existing, spec)

        schedule = existing.schedule if spec is None else self._with_existing_mode(spec, existing.schedule)
        if spec is not None:
            ScheduleExpression.validate(schedule, self._scheduler.minimum_granularity)

        task = existing.model_copy(
            update={
                "schedule": schedule,
                "message": self._build_message(
                    prompt=prompt if prompt is not None else existing.message.get("prompt"),
                    agent=agent if agent is not None else existing.message.get("agent"),
                    owner_id=existing.owner_id,
                ),
                "updated_at": datetime.now(timezone.utc),
                # Re-arms a fired one-time task: the guard above has already required the new
                # instant, so this is the same scheduled task rescheduled and the version is
                # retained. A no-op for a recurring task, which is never COMPLETED.
                "status": TaskStatus.ACTIVE,
                "completed_at": None,
            }
        )
        self._scheduler.upsert(task)
        self._log.info("Updated scheduled task %s", scheduled_task_id)
        return task

    def delete(self, scheduled_task_id: str, *, owner_id: str) -> None:
        """Remove the timer registration and soft-delete the row. Idempotent.

        :param scheduled_task_id: Identity of the scheduled task to delete.
        :param owner_id: The caller's authenticated identity.
        :raises SchedulerPermissionError: The caller does not own it.
        """
        existing = self._scheduler.get(scheduled_task_id)
        if existing is None:
            return
        self._require_owner(existing, owner_id)
        self._scheduler.delete(scheduled_task_id)
        self._log.info("Deleted scheduled task %s", scheduled_task_id)

    def get(self, scheduled_task_id: str, *, owner_id: str) -> ScheduledTask:
        """Read one scheduled task's definition and last-run state.

        :param scheduled_task_id: Identity of the scheduled task.
        :param owner_id: The caller's authenticated identity.
        :return: The scheduled task.
        :raises SchedulerNotFoundError: Unknown or soft-deleted — a tombstone is an
            internal grace-period artefact, not a user-visible state.
        :raises SchedulerPermissionError: The caller does not own it.
        """
        task = self._scheduler.get(scheduled_task_id)
        if task is None:
            raise SchedulerNotFoundError(f"no scheduled task '{scheduled_task_id}'")
        self._require_owner(task, owner_id)
        return task

    def list(self, *, owner_id: str, limit: Optional[int] = None, cursor: Optional[str] = None) -> ScheduledTaskPage:
        """List the caller's own scheduled tasks. Soft-deleted rows are never returned.

        :param owner_id: The caller's authenticated identity.
        :param limit: Maximum number of rows in the page.
        :param cursor: Opaque continuation token from a previous page.
        :return: One page of scheduled tasks.
        """
        return self._scheduler.list(owner_id, limit=limit, cursor=cursor)

    # ------------------------------------------------------------------ helpers

    def _resolve_existing(self, scheduled_task_id: str, owner_id: str) -> Optional[ScheduledTask]:
        """Resolve what a create at this id is replacing, if anything.

        :param scheduled_task_id: The id being created at.
        :param owner_id: The caller's authenticated identity.
        :return: The live row being upserted over, or None for a fresh id.
        :raises SchedulerPermissionError: A live row there belongs to someone else.
        :raises SchedulerConflictError: The id is soft-deleted.
        """
        existing = self._scheduler.get(scheduled_task_id, include_deleted=True)
        if existing is None:
            return None
        if existing.deleted:
            raise SchedulerConflictError(
                f"scheduled task '{scheduled_task_id}' is deleted; deletion is terminal and the id frees up when its grace period expires"
            )
        self._require_owner(existing, owner_id)
        return existing

    def _require_live(self, scheduled_task_id: str, owner_id: str) -> ScheduledTask:
        """Load a row that an update may act on.

        :param scheduled_task_id: Identity of the scheduled task.
        :param owner_id: The caller's authenticated identity.
        :return: The live row.
        :raises SchedulerNotFoundError: There is no row at that id.
        :raises SchedulerConflictError: The row is soft-deleted.
        :raises SchedulerPermissionError: The caller does not own it.
        """
        existing = self._scheduler.get(scheduled_task_id, include_deleted=True)
        if existing is None:
            raise SchedulerNotFoundError(f"no scheduled task '{scheduled_task_id}'; creation is the chat endpoint's job")
        if existing.deleted:
            raise SchedulerConflictError(f"scheduled task '{scheduled_task_id}' is deleted and cannot be restored")
        self._require_owner(existing, owner_id)
        return existing

    @staticmethod
    def _require_instant_to_rearm(existing: ScheduledTask, spec: Optional[ScheduleSpec]) -> None:
        """Require a new instant before a one-time task that has already run may be updated.

        Its ``at`` has elapsed, so re-registering the schedule unchanged would be rejected as a
        schedule in the past. Asking for the replacement here names the field the caller has to
        change, rather than failing on a schedule they never supplied.

        :param existing: The live row being updated.
        :param spec: The replacement schedule, or None when the caller supplied none.
        :raises ScheduleValidationError: The task has already run and no new instant was given.
        """
        if spec is not None or not ScheduleExpression.is_one_time(existing.schedule):
            return
        # Either signal is enough: status is COMPLETED once the run's outcome is recorded, and
        # the instant is in the past from the moment it fires.
        has_run = existing.status == TaskStatus.COMPLETED or ScheduleExpression.as_utc(existing.schedule.at) <= datetime.now(timezone.utc)
        if not has_run:
            return
        raise ScheduleValidationError(
            f"one-time scheduled task '{existing.scheduled_task_id}' has already run; " "supply a schedule with a new future 'at' to re-arm it"
        )

    @staticmethod
    def _with_existing_mode(spec: ScheduleSpec, existing: ScheduleSpec) -> ScheduleSpec:
        """Carry the current conversation mode onto a replacement schedule that names none.

        ``mode`` has a model default, so a caller changing only the timing would otherwise
        silently move a continuous task to per-run — which changes its session id and abandons
        the conversation it had been accumulating.

        :param spec: The replacement schedule as supplied.
        :param existing: The schedule being replaced.
        :return: The replacement, taking the existing mode when the caller named none.
        """
        if "mode" in spec.model_fields_set:
            return spec
        return spec.model_copy(update={"mode": existing.mode})

    @staticmethod
    def _require_owner(task: ScheduledTask, owner_id: str) -> None:
        """Enforce that the caller owns the scheduled task.

        :param task: The row being acted on.
        :param owner_id: The caller's authenticated identity.
        :raises SchedulerPermissionError: The caller does not own it.
        """
        if task.owner_id != owner_id:
            raise SchedulerPermissionError(f"scheduled task '{task.scheduled_task_id}' is not owned by the authenticated caller")

    @staticmethod
    def _build_message(*, prompt: Optional[str], agent: Optional[str], owner_id: str) -> dict[str, Any]:
        """Build the agent message template the timer delivers.

        The owner is stamped from a resolved parameter, never read from a request body, so it
        cannot be forged. ``session_id`` is absent because only the provider knows how its
        timer expresses the fire time, so the provider fills it in at registration.

        :param prompt: The prompt the agent receives on every fire.
        :param agent: The agent to run; omitted when None.
        :param owner_id: The scheduled task's owner.
        :return: The message template.
        """
        message: dict[str, Any] = {"prompt": prompt, "user_id": owner_id}
        if agent:
            message["agent"] = agent
        return message

    @staticmethod
    def _acknowledge(task: ScheduledTask, request_id: Optional[str]) -> CreateAck:
        """Build the creation acknowledgement.

        :param task: The registered scheduled task.
        :param request_id: Correlation id supplied by the surface, or None to generate one.
        :return: The acknowledgement payload.
        """
        return CreateAck(
            scheduled_task_id=task.scheduled_task_id,
            scheduled_task_version=task.scheduled_task_version,
            # Only continuous mode has a stable session; a per-run id exists only at fire time.
            session_id=(f"{SCHEDULED_SESSION_PREFIX}{task.scheduled_task_id}" if task.schedule.mode == ScheduleMode.CONTINUOUS else None),
            # updated_at, not created_at: a create over a live id re-registers the schedule, so
            # a rate counts from this call rather than from when the id was first taken.
            next_run_at=ScheduleExpression.next_run_at(task.schedule, task.updated_at),
            request_id=request_id or str(uuid.uuid4()),
        )
