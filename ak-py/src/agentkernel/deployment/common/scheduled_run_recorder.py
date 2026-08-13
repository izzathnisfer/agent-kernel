"""Records a scheduled run's terminal outcome from an output-queue response body.

Shared by both output consumers so the recognition step is written once. The write policy
itself (guards, run-field updates, one-time completion) lives in the ``Scheduler``.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ...core.model import ScheduledRunMetadata
from ...scheduler import RunStatus, ScheduleExpression, SchedulerFactory

# An outcome arriving later than this after its scheduled time gets logged, so a backed-up
# deployment is visible. Fixed rather than read from queue attributes, which consumers can't access.
STALE_RUN_WARNING_SECONDS = 900


class ScheduledRunRecorder:
    """Turns an output-queue response body into a run outcome, when it is one."""

    _log = logging.getLogger("ak.deployment.scheduled_run")

    @classmethod
    def record(cls, body: Any) -> bool:
        """Record the outcome if the body carries a ``scheduled_run`` block.

        The block's presence is how a consumer tells a scheduled run from an ordinary one; an
        ``error`` key in the body still means FAILED, so no scheduling-specific status is needed.

        :param body: The output-queue message body, parsed or raw JSON.
        :return: True when this was a scheduled run — it has no client channel to broadcast on
            and nobody polling the response store, so the consumer should stop here.
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

        Called from ``on_permanent_failure``, the last code to see the message before it's
        deleted rather than moved to a DLQ — an outcome not written here is lost forever.

        Status still comes from the body, not from the fact that this is the failure path: a
        body reporting a result means the agent finished and only the recording failed, so
        inventing FAILED here would misreport a task whose agent actually ran fine.

        :param raw_body: The raw output-queue message body, parsed or raw JSON.
        :return: True when this was a scheduled run, whether or not the outcome could be written.
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
        """Log a run whose outcome arrives long after its scheduled time; recording it is unaffected.

        ``last_run_at`` alone can't distinguish a late deployment from an on-time one — this is
        what lets an operator tell the difference.

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
