import json
from unittest.mock import patch

import pytest
from conftest_scheduler import enable_scheduler_config, install_scheduler, reset_scheduler_config

from agentkernel.core.model import BaseRequest, BaseRunRequest
from agentkernel.deployment.aws.core.sqs_handler import SQSHandler
from agentkernel.deployment.aws.serverless.core.router.rest_lambda import DefaultEndpointsHandler
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore


def test_base_request_from_nested_payload_generates_request_id_and_body():
    payload = {
        "user_id": "user-1",
        "route": "route-1",
        "body": {
            "prompt": "hello",
            "session_id": "session-1",
            "agent": "openai",
        },
    }

    request = BaseRequest.from_payload(payload)

    assert request.request_id is not None
    assert request.user_id == "user-1"
    assert request.route == "route-1"
    assert request.body is not None
    assert request.body.prompt == "hello"
    assert request.body.session_id == "session-1"
    assert request.body.agent == "openai"


def test_base_request_from_flat_payload_excludes_envelope_fields_from_body():
    payload = {
        "request_id": "request-1",
        "user_id": "user-1",
        "route": "route-1",
        "prompt": "hello",
        "session_id": "session-1",
        "agent": "openai",
    }

    request = BaseRequest.from_payload(payload)

    assert request.request_id == "request-1"
    assert request.user_id == "user-1"
    assert request.route == "route-1"
    assert request.body is not None
    assert request.body.prompt == "hello"
    assert request.body.session_id == "session-1"
    assert request.body.agent == "openai"
    assert "request_id" not in request.body.model_dump()
    # user_id is a declared body field (thread support) — the envelope value is propagated into it
    assert request.body.user_id == "user-1"
    assert "route" not in request.body.model_dump()


def test_base_request_from_envelope_only_payload_keeps_body_empty():
    payload = {
        "user_id": "user-1",
        "request_id": "request-1",
        "route": "route-1",
    }

    request = BaseRequest.from_payload(payload)

    assert request.request_id == "request-1"
    assert request.user_id == "user-1"
    assert request.route == "route-1"
    assert request.body is None


def test_base_request_from_user_only_payload_generates_request_id():
    payload = {
        "user_id": "user-1",
        "route": "route-1",
    }

    request = BaseRequest.from_payload(payload)

    assert request.request_id is not None
    assert request.user_id == "user-1"
    assert request.route == "route-1"
    assert request.body is None


def test_base_request_from_flat_run_payload_generates_body():
    payload = {
        "prompt": "hello",
        "session_id": "session-1",
        "agent": "openai",
    }

    request = BaseRequest.from_payload(payload)

    assert request.request_id is not None
    assert request.user_id is None
    assert request.route is None
    assert request.body is not None
    assert request.body.prompt == "hello"
    assert request.body.session_id == "session-1"
    assert request.body.agent == "openai"


def test_sqs_handler_build_send_message_kwargs_serializes_body_and_attributes():
    request_body = BaseRunRequest(prompt="hello", session_id="session-1", agent="openai")
    kwargs = SQSHandler.build_send_message_kwargs(
        message_body=request_body,
        message_group_id="session-1",
        message_deduplication_id="request-1",
        message_attributes=[
            SQSHandler.CustomAttribute(
                name="request_id",
                value="request-1",
                datatype=SQSHandler.AttributeDataType.STRING,
            )
        ],
    )

    assert kwargs["MessageBody"] == '{"prompt": "hello", "agent": "openai", "session_id": "session-1"}'
    assert kwargs["MessageGroupId"] == "session-1"
    assert kwargs["MessageDeduplicationId"] == "request-1"
    assert kwargs["MessageAttributes"]["request_id"]["StringValue"] == "request-1"


def test_sqs_handler_get_message_system_attributes_returns_system_attributes():
    record = {
        "attributes": {
            "MessageGroupId": "session-1",
            "MessageDeduplicationId": "request-1",
        }
    }

    attributes = SQSHandler.get_message_system_attributes(record)

    assert attributes == {"MessageGroupId": "session-1", "MessageDeduplicationId": "request-1"}


def test_sqs_handler_get_message_custom_attributes_flattens_message_attributes():
    record = {
        "messageAttributes": {
            "request_id": {"stringValue": "request-1", "DataType": "String"},
            "user_id": {"StringValue": "user-1", "DataType": "String"},
        }
    }

    attributes = SQSHandler.get_message_custom_attributes(record)

    assert attributes == {"request_id": "request-1", "user_id": "user-1"}


def test_base_request_route_filtered_from_nested_body():
    """Test that route is filtered out when present in nested body dict."""
    payload = {
        "user_id": "user-1",
        "route": "route-1",
        "body": {
            "prompt": "hello",
            "route": "should-be-filtered",
            "session_id": "session-1",
        },
    }

    request = BaseRequest.from_payload(payload)

    assert request.route == "route-1"
    assert request.body is not None
    assert request.body.prompt == "hello"
    assert request.body.session_id == "session-1"
    assert "route" not in request.body.model_dump()


