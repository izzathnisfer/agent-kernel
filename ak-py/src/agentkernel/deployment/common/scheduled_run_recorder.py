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
        return True

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
