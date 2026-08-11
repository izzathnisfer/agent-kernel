"""EventBridge Scheduler + SQS implementation of the ``Scheduler`` contract.

The timer's target is the deployment's input queue, not the agent: when a schedule fires,
EventBridge Scheduler puts an ordinary agent message on the queue and the existing agent
runner consumes it exactly as it would any other queued request.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from ...core.config import AKConfig
from ...core.model import SCHEDULED_SESSION_PREFIX, ScheduleMode
from ..base import Scheduler
from ..errors import SchedulerError, ScheduleValidationError
from ..expression import ScheduleExpression
from ..model import RunStatus, ScheduledTask, ScheduledTaskPage, TaskStatus
from ..store.base import ScheduledTaskStore, ScheduledTaskStoreBuilder

# EventBridge Scheduler's finest interval, for both cron and rate expressions.
MINIMUM_GRANULARITY = timedelta(minutes=1)

# SQS's FIFO deduplication window is fixed at 5 minutes, so a timer-side retry delivered
# later than that would not be deduplicated. Capping event age keeps every retry inside it.
MAX_EVENT_AGE_SECONDS = 300

# Added to the derived soft-delete TTL so the id stays reserved a little past the longest
# execution an already-enqueued fire can have.
TTL_SAFETY_MARGIN_SECONDS = 300

# Floor for the derived soft-delete TTL, so a short visibility timeout cannot shrink the
# grace window below what an in-flight run needs.
TTL_FLOOR_SECONDS = 900

# Substituted by EventBridge Scheduler into the payload at fire time, so the delivered
# message needs nothing derived by the agent runner.
SCHEDULED_TIME_VARIABLE = "<aws.scheduler.scheduled-time>"
EXECUTION_ID_VARIABLE = "<aws.scheduler.execution-id>"

# The only SQS target that can set both MessageGroupId and MessageDeduplicationId, and the
# only one that substitutes context variables into the payload.
UNIVERSAL_SQS_TARGET_ARN = "arn:aws:scheduler:::aws-sdk:sqs:sendMessage"


class AWSScheduler(Scheduler):
    """Registers schedules with EventBridge Scheduler, delivering to the input SQS queue."""

    _log = logging.getLogger("ak.scheduler.aws")

    def __init__(
        self,
        group_name: str,
        target_role_arn: str,
        input_queue_url: str,
        store: ScheduledTaskStore,
        scheduler_client: Any = None,
        sqs_client: Any = None,
        region: Optional[str] = None,
    ):
        """
        Both boto3 clients are created eagerly: boto3 clients are safe for concurrent calls
        but not concurrent creation, and this object is shared across consumer threads.

        :param group_name: EventBridge Scheduler schedule group for this deployment.
        :param target_role_arn: Role the timer assumes to send to the input queue.
        :param input_queue_url: The FIFO input queue every fire is delivered to.
        :param store: The scheduled-task store; a private collaborator, never exposed.
        :param scheduler_client: Override for the EventBridge Scheduler client (tests).
        :param sqs_client: Override for the SQS client (tests).
        :param region: AWS region; defaults to the boto3 environment default.
        """
        self._group_name = group_name
        self._target_role_arn = target_role_arn
        self._input_queue_url = input_queue_url
        self._store = store
        self._scheduler = scheduler_client if scheduler_client is not None else boto3.client("scheduler", region_name=region)
        self._sqs = sqs_client if sqs_client is not None else boto3.client("sqs", region_name=region)
        # Derived lazily: only delete() needs it, so output consumers (which only record run
        # outcomes) never need read permission on the input queue.
        self._soft_delete_ttl_seconds: Optional[int] = None
        self._log.info("AWSScheduler ready — group=%s", group_name)

    @property
    def minimum_granularity(self) -> timedelta:
        return MINIMUM_GRANULARITY

    @property
    def soft_delete_ttl_seconds(self) -> int:
        """The derived window during which a deleted scheduled task's id stays reserved.

        Derived once, on first use. Two threads racing the first read is harmless: the
        derivation is a read of queue attributes that both would resolve to the same value.

        :return: The grace window in seconds.
        :raises SchedulerError: The input queue's attributes could not be read.
        """
        if self._soft_delete_ttl_seconds is None:
            self._soft_delete_ttl_seconds = self._derive_soft_delete_ttl()
        return self._soft_delete_ttl_seconds

    # ------------------------------------------------------------------ contract

    def upsert(self, task: ScheduledTask) -> ScheduledTask:
        ScheduleExpression.validate(task.schedule, MINIMUM_GRANULARITY)

        previous = self._store.get(task.scheduled_task_id)
        self._store.put(task)
        try:
            self._register(task)
        except Exception:
            # A row without a registration would silently never fire, so restore it.
            self._restore(task.scheduled_task_id, previous)
            raise
        return task

    def delete(self, scheduled_task_id: str) -> None:
        # Deregister first to stop future fires. A fire already on the queue still runs, but
        # its outcome is discarded by the soft-delete guard.
        self._deregister(scheduled_task_id)
        if self._store.get(scheduled_task_id) is None:
            return
        self._store.soft_delete(scheduled_task_id, datetime.now(timezone.utc), self.soft_delete_ttl_seconds)

    def get(self, scheduled_task_id: str, *, include_deleted: bool = False) -> Optional[ScheduledTask]:
        task = self._store.get(scheduled_task_id)
        if task is None:
            return None
        if task.deleted and not include_deleted:
            return None
        return task

    def list(self, owner_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None) -> ScheduledTaskPage:
        return self._store.list_by_owner(owner_id, limit=limit, cursor=cursor)

    def mark_run_completed(
        self,
        scheduled_task_id: str,
        scheduled_task_version: str,
        scheduled_time: datetime,
        status: RunStatus,
        last_error: Optional[str] = None,
    ) -> bool:
        task = self._store.get(scheduled_task_id)
        rejection = self._guard_rejection(task, scheduled_task_id, scheduled_task_version, scheduled_time)
        if rejection is not None:
            self._log.warning("Discarding outcome for %s: %s", scheduled_task_id, rejection)
            return False

        fields: dict[str, Any] = {
            "last_run_at": datetime.now(timezone.utc),
            "last_run_status": status,
            "last_run_scheduled_time": ScheduleExpression.as_utc(scheduled_time),
            "last_error": last_error,
        }
        if ScheduleExpression.is_one_time(task.schedule):
            fields["status"] = TaskStatus.COMPLETED
            fields["completed_at"] = datetime.now(timezone.utc)

        # A field update, not a put: a put would rewrite the definition from a row that may be
        # stale. expected_version re-checks the incarnation, closing the gap since the get.
        return self._store.update_fields(scheduled_task_id, fields, expected_version=scheduled_task_version)

    # ------------------------------------------------------------------ guards

    @staticmethod
    def _guard_rejection(
        task: Optional[ScheduledTask],
        scheduled_task_id: str,
        scheduled_task_version: str,
        scheduled_time: datetime,
    ) -> Optional[str]:
        """Apply the four outcome-write guards, in order.

        :param task: The loaded row, or None when absent.
        :param scheduled_task_id: Identity the outcome reports for.
        :param scheduled_task_version: Incarnation token the fire carried.
        :param scheduled_time: The time the fire was scheduled for.
        :return: Why the write is rejected, or None when it is accepted.
        """
        if task is None:
            return f"no row at '{scheduled_task_id}' — it was deleted and its TTL has expired"
        if task.deleted:
            return "the scheduled task was deleted while this run was in flight"
        # Makes caller-chosen, reusable ids safe: an outcome from a deleted-and-recreated
        # task must never land on its successor's row.
        if task.scheduled_task_version != scheduled_task_version:
            return f"incarnation mismatch — row is version {task.scheduled_task_version}, outcome reports {scheduled_task_version}"
        # Defence in depth behind FIFO ordering, not the primary mechanism.
        if task.last_run_scheduled_time is not None and ScheduleExpression.as_utc(scheduled_time) < ScheduleExpression.as_utc(
            task.last_run_scheduled_time
        ):
            return f"stale scheduled time {scheduled_time.isoformat()} is older than the recorded {task.last_run_scheduled_time.isoformat()}"
        return None

    # ------------------------------------------------------------------ timer

    def _register(self, task: ScheduledTask) -> None:
        """Create or replace the task's EventBridge Scheduler registration.

        :param task: The scheduled task to register.
        :raises ScheduleValidationError: The timer rejected the request as malformed.
        """
        request = self._schedule_request(task)
        try:
            self._upsert_schedule(request)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") != "ValidationException":
                raise
            # Local validation cannot cover every EventBridge Scheduler rule. Map its
            # rejection to a validation error so it reports as bad input, not a server fault.
            raise ScheduleValidationError(f"EventBridge Scheduler rejected the schedule: {error.get('Message') or exc}") from exc

    def _upsert_schedule(self, request: dict[str, Any]) -> None:
        """Update the registration, creating it when it does not exist yet.

        :param request: The create/update request.
        """
        try:
            self._scheduler.update_schedule(**request)
            self._log.info("Updated schedule %s in group %s", request["Name"], self._group_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            self._scheduler.create_schedule(**request)
            self._log.info("Created schedule %s in group %s", request["Name"], self._group_name)

    def _deregister(self, scheduled_task_id: str) -> None:
        """Remove the task's registration. Idempotent — a one-time schedule may already be gone.

        :param scheduled_task_id: Identity of the registration to remove.
        """
        try:
            self._scheduler.delete_schedule(Name=scheduled_task_id, GroupName=self._group_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            self._log.debug("Schedule %s was already absent from group %s", scheduled_task_id, self._group_name)

    def _schedule_request(self, task: ScheduledTask) -> dict[str, Any]:
        """Build the create/update request for one scheduled task.

        :param task: The scheduled task to register.
        :return: The keyword arguments for create_schedule/update_schedule.
        """
        request: dict[str, Any] = {
            "Name": task.scheduled_task_id,
            "GroupName": self._group_name,
            "ScheduleExpression": self._schedule_expression(task),
            "ScheduleExpressionTimezone": task.schedule.timezone,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": self._target(task),
            "State": "ENABLED",
        }
        if ScheduleExpression.is_one_time(task.schedule):
            # The registration removes itself after firing, so no cleanup process is needed.
            request["ActionAfterCompletion"] = "DELETE"
        return request

    @staticmethod
    def _schedule_expression(task: ScheduledTask) -> str:
        """Render the spec as an EventBridge Scheduler expression.

        :param task: The scheduled task whose schedule to render.
        :return: A ``cron(...)``, ``rate(...)`` or ``at(...)`` expression.
        """
        spec = task.schedule
        if spec.cron is not None:
            return f"cron({spec.cron})"
        if spec.rate is not None:
            return f"rate({spec.rate.strip()})"
        return f"at({ScheduleExpression.as_utc(spec.at).strftime('%Y-%m-%dT%H:%M:%S')})"

    def _target(self, task: ScheduledTask) -> dict[str, Any]:
        """Build the universal SQS target that delivers one fire.

        :param task: The scheduled task being registered.
        :return: The Target block of the schedule request.
        """
        return {
            "Arn": UNIVERSAL_SQS_TARGET_ARN,
            "RoleArn": self._target_role_arn,
            "Input": json.dumps(
                {
                    "QueueUrl": self._input_queue_url,
                    "MessageBody": json.dumps(self._message_body(task)),
                    # Grouped by scheduled_task_id, not session_id as ordinary chat traffic is:
                    # a per-run session id changes between fires and would not serialize them.
                    "MessageGroupId": task.scheduled_task_id,
                    "MessageDeduplicationId": f"{task.scheduled_task_id}:{SCHEDULED_TIME_VARIABLE}",
                    "MessageAttributes": {
                        # Both runners raise without a request_id; the execution id makes it
                        # unique per fire.
                        "request_id": {"DataType": "String", "StringValue": EXECUTION_ID_VARIABLE},
                        "user_id": {"DataType": "String", "StringValue": task.owner_id},
                    },
                }
            ),
            "RetryPolicy": {"MaximumEventAgeInSeconds": MAX_EVENT_AGE_SECONDS},
        }

    @staticmethod
    def _message_body(task: ScheduledTask) -> dict[str, Any]:
        """Build the ordinary agent message the timer delivers.

        :param task: The scheduled task being registered.
        :return: The message template, with fire-time placeholders still in place.
        """
        body = dict(task.message)
        body["session_id"] = AWSScheduler.session_id_for(task.scheduled_task_id, task.schedule.mode)
        body["scheduled_run"] = {
            "scheduled_task_id": task.scheduled_task_id,
            "scheduled_task_version": task.scheduled_task_version,
            "scheduled_time": SCHEDULED_TIME_VARIABLE,
            "run_id": EXECUTION_ID_VARIABLE,
        }
        return body

    @staticmethod
    def session_id_for(scheduled_task_id: str, mode: ScheduleMode) -> str:
        """Resolve the session id a fire carries.

        In per-run mode the scheduled time is not known at registration, so the timer
        substitutes it at fire time; in continuous mode the value is static.

        :param scheduled_task_id: Identity of the scheduled task.
        :param mode: The task's conversation mode.
        :return: The session id, or the substitution template for per-run mode.
        """
        base = f"{SCHEDULED_SESSION_PREFIX}{scheduled_task_id}"
        if mode == ScheduleMode.CONTINUOUS:
            return base
        return f"{base}:{SCHEDULED_TIME_VARIABLE}"

    # ------------------------------------------------------------------ helpers

    def _restore(self, scheduled_task_id: str, previous: Optional[ScheduledTask]) -> None:
        """Undo a row write whose registration failed.

        :param scheduled_task_id: Identity of the row to restore.
        :param previous: The row as it was before the write, or None when it was new.
        """
        try:
            if previous is None:
                # A tombstone would block retrying the create at the same id, so a row that
                # never existed before this call is removed outright.
                self._store.remove(scheduled_task_id)
            else:
                self._store.put(previous)
        except Exception:
            self._log.exception("Failed to roll back scheduled task %s after a registration failure", scheduled_task_id)

    def _derive_soft_delete_ttl(self) -> int:
        """Size the window during which a deleted scheduled task's id stays reserved.

        Sized to outlive the longest execution an already-enqueued fire can have, so an
        in-flight run's id is not claimed by a new scheduled task. This is a convenience, not
        a correctness requirement: the incarnation guard rejects cross-incarnation outcomes
        regardless of the window.

        :return: The grace window in seconds.
        :raises SchedulerError: The queue's attributes could not be read.
        """
        try:
            attributes = self._sqs.get_queue_attributes(
                QueueUrl=self._input_queue_url,
                AttributeNames=["VisibilityTimeout", "RedrivePolicy"],
            )["Attributes"]
        except Exception as exc:
            raise SchedulerError(f"could not read attributes of input queue '{self._input_queue_url}' to derive the soft-delete TTL") from exc

        visibility_timeout = int(attributes["VisibilityTimeout"])
        redrive_policy = json.loads(attributes.get("RedrivePolicy") or "{}")
        # A deployment with no DLQ has no redrive policy, so fall back to the configured
        # count. Taking the max of both keeps the TTL an upper bound either way.
        receives = max(int(redrive_policy.get("maxReceiveCount", 0)), AKConfig.get().execution.queues.input.max_receive_count)
        return max(visibility_timeout * receives + TTL_SAFETY_MARGIN_SECONDS, TTL_FLOOR_SECONDS)


class AWSSchedulerBuilder:
    """Builds an :class:`AWSScheduler` from ``AKConfig``.

    Config reading lives here rather than in the provider, the same split the shared
    drivers and stores already follow.
    """

    @staticmethod
    def build() -> AWSScheduler:
        """Construct the provider and its store from configuration.

        :return: The configured AWS scheduler.
        """
        config = AKConfig.get()
        return AWSScheduler(
            group_name=config.scheduler.group_name,
            target_role_arn=config.scheduler.target_role_arn,
            input_queue_url=config.execution.queues.input.url,
            store=ScheduledTaskStoreBuilder.build(),
            region=config.scheduler.region,
        )


__all__ = ["AWSScheduler", "AWSSchedulerBuilder", "MINIMUM_GRANULARITY"]
