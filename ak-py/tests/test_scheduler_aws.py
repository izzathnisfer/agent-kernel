"""AWSScheduler against mocked boto3 clients and an in-memory store."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from conftest_scheduler import enable_scheduler_config, make_scheduler, reset_scheduler_config

from agentkernel.core.model import ScheduleMode
from agentkernel.scheduler.errors import ScheduleValidationError
from agentkernel.scheduler.model import RunStatus, ScheduleSpec, TaskStatus
from agentkernel.scheduler.providers.aws import MAX_EVENT_AGE_SECONDS, UNIVERSAL_SQS_TARGET_ARN, AWSScheduler
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore, SchedulerContract, build_task


@pytest.fixture(autouse=True)
def _scheduler_config():
    enable_scheduler_config()
    yield
    reset_scheduler_config()


@pytest.fixture
def store() -> InMemoryScheduledTaskStore:
    return InMemoryScheduledTaskStore()


@pytest.fixture
def scheduler(store) -> AWSScheduler:
    return make_scheduler(store)


def _registration(scheduler: AWSScheduler) -> dict:
    """Return the create/update request the scheduler last issued."""
    client = scheduler._scheduler
    call = client.update_schedule.call_args or client.create_schedule.call_args
    return call.kwargs


def _target_input(scheduler: AWSScheduler) -> dict:
    return json.loads(_registration(scheduler)["Target"]["Input"])


class TestAWSSchedulerContract(SchedulerContract):
    """The provider-agnostic obligations, run against the AWS implementation."""

    @pytest.fixture
    def scheduler(self) -> AWSScheduler:
        return make_scheduler()


class TestRegistration:
    def test_target_is_the_universal_sqs_target(self, scheduler):
        scheduler.upsert(build_task("schedule_a"))
        target = _registration(scheduler)["Target"]
        assert target["Arn"] == UNIVERSAL_SQS_TARGET_ARN
        assert target["RoleArn"] == "arn:aws:iam::1:role/timer"

    def test_fires_are_grouped_and_deduplicated_per_scheduled_time(self, scheduler):
        scheduler.upsert(build_task("schedule_a"))
        target_input = _target_input(scheduler)
        assert target_input["MessageGroupId"] == "schedule_a"
        assert target_input["MessageDeduplicationId"] == "schedule_a:<aws.scheduler.scheduled-time>"

    def test_payload_is_an_ordinary_agent_message(self, scheduler):
        task = build_task("schedule_a")
        scheduler.upsert(task)
        body = json.loads(_target_input(scheduler)["MessageBody"])
        assert body["prompt"] == "run the report"
        assert body["user_id"] == task.owner_id
        assert body["session_id"] == "schedule:schedule_a:<aws.scheduler.scheduled-time>"
        assert body["scheduled_run"] == {
            "scheduled_task_id": "schedule_a",
            "scheduled_task_version": task.scheduled_task_version,
            "scheduled_time": "<aws.scheduler.scheduled-time>",
            "run_id": "<aws.scheduler.execution-id>",
        }

    def test_continuous_mode_uses_a_static_session_id(self, scheduler):
        scheduler.upsert(build_task("schedule_a", spec=ScheduleSpec(rate="1 hour", mode=ScheduleMode.CONTINUOUS)))
        body = json.loads(_target_input(scheduler)["MessageBody"])
        assert body["session_id"] == "schedule:schedule_a"

    def test_request_id_attribute_is_unique_per_fire(self, scheduler):
        """Both runners raise without a request_id."""
        scheduler.upsert(build_task("schedule_a"))
        attributes = _target_input(scheduler)["MessageAttributes"]
        assert attributes["request_id"]["StringValue"] == "<aws.scheduler.execution-id>"

    def test_event_age_is_capped_inside_the_fifo_dedup_window(self, scheduler):
        scheduler.upsert(build_task("schedule_a"))
        assert _registration(scheduler)["Target"]["RetryPolicy"]["MaximumEventAgeInSeconds"] == MAX_EVENT_AGE_SECONDS

    @pytest.mark.parametrize(
        "spec, expression",
        [
            (ScheduleSpec(rate="30 minutes"), "rate(30 minutes)"),
            (ScheduleSpec(cron="0 9 * * ? *"), "cron(0 9 * * ? *)"),
        ],
    )
    def test_expression_is_rendered_for_eventbridge(self, scheduler, spec, expression):
        scheduler.upsert(build_task("schedule_a", spec=spec))
        assert _registration(scheduler)["ScheduleExpression"] == expression

    def test_one_time_schedules_remove_their_own_registration(self, scheduler):
        spec = ScheduleSpec(at=datetime.now(timezone.utc) + timedelta(days=1))
        scheduler.upsert(build_task("schedule_once", spec=spec))
        request = _registration(scheduler)
        assert request["ActionAfterCompletion"] == "DELETE"
        assert request["ScheduleExpression"].startswith("at(")

    def test_recurring_schedules_do_not_self_delete(self, scheduler):
        scheduler.upsert(build_task("schedule_a"))
        assert "ActionAfterCompletion" not in _registration(scheduler)

    def test_a_missing_registration_is_created_rather_than_updated(self, scheduler):
        scheduler._scheduler.update_schedule.side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "UpdateSchedule")
        scheduler.upsert(build_task("schedule_a"))
        scheduler._scheduler.create_schedule.assert_called_once()


class TestValidation:
    @pytest.mark.parametrize("spec", [ScheduleSpec(rate="30 seconds"), ScheduleSpec(cron="* * * * * * *")])
    def test_sub_minute_schedules_are_rejected_before_any_aws_call(self, scheduler, spec):
        with pytest.raises(ScheduleValidationError):
            scheduler.upsert(build_task("schedule_fine", spec=spec))
        scheduler._scheduler.create_schedule.assert_not_called()
        scheduler._scheduler.update_schedule.assert_not_called()

    @pytest.mark.parametrize(
        "spec",
        [
            ScheduleSpec(cron="cron(0 9 * * ? *)"),
            ScheduleSpec(rate="rate(1 minute)"),
        ],
    )
    def test_a_provider_wrapped_expression_is_rejected_before_any_aws_call(self, scheduler, spec):
        """The provider adds the wrapper itself; accepting a pre-wrapped one would double it."""
        with pytest.raises(ScheduleValidationError, match="bare expression"):
            scheduler.upsert(build_task("schedule_wrapped", spec=spec))
        scheduler._scheduler.create_schedule.assert_not_called()
        scheduler._scheduler.update_schedule.assert_not_called()

    def test_a_timer_side_rejection_surfaces_as_a_validation_error(self, scheduler):
        """A malformed expression is the caller's to fix, so it must not read as a server fault."""
        error = ClientError({"Error": {"Code": "ValidationException", "Message": "Invalid Schedule Expression"}}, "UpdateSchedule")
        scheduler._scheduler.update_schedule.side_effect = error

        with pytest.raises(ScheduleValidationError, match="Invalid Schedule Expression"):
            scheduler.upsert(build_task("schedule_rejected"))

    def test_a_one_time_schedule_in_the_past_is_rejected(self, scheduler):
        spec = ScheduleSpec(at=datetime.now(timezone.utc) - timedelta(minutes=1))
        with pytest.raises(ScheduleValidationError, match="not in the future"):
            scheduler.upsert(build_task("schedule_past", spec=spec))


