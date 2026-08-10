"""Exception hierarchy for the scheduled-task capability.

Every scheduling failure is a subclass of :class:`SchedulerError`. A *run* that fails is
not an error here — it is recorded on the row as ``last_run_status = FAILED``. These
exceptions signal failures of the scheduling machinery only.
"""


class SchedulerError(Exception):
    """Base class for all scheduled-task capability errors."""


class ScheduleValidationError(SchedulerError):
    """The schedule expression is invalid, or finer than the provider's granularity.

    Mapped to HTTP 400; raised before any provider call so a bad expression never
    reaches the timer.
    """


class SchedulerNotFoundError(SchedulerError):
    """No live scheduled task exists at the given id. Mapped to HTTP 404."""


class SchedulerPermissionError(SchedulerError):
    """The caller does not own the scheduled task. Mapped to HTTP 403."""


class SchedulerConflictError(SchedulerError):
    """The id is soft-deleted, or a concurrent writer holds the row. Mapped to HTTP 409."""
