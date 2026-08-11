"""Records a scheduled run's terminal outcome from an output-queue response body.

Shared by both output consumers (the serverless response handler and the ECS output
consumer) so the recognition step is written once and behaves identically on both targets.

The consumers depend only on the ``Scheduler`` interface: none of the outcome-write policy
lives here. Loading the row, applying the guards, updating the run fields and completing a
one-time task all happen inside the ``Scheduler`` implementation.
"""

import json
import logging
from typing import Any, Optional

from ...core.model import ScheduledRunMetadata
from ...scheduler import RunStatus, SchedulerFactory


class ScheduledRunRecorder:
    """Turns an output-queue response body into a run outcome, when it is one."""

    _log = logging.getLogger("ak.deployment.scheduled_run")

    @classmethod
    def record(cls, body: Any) -> bool:
        """Record the outcome if the body carries a ``scheduled_run`` block.

        The presence of the block is exactly how a consumer tells a scheduled run from an
        ordinary one, and the status comes from the ordinary response shape — an ``error``
        key means FAILED — so no scheduling-specific status or error field is introduced.

        :param body: The output-queue message body, parsed or raw JSON.
        :return: True when this was a scheduled run and the consumer should stop here —
            a scheduled run has no live client channel to broadcast on and nobody polling
            the response store for it.
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

        Called from the output consumers' ``on_permanent_failure``. That is the last code to
        see the message: the consumers' retry limit is one below the queue's, and swallowing
        the failure means the message is deleted rather than moved to a DLQ — so an outcome
        not written here is lost, and the task's row keeps a stale ``last_run_*`` forever.

        The status still comes from the body, never from the fact that we are on the failure
        path: a body reporting a result describes a run the agent completed, and only the
        recording of it failed. Inventing FAILED here would report a broken scheduled task to
        a caller whose agent ran fine.

        :param raw_body: The raw output-queue message body, parsed or raw JSON.
        :return: True when this was a scheduled run and the consumer should stop here,
            whether or not the outcome could be written.
        """
        # The never-raising parser, not from_body: this path has no error channel left, so a
        # malformed block must read as "not a scheduled run" rather than raise.
        scheduled_run = ScheduledRunMetadata.from_raw_body(raw_body)
        if scheduled_run is None:
            return False

        try:
            # from_raw_body only succeeds on a JSON object, so this cannot come back None.
            cls._write_outcome(cls._as_dict(raw_body) or {}, scheduled_run)
        except Exception:
            # Nowhere left to record it: the log is the only trace of this run that survives,
            # so it carries everything needed to identify what was lost.
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
        error = parsed.get("error")
        SchedulerFactory.build().mark_run_completed(
            scheduled_task_id=scheduled_run.scheduled_task_id,
            scheduled_task_version=scheduled_run.scheduled_task_version,
            scheduled_time=scheduled_run.scheduled_time,
            status=RunStatus.FAILED if error else RunStatus.COMPLETED,
            last_error=error,
        )
        cls._log.info(
            "Recorded scheduled run outcome — scheduled_task_id=%s, run_id=%s, status=%s",
            scheduled_run.scheduled_task_id,
            scheduled_run.run_id,
            "FAILED" if error else "COMPLETED",
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