class TestRollback:
    def test_a_failed_registration_removes_a_newly_created_row(self, scheduler, store):
        scheduler._scheduler.update_schedule.side_effect = RuntimeError("boom")
        scheduler._scheduler.create_schedule.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            scheduler.upsert(build_task("schedule_new"))

        # No tombstone either: a tombstone would block retrying the create at the same id.
        assert store.get("schedule_new") is None

    def test_a_failed_registration_restores_the_previous_row(self, scheduler, store):
        original = build_task("schedule_a")
        scheduler.upsert(original)
        scheduler._scheduler.update_schedule.side_effect = RuntimeError("boom")

        replacement = original.model_copy(update={"schedule": ScheduleSpec(rate="2 hours")})
        with pytest.raises(RuntimeError):
            scheduler.upsert(replacement)

        assert store.get("schedule_a").schedule.rate == "1 hour"


class TestOutcomeGuards:
    """mark_run_completed is a logged no-op when any guard rejects, never a retry."""

    def test_a_store_failure_propagates_rather_than_no_op(self, scheduler, store, monkeypatch):
        task = build_task("schedule_a")
        scheduler.upsert(task)

        def explode(*args, **kwargs):
            raise RuntimeError("dynamodb unavailable")

        monkeypatch.setattr(store, "update_fields", explode)
        with pytest.raises(RuntimeError):
            scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, datetime.now(timezone.utc), RunStatus.COMPLETED)

    def test_a_failed_run_records_its_error(self, scheduler):
        task = build_task("schedule_a")
        scheduler.upsert(task)
        scheduler.mark_run_completed(
            task.scheduled_task_id, task.scheduled_task_version, datetime.now(timezone.utc), RunStatus.FAILED, last_error="agent blew up"
        )
        loaded = scheduler.get("schedule_a")
        assert loaded.last_run_status == RunStatus.FAILED
        assert loaded.last_error == "agent blew up"

    def test_a_recurring_task_stays_active_after_a_run(self, scheduler):
        task = build_task("schedule_a")
        scheduler.upsert(task)
        scheduler.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, datetime.now(timezone.utc), RunStatus.COMPLETED)
        assert scheduler.get("schedule_a").status == TaskStatus.ACTIVE

    def test_an_outcome_records_without_the_input_queue(self, store, scheduler):
        """The deployment shape of the output consumers: they record outcomes but are given no
        input queue URL, and recording must not depend on one."""
        task = build_task("schedule_a")
        scheduler.upsert(task)

        sqs = MagicMock()
        sqs.get_queue_attributes.side_effect = RuntimeError("queue does not exist")
        consumer_side = AWSScheduler("grp", "arn:role", "", store, scheduler_client=MagicMock(), sqs_client=sqs)

        assert consumer_side.mark_run_completed(task.scheduled_task_id, task.scheduled_task_version, datetime.now(timezone.utc), RunStatus.COMPLETED)
        assert consumer_side.get("schedule_a").last_run_status == RunStatus.COMPLETED
