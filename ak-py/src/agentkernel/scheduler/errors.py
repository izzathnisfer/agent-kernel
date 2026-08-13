"""Exception hierarchy for the scheduled-task capability.

Every scheduling failure is a subclass of :class:`SchedulerError`. These signal failures of
the scheduling machinery only. A failed *run* is not an error here; it is recorded on the row
as ``last_run_status = FAILED``.
"""


class SchedulerError(Exception):
    """Base class for all scheduled-task capability errors."""


class ScheduleValidationError(SchedulerError):
    """The schedule expression is invalid, or finer than the provider's granularity.

    Mapped to HTTP 400. Raised before any provider call, so a bad expression never reaches
    the timer.
    """


class SchedulerNotFoundError(SchedulerError):
    """No live scheduled task exists at the given id. Mapped to HTTP 404."""


class SchedulerPermissionError(SchedulerError):
    """The caller does not own the scheduled task. Mapped to HTTP 403."""


class SchedulerConflictError(SchedulerError):
    """The id is soft-deleted, or a concurrent writer holds the row. Mapped to HTTP 409."""


# Ordered most-derived first so a subclass isn't shadowed by its base; `SchedulerError`
# last, so an unrecognised failure reads as a 400 rather than escaping as a 500.
_STATUS_BY_ERROR: tuple[tuple[type[SchedulerError], int], ...] = (
    (SchedulerNotFoundError, 404),
    (SchedulerPermissionError, 403),
    (SchedulerConflictError, 409),
    (ScheduleValidationError, 400),
    (SchedulerError, 400),
)


def http_status_for(exc: BaseException, default: int = 400) -> int:
    """Map a scheduling failure to the HTTP status every surface answers it with.

    The single place this mapping is written down, so REST routes, WebSocket routes and
    Lambda routers can't drift on what a given failure means.

    :param exc: The failure to classify.
    :param default: Status for anything not in the table, including a plain ``ValueError`` from
        request parsing, which is bad input like the rest.
    :return: The HTTP status.
    """
    for error_type, status_code in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status_code
    return default
