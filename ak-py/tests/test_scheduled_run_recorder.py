"""The shared recognition step both output consumers use.

``record`` runs on the ordinary path and may raise; ``record_before_discard`` runs from
``on_permanent_failure``, which has no error channel left, so it must never raise.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agentkernel.deployment.common.scheduled_run_recorder import STALE_RUN_WARNING_SECONDS, ScheduledRunRecorder

SCHEDULED_RUN = {
    "scheduled_task_id": "schedule_a",
    "scheduled_task_version": "v1",
    "scheduled_time": "2026-08-09T09:00:00Z",
    "run_id": "exec-1",
}


def _body_fired_at(moment: datetime) -> str:
    """A response body for a run scheduled at ``moment``."""
    return json.dumps({"result": "tick", "scheduled_run": {**SCHEDULED_RUN, "scheduled_time": moment.isoformat()}})


@pytest.fixture
def scheduler():
    with patch("agentkernel.deployment.common.scheduled_run_recorder.SchedulerFactory") as factory:
        yield factory.build.return_value


class TestOutcomeLogging:
    """The log lines an operator reads outcomes through must not overstate what happened."""

    def test_a_guard_rejected_write_is_not_reported_as_recorded(self, scheduler, caplog):
        """The provider already logged why it refused; claiming success would contradict it."""
        scheduler.mark_run_completed.return_value = False

        with caplog.at_level("INFO"):
            assert ScheduledRunRecorder.record(_body_fired_at(datetime.now(timezone.utc))) is True

        assert "Recorded scheduled run outcome" not in caplog.text

    def test_an_accepted_write_is_reported(self, scheduler, caplog):
        scheduler.mark_run_completed.return_value = True

        with caplog.at_level("INFO"):
            ScheduledRunRecorder.record(_body_fired_at(datetime.now(timezone.utc)))

        assert "Recorded scheduled run outcome" in caplog.text


class TestStaleness:
    """A late fire is still a real run, so it is recorded — but it must be visible as late."""

    def test_a_run_far_past_its_scheduled_time_is_logged(self, scheduler, caplog):
        stale = datetime.now(timezone.utc) - timedelta(seconds=STALE_RUN_WARNING_SECONDS + 60)

        with caplog.at_level("WARNING"):
            ScheduledRunRecorder.record(_body_fired_at(stale))

        assert "Scheduled run outcome is" in caplog.text
        assert "schedule_a" in caplog.text
        # Recorded anyway: the outcome belongs on the row either way.
        scheduler.mark_run_completed.assert_called_once()

    def test_an_on_time_run_is_not_logged_as_late(self, scheduler, caplog):
        with caplog.at_level("WARNING"):
            ScheduledRunRecorder.record(_body_fired_at(datetime.now(timezone.utc) - timedelta(seconds=5)))

        assert "Scheduled run outcome is" not in caplog.text

    def test_a_naive_fire_time_does_not_break_the_comparison(self, scheduler):
        """The timer writes whatever it writes; a naive instant must read as UTC, not raise."""
        naive = datetime.now(timezone.utc).replace(tzinfo=None)

        assert ScheduledRunRecorder.record(_body_fired_at(naive)) is True


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
        # record() raises on the same body, because on the ordinary path it is a bug.
        with pytest.raises(ValidationError):
            ScheduledRunRecorder.record(raw_body)

    def test_a_failed_write_is_swallowed_and_logged(self, scheduler, caplog):
        scheduler.mark_run_completed.side_effect = RuntimeError("dynamodb unavailable")

        # Still True even though nothing was written: the caller must skip broadcast and store.
        with caplog.at_level("ERROR"):
            assert ScheduledRunRecorder.record_before_discard(json.dumps({"result": "tick", "scheduled_run": SCHEDULED_RUN})) is True

        assert "Lost the outcome of a scheduled run" in caplog.text
        for identifier in ("schedule_a", "v1", "exec-1"):
            assert identifier in caplog.text
