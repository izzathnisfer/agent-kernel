"""Records a scheduled run's terminal outcome from an output-queue response body.

Shared by both output consumers (the serverless response handler and the ECS output
consumer) so the recognition step is written once and behaves identically on both targets.

No outcome-write policy lives here. Loading the row, applying the guards, updating the run
fields and completing a one-time task all happen inside the ``Scheduler`` implementation.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ...core.model import ScheduledRunMetadata
from ...scheduler import RunStatus, ScheduleExpression, SchedulerFactory

# A fire whose outcome arrives more than this long after its scheduled time is logged, so a
# late-firing or badly backed-up deployment is visible rather than silently on time. Matches the
# floor of the providers' derived grace window, and is a fixed number precisely so recording an
# outcome needs no queue-attribute read (the output consumers deliberately have no permission
# for one).
STALE_RUN_WARNING_SECONDS = 900


class ScheduledRunRecorder:
    """Turns an output-queue response body into a run outcome, when it is one."""

    _log = logging.getLogger("ak.deployment.scheduled_run")

    @classmethod
    def record(cls, body: Any) -> bool:
        """Record the outcome if the body carries a ``scheduled_run`` block.

        The block's presence is how a consumer tells a scheduled run from an ordinary one, and
        the status comes from the ordinary response shape (an ``error`` key means FAILED), so
        no scheduling-specific status or error field is needed.

        :param body: The output-queue message body, parsed or raw JSON.
        :return: True when this was a scheduled run and the consumer should stop here, since a
            scheduled run has no client channel to broadcast on and nobody polling the
            response store for it.
        """
        parsed = cls._as_dict(body)
        if parsed is None:
            return False
        scheduled_run = ScheduledRunMetadata.from_body(parsed)
        if scheduled_run is None:
            return False

        cls._write_outcome(parsed, scheduled_run)
        return True

    @classmethod
    def record_before_discard(cls, raw_body: Any) -> bool:
        """Make the last possible attempt to record the outcome, for a consumer that is giving up.

        Called from the output consumers' ``on_permanent_failure``, the last code to see the
        message: the consumers' retry limit is one below the queue's and swallowing the failure
        deletes the message rather than moving it to a DLQ. An outcome not written here is lost
        and the row keeps a stale ``last_run_*`` forever.

        The status still comes from the body, not from the fact that this is the failure path.
        A body reporting a result describes a run the agent completed where only the recording
        failed, so inventing FAILED would report a broken task to a caller whose agent ran fine.

        :param raw_body: The raw output-queue message body, parsed or raw JSON.
        :return: True when this was a scheduled run and the consumer should stop here,
            whether or not the outcome could be written.
        """
        # from_raw_body, not from_body: this path has no error channel left, so a malformed
        # block must read as "not a scheduled run" rather than raise.
        scheduled_run = ScheduledRunMetadata.from_raw_body(raw_body)
        if scheduled_run is None:
            return False

        try:
            # from_raw_body only succeeds on a JSON object, so this cannot come back None.
            cls._write_outcome(cls._as_dict(raw_body) or {}, scheduled_run)
        except Exception:
            # Nothing left to record it on, so this log is the run's only surviving trace.
            cls._log.exception(
                "Lost the outcome of a scheduled run — scheduled_task_id=%s, scheduled_task_version=%s, " "scheduled_time=%s, run_id=%s",
                scheduled_run.scheduled_task_id,
                scheduled_run.scheduled_task_version,
                scheduled_run.scheduled_time.isoformat(),
                scheduled_run.run_id,
            )
        return True

    @classmethod
    def _write_outcome(cls, parsed: dict, scheduled_run: ScheduledRunMetadata) -> None:
        """Write one run's terminal outcome, deriving the status from the body.

        :param parsed: The parsed message body.
        :param scheduled_run: The identity block the fire carried.
        """
        cls._warn_if_stale(scheduled_run)
        error = parsed.get("error")
        recorded = SchedulerFactory.build().mark_run_completed(
            scheduled_task_id=scheduled_run.scheduled_task_id,
            scheduled_task_version=scheduled_run.scheduled_task_version,
            scheduled_time=scheduled_run.scheduled_time,
            status=RunStatus.FAILED if error else RunStatus.COMPLETED,
            last_error=error,
        )
        if not recorded:
            # A guard rejected the write and logged why. Claiming a recorded outcome here would
            # contradict that log line.
            return
        cls._log.info(
            "Recorded scheduled run outcome — scheduled_task_id=%s, run_id=%s, status=%s",
            scheduled_run.scheduled_task_id,
            scheduled_run.run_id,
            "FAILED" if error else "COMPLETED",
        )

    @classmethod
    def _warn_if_stale(cls, scheduled_run: ScheduledRunMetadata) -> None:
        """Log a run whose outcome arrives long after the time it was scheduled for.

        The run is still recorded: a late fire is a real run whose outcome belongs on the row.
        The log is what lets an operator tell a late deployment from an on-time one, since
        ``last_run_at`` alone shows only that the run happened.

        :param scheduled_run: The identity block the fire carried.
        """
        # as_utc: the fire time arrives as whatever the timer wrote, and comparing a naive one
        # against an aware now() would raise.
        lateness = (datetime.now(timezone.utc) - ScheduleExpression.as_utc(scheduled_run.scheduled_time)).total_seconds()
        if lateness <= STALE_RUN_WARNING_SECONDS:
            return
        cls._log.warning(
            "Scheduled run outcome is %ss late — scheduled_task_id=%s, run_id=%s, scheduled_time=%s",
            int(lateness),
            scheduled_run.scheduled_task_id,
            scheduled_run.run_id,
            scheduled_run.scheduled_time.isoformat(),
        )

    @staticmethod
    def _as_dict(body: Any) -> Optional[dict]:
        """Coerce a message body to a dict without raising on an unparseable one.

        :param body: The output-queue message body.
        :return: The body as a dict, or None when it is not a JSON object.
        """
        if isinstance(body, dict):
            return body
        if isinstance(body, (str, bytes)):
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None
