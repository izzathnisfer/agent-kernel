"""Both non-stream agent runners' on_permanent_failure paths.

A retry-exhausted run must still reach the output consumer carrying its ``scheduled_run``
block — that is what makes it recordable as FAILED with no DLQ processing.
"""

import json
from unittest.mock import patch

import pytest

from agentkernel.deployment.aws.containerized.akagentrunner import ECSAgentRunner
from agentkernel.deployment.aws.serverless.akagentrunner import ServerlessAgentRunner

SCHEDULED_RUN = {
    "scheduled_task_id": "schedule_a",
    "scheduled_task_version": "v1",
    "scheduled_time": "2026-08-09T09:00:00Z",
    "run_id": "exec-1",
}


def _ecs_record(body, message_group_id: str = "schedule_a") -> dict:
    return {
        "MessageId": "m1",
        "Body": body if isinstance(body, str) else json.dumps(body),
        "Attributes": {"MessageGroupId": message_group_id, "MessageDeduplicationId": "d1"},
        "MessageAttributes": {
            "request_id": {"StringValue": "req-1", "DataType": "String"},
            "user_id": {"StringValue": "u1", "DataType": "String"},
        },
    }


def _serverless_record(body, message_group_id: str = "schedule_a") -> dict:
    return {
        "messageId": "m1",
        "body": body if isinstance(body, str) else json.dumps(body),
        "attributes": {"MessageGroupId": message_group_id, "MessageDeduplicationId": "d1"},
        "messageAttributes": {
            "request_id": {"stringValue": "req-1", "dataType": "String"},
            "user_id": {"stringValue": "u1", "dataType": "String"},
        },
    }


def _fire_body() -> dict:
    return {
        "prompt": "run the report",
        "user_id": "u1",
        "session_id": "schedule:schedule_a:2026-08-09T09:00:00Z",
        "scheduled_run": SCHEDULED_RUN,
    }


@pytest.fixture
def ecs_sent():
    with patch.object(ECSAgentRunner, "_send_to_output_queue") as sender:
        yield sender


@pytest.fixture
def serverless_sent():
    with patch.object(ServerlessAgentRunner, "_send_to_output_queue") as sender:
        yield sender


class TestECSAgentRunner:
    def test_a_failed_fire_echoes_its_correlation_block(self, ecs_sent):
        ECSAgentRunner.on_permanent_failure(_ecs_record(_fire_body()))

        body = ecs_sent.call_args.kwargs["message_body"]
        assert body["scheduled_run"] == SCHEDULED_RUN
        assert "error" in body

    def test_an_ordinary_failure_carries_no_correlation_block(self, ecs_sent):
        ECSAgentRunner.on_permanent_failure(_ecs_record({"prompt": "hi", "session_id": "s1"}))
        assert "scheduled_run" not in ecs_sent.call_args.kwargs["message_body"]

    def test_an_unparseable_body_still_produces_the_error_body(self, ecs_sent):
        ECSAgentRunner.on_permanent_failure(_ecs_record("{not json"))

        body = ecs_sent.call_args.kwargs["message_body"]
        assert "error" in body
        assert "scheduled_run" not in body


class TestServerlessAgentRunner:
    def test_a_failed_fire_echoes_its_correlation_block(self, serverless_sent):
        ServerlessAgentRunner.on_permanent_failure(_serverless_record(_fire_body()))

        body = serverless_sent.call_args.kwargs["message_body"]
        assert body["scheduled_run"] == SCHEDULED_RUN

    def test_a_fires_session_id_comes_from_the_body_not_the_group_id(self, serverless_sent):
        """For a fire the group id is the scheduled_task_id, not a session id."""
        ServerlessAgentRunner.on_permanent_failure(_serverless_record(_fire_body()))

        body = serverless_sent.call_args.kwargs["message_body"]
        assert body["session_id"] == "schedule:schedule_a:2026-08-09T09:00:00Z"

    def test_an_ordinary_failure_still_takes_session_id_from_the_group_id(self, serverless_sent):
        ServerlessAgentRunner.on_permanent_failure(_serverless_record({"prompt": "hi", "session_id": "s1"}, message_group_id="s1"))

        body = serverless_sent.call_args.kwargs["message_body"]
        assert body["session_id"] == "s1"
        assert "scheduled_run" not in body

    def test_an_unparseable_body_does_not_raise(self, serverless_sent):
        ServerlessAgentRunner.on_permanent_failure(_serverless_record("{not json"))

        body = serverless_sent.call_args.kwargs["message_body"]
        assert "error" in body
        assert "scheduled_run" not in body
