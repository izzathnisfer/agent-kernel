"""The shared recognition step both output consumers use.

``record`` runs on the ordinary path and may raise; ``record_before_discard`` runs from
``on_permanent_failure``, which has no error channel left, so it must never raise.
"""

import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agentkernel.deployment.common.scheduled_run_recorder import ScheduledRunRecorder

SCHEDULED_RUN = {
    "scheduled_task_id": "schedule_a",
    "scheduled_task_version": "v1",
    "scheduled_time": "2026-08-09T09:00:00Z",
    "run_id": "exec-1",
}


@pytest.fixture
def scheduler():
    with patch("agentkernel.deployment.common.scheduled_run_recorder.SchedulerFactory") as factory:
        yield factory.build.return_value


class TestRecordBeforeDiscard:
    def test_the_status_comes_from_the_body_not_from_the_failure_path(self, scheduler):
        """The agent completed the run; only the recording of it failed."""
        assert ScheduledRunRecorder.record_before_discard(json.dumps({"result": "tick", "scheduled_run": SCHEDULED_RUN})) is True

        kwargs = scheduler.mark_run_completed.call_args.kwargs
        assert kwargs["status"].value == "COMPLETED"
        assert kwargs["last_error"] is None

    def test_a_body_carrying_an_error_is_still_recorded_as_failed(self, scheduler):
        ScheduledRunRecorder.record_before_discard(json.dumps({"error": "agent blew up", "scheduled_run": SCHEDULED_RUN}))

        kwargs = scheduler.mark_run_completed.call_args.kwargs
        assert kwargs["status"].value == "FAILED"
        assert kwargs["last_error"] == "agent blew up"

    def test_an_ordinary_body_is_left_to_the_caller(self, scheduler):
        assert ScheduledRunRecorder.record_before_discard(json.dumps({"result": "hi", "session_id": "s1"})) is False
        scheduler.mark_run_completed.assert_not_called()

    @pytest.mark.parametrize("raw_body", ["not json at all", "", None, json.dumps(["a", "list"])])
    def test_an_unparseable_body_is_left_to_the_caller(self, scheduler, raw_body):
        assert ScheduledRunRecorder.record_before_discard(raw_body) is False
        scheduler.mark_run_completed.assert_not_called()

    def test_a_malformed_block_never_raises_on_this_path(self, scheduler):
        """The contract that separates this from record(): a body that failed every retry
        cannot be trusted, and there is no error channel left to raise into."""
        raw_body = json.dumps({"result": "tick", "scheduled_run": {"scheduled_task_id": "schedule_a"}})

        assert ScheduledRunRecorder.record_before_discard(raw_body) is False
        scheduler.mark_run_completed.assert_not_called()
        # The ordinary path deliberately surfaces the same body as a bug.
        with pytest.raises(ValidationError):
            ScheduledRunRecorder.record(raw_body)

    def test_a_failed_write_is_swallowed_and_logged(self, scheduler, caplog):
        scheduler.mark_run_completed.side_effect = RuntimeError("dynamodb unavailable")

        with caplog.at_level("ERROR"):
            assert ScheduledRunRecorder.record_before_discard(json.dumps({"result": "tick", "scheduled_run": SCHEDULED_RUN})) is True

        # True even though nothing was written: the caller must still skip broadcast and store.
        assert "Lost the outcome of a scheduled run" in caplog.text
        for identifier in ("schedule_a", "v1", "exec-1"):
            assert identifier in caplog.text
