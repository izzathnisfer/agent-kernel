"""Data shapes of the scheduled-task capability.

``ScheduleSpec``, ``ScheduleMode`` and ``ScheduledRunMetadata`` are defined in
``core/model.py`` and re-exported here. They are fields on ``BaseRunRequest``, and Pydantic
resolves field annotations at import time, so a lazy import cannot satisfy them. Defining
them in core keeps the dependency one-way: ``core/`` imports nothing from ``scheduler/``.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel

from ..core.model import SCHEDULED_SESSION_PREFIX, ScheduledRunMetadata, ScheduleMode, ScheduleSpec

__all__ = [
    "SCHEDULED_SESSION_PREFIX",
    "CreateAck",
    "RunStatus",
    "ScheduleMode",
    "ScheduleSpec",
    "ScheduledRunMetadata",
    "ScheduledTask",
    "ScheduledTaskPage",
    "TaskStatus",
]


class TaskStatus(str, Enum):
    """Lifecycle state of a scheduled task's definition."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"  # a one-time task that has fired


class RunStatus(str, Enum):
    """Terminal outcome of one run. There is no started state — only outcomes are recorded."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ScheduledTask(BaseModel):
    """The stored row: an agent message plus the schedule that fires it.

    Written only by the ``Scheduler``; no other component touches the store.
    """

    scheduled_task_id: str
    # Incarnation token. Caller-chosen ids become reusable once a deleted row's TTL expires,
    # so an outcome write carries this to prove it belongs to the row it lands on.
    scheduled_task_version: str
    owner_id: str
    schedule: ScheduleSpec
    # The agent message the timer delivers, with the provider's substitution placeholders
    # still in place, so a GET shows exactly what will be sent.
    message: dict[str, Any]
    status: TaskStatus = TaskStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[RunStatus] = None
    last_run_scheduled_time: Optional[datetime] = None
    last_error: Optional[str] = None
    deleted: bool = False
    deleted_at: Optional[datetime] = None


class ScheduledTaskPage(BaseModel):
    """One page of an owner's scheduled tasks."""

    items: list[ScheduledTask] = []
    next_cursor: Optional[str] = None


class CreateAck(BaseModel):
    """Acknowledgement that a task was registered, not that a run happened.

    Delivered on the channel the caller would have received a chat reply on. Run outcomes are
    observed through ``GET /api/v1/schedule/{scheduled_task_id}``.
    """

    status: Literal["SCHEDULED"] = "SCHEDULED"
    scheduled_task_id: str
    scheduled_task_version: str
    # CONTINUOUS mode only. A PER_RUN session id is a template resolved at fire time, which
    # would look like a usable session id without being one.
    session_id: Optional[str] = None
    # Best-effort: set when derivable from the expression, None for cron. None never means
    # "not scheduled".
    next_run_at: Optional[datetime] = None
    # Correlates the create call itself, not a run — creation enqueues nothing.
    request_id: str