class TestServerlessScheduleCreate:
    """A payload carrying a `schedule` block is registered, never enqueued."""

    @pytest.fixture(autouse=True)
    def _scheduler_config(self):
        enable_scheduler_config()
        install_scheduler(InMemoryScheduledTaskStore())
        yield
        reset_scheduler_config()

    @pytest.fixture
    def handler(self):
        with patch("agentkernel.deployment.aws.serverless.core.router.rest_lambda.ResponseDBHandler"):
            return DefaultEndpointsHandler()

    def _event(self, body: dict, principal_id: str | None = "u1") -> dict:
        request_context = {"authorizer": {"principalId": principal_id}} if principal_id else {}
        return {"httpMethod": "POST", "body": json.dumps(body), "requestContext": request_context}

    def _schedule_body(self, **overrides) -> dict:
        body = {"prompt": "run the report", "schedule": {"rate": "1 hour", "id": "a"}}
        body.update(overrides)
        return body

    def test_a_scheduled_payload_is_registered_and_never_enqueued(self, handler):
        with patch("agentkernel.deployment.aws.serverless.core.router.rest_lambda.SQSHandler") as sqs:
            status, body = handler._handle_rest_sync(self._event(self._schedule_body()), context=None)

        assert status == 201
        assert body["status"] == "SCHEDULED"
        assert body["scheduled_task_id"] == "a"
        sqs.send_message_to_input_queue.assert_not_called()

    def test_the_owner_comes_from_the_authorizer_context(self, handler):
        handler._handle_rest_sync(self._event(self._schedule_body()), context=None)
        assert handler._schedule_service.get("a", owner_id="u1").owner_id == "u1"

    def test_a_request_with_no_authorizer_context_is_rejected(self, handler):
        """Python cannot observe whether Terraform attached the authorizer, so the
        identity requirement is enforced per request here."""
        status, body = handler._handle_rest_sync(self._event(self._schedule_body(), principal_id=None), context=None)

        assert status == 401
        assert "authenticated caller" in body["error"]

    def test_the_async_submit_path_registers_too(self, handler):
        with patch("agentkernel.deployment.aws.serverless.core.router.rest_lambda.SQSHandler") as sqs:
            status, body = handler._handle_async_submit(self._event(self._schedule_body()), context=None)

        assert status == 201
        sqs.send_message_to_input_queue.assert_not_called()

    def test_a_too_fine_schedule_is_rejected(self, handler):
        status, body = handler._handle_rest_sync(self._event({"prompt": "hi", "schedule": {"rate": "10 seconds"}}), context=None)
        assert status == 400

    def test_an_ordinary_payload_is_still_enqueued(self, handler):
        with patch("agentkernel.deployment.aws.serverless.core.router.rest_lambda.SQSHandler") as sqs:
            handler._handle_async_submit(self._event({"prompt": "hi", "session_id": "s1"}), context=None)

        sqs.send_message_to_input_queue.assert_called_once()

    def test_a_scheduled_payload_is_rejected_when_scheduling_is_disabled(self, handler):
        handler._schedule_service = None
        status, body = handler._handle_rest_sync(self._event(self._schedule_body()), context=None)

        assert status == 400
        assert "not enabled" in body["error"]

    def test_another_owners_live_row_is_403(self, handler):
        handler._handle_rest_sync(self._event(self._schedule_body()), context=None)

        status, _ = handler._handle_rest_sync(self._event(self._schedule_body(), principal_id="u2"), context=None)

        assert status == 403

    def test_a_soft_deleted_id_is_409(self, handler):
        handler._handle_rest_sync(self._event(self._schedule_body()), context=None)
        handler._schedule_service.delete("a", owner_id="u1")

        status, _ = handler._handle_rest_sync(self._event(self._schedule_body()), context=None)

        assert status == 409

    def test_an_unparseable_schedule_block_is_400(self, handler):
        """Pydantic rejects it before the schedule branch runs, but the caller is still owed why."""
        status, body = handler._handle_rest_sync(self._event({"prompt": "hi", "schedule": {"rate": "1 hour", "cron": "0 9 * * ? *"}}), context=None)

        assert status == 400
        assert "exactly one" in body["error"]


class TestOrdinaryRequestsAreUnaffected:
    """The scheduling branch's statuses must not leak onto requests that carry no schedule."""

    @pytest.fixture(autouse=True)
    def _scheduler_config(self):
        enable_scheduler_config()
        install_scheduler(InMemoryScheduledTaskStore())
        yield
        reset_scheduler_config()

    @pytest.fixture
    def handler(self):
        with patch("agentkernel.deployment.aws.serverless.core.router.rest_lambda.ResponseDBHandler"):
            return DefaultEndpointsHandler()

    def test_a_body_missing_its_session_id_still_answers_500(self, handler):
        """The pre-change behaviour: _send_to_queue's ValueError is not a schedule rejection."""
        event = {"httpMethod": "POST", "body": json.dumps({"prompt": "hi"}), "requestContext": {}}

        status, body = handler._handle_async_submit(event, context=None)

        assert status == 500
        assert body["error"] == "An unexpected error occurred"

    def test_an_unparseable_ordinary_body_still_answers_500_without_echoing_the_reason(self, handler):
        event = {"httpMethod": "POST", "body": "{not json", "requestContext": {}}

        status, body = handler._handle_async_submit(event, context=None)

        assert status == 500
        assert body["error"] == "An unexpected error occurred"
