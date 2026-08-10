import json
from unittest.mock import MagicMock, patch

import pytest

from agentkernel.deployment.aws.serverless.akresponsehandler import ResponseHandler


def test_construct_message_for_store_uses_request_id_from_message_attributes():
    record = {
        "body": json.dumps({"session_id": "session-1", "request_id": "body-request", "result": "ok"}),
        "messageAttributes": {
            "request_id": {"StringValue": "attr-request", "DataType": "String"},
        },
    }

    message = ResponseHandler._construct_message_for_store(record)

    assert message["session_id"] == "session-1"
    assert message["request_id"] == "attr-request"
    assert message["body"] == {"session_id": "session-1", "request_id": "body-request", "result": "ok"}


def test_construct_message_for_store_rejects_request_id_in_body_only():
    record = {
        "body": json.dumps({"session_id": "session-1", "request_id": "body-request", "result": "ok"}),
        "messageAttributes": {},
    }

    with pytest.raises(ValueError, match="request_id is required in SQS message attributes"):
        ResponseHandler._construct_message_for_store(record)


def test_broadcast_stream_chunk_raises_when_endpoint_url_missing():
    record = {
        "body": json.dumps({"delta": "hello", "done": False, "session_id": "s1"}),
        "messageAttributes": {
            "user_id": {"StringValue": "user-1", "DataType": "String"},
        },
    }
    with pytest.raises(ValueError, match="endpoint_url is required"):
        ResponseHandler._broadcast_via_websocket(record)


def test_broadcast_stream_chunk_raises_when_user_id_missing():
    record = {
        "body": json.dumps({"delta": "hello", "done": False, "session_id": "s1"}),
        "messageAttributes": {
            "endpoint_url": {"StringValue": "https://example.execute-api.us-east-1.amazonaws.com/prod", "DataType": "String"},
        },
    }
    with pytest.raises(ValueError, match="user_id is required"):
        ResponseHandler._broadcast_via_websocket(record)


def test_broadcast_stream_chunk_wraps_with_type_and_broadcasts():
    from agentkernel.deployment.aws.serverless.core.router.ws_lambda import LambdaWSHandler

    record = {
        "body": json.dumps({"delta": "hello", "done": False, "session_id": "s1"}),
        "messageAttributes": {
            "endpoint_url": {"StringValue": "https://example.execute-api.us-east-1.amazonaws.com/prod", "DataType": "String"},
            "user_id": {"StringValue": "user-1", "DataType": "String"},
        },
    }

    mock_ws_handler = MagicMock()
    with patch.object(ResponseHandler, "_get_base_ws_handler", return_value=mock_ws_handler):
        ResponseHandler._broadcast_via_websocket(record, message_type=LambdaWSHandler.MessageType.STREAM_CHUNK)

    mock_ws_handler.broadcast.assert_called_once()
    call_kwargs = mock_ws_handler.broadcast.call_args
    endpoint_url = call_kwargs.kwargs["endpoint_url"]
    user_id = call_kwargs.kwargs["user_id"]
    message_type = call_kwargs.kwargs["message_type"]
    broadcasted_message = call_kwargs.kwargs["message"]
    assert endpoint_url == "https://example.execute-api.us-east-1.amazonaws.com/prod"
    assert user_id == "user-1"
    assert message_type == LambdaWSHandler.MessageType.STREAM_CHUNK
    assert broadcasted_message["delta"] == "hello"
    assert broadcasted_message["done"] is False


@patch("agentkernel.deployment.aws.serverless.akresponsehandler.AKConfig")
def test_process_message_stream_mode_calls_broadcast_stream_chunk(mock_config_cls):
    from agentkernel.core.model import ExecutionMode
    from agentkernel.deployment.aws.serverless.core.router.ws_lambda import LambdaWSHandler

    mock_config = MagicMock()
    mock_config.execution.mode = ExecutionMode.STREAM
    mock_config_cls.get.return_value = mock_config

    ResponseHandler._config = mock_config

    record = {
        "body": json.dumps({"delta": "token", "done": False, "session_id": "s1"}),
        "messageAttributes": {
            "endpoint_url": {"StringValue": "https://example.execute-api.us-east-1.amazonaws.com/prod", "DataType": "String"},
            "user_id": {"StringValue": "user-1", "DataType": "String"},
            "request_id": {"StringValue": "req-1", "DataType": "String"},
        },
    }

    mock_ws_handler = MagicMock()
    with patch.object(ResponseHandler, "_get_base_ws_handler", return_value=mock_ws_handler):
        ResponseHandler.process_message(record)

    mock_ws_handler.broadcast.assert_called_once()
    call_kwargs = mock_ws_handler.broadcast.call_args
    message_type = call_kwargs.kwargs["message_type"]
    broadcasted = call_kwargs.kwargs["message"]
    assert message_type == LambdaWSHandler.MessageType.STREAM_CHUNK
    assert broadcasted["delta"] == "token"


class TestScheduledRunOutcomes:
    """A scheduled run has no live client channel and nobody polls for its response."""

    SCHEDULED_RUN = {
        "scheduled_task_id": "schedule_a",
        "scheduled_task_version": "v1",
        "scheduled_time": "2026-08-09T09:00:00Z",
        "run_id": "exec-1",
    }

    @pytest.fixture
    def scheduler(self):
        with patch("agentkernel.deployment.common.scheduled_run_recorder.SchedulerFactory") as factory:
            yield factory.build.return_value

    def _record(self, body_overrides=None, with_endpoint_url=True):
        body = {"result": "the report", "session_id": "schedule:schedule_a:2026-08-09T09:00:00Z", "scheduled_run": self.SCHEDULED_RUN}
        body.update(body_overrides or {})
        attributes = {"request_id": {"StringValue": "req-1", "DataType": "String"}}
        if with_endpoint_url:
            attributes["endpoint_url"] = {"StringValue": "https://example.execute-api.us-east-1.amazonaws.com/prod", "DataType": "String"}
            attributes["user_id"] = {"StringValue": "user-1", "DataType": "String"}
        return {"body": json.dumps(body), "messageAttributes": attributes}

    def test_a_successful_run_is_recorded_and_not_stored(self, scheduler):
        with patch.object(ResponseHandler, "_get_response_store") as store:
            ResponseHandler.process_message(self._record())

        store.assert_not_called()
        kwargs = scheduler.mark_run_completed.call_args.kwargs
        assert kwargs["scheduled_task_id"] == "schedule_a"
        assert kwargs["status"].value == "COMPLETED"

    def test_an_errored_run_is_recorded_as_failed_with_its_error(self, scheduler):
        ResponseHandler.process_message(self._record({"error": "agent blew up"}))

        kwargs = scheduler.mark_run_completed.call_args.kwargs
        assert kwargs["status"].value == "FAILED"
        assert kwargs["last_error"] == "agent blew up"

    @pytest.mark.parametrize("mode", ["async", "stream"])
    def test_a_timer_originated_message_is_not_broadcast(self, scheduler, mode):
        """The regression guard: a fire carries no endpoint_url, so a broadcast would raise."""
        from agentkernel.core.model import ExecutionMode

        mock_config = MagicMock()
        mock_config.execution.mode = ExecutionMode(mode)
        with patch("agentkernel.deployment.aws.serverless.akresponsehandler.AKConfig") as config_cls:
            config_cls.get.return_value = mock_config
            with patch.object(ResponseHandler, "_get_base_ws_handler") as ws_handler:
                ResponseHandler.process_message(self._record(with_endpoint_url=False))  # must not raise

        ws_handler.assert_not_called()
        scheduler.mark_run_completed.assert_called_once()

    def test_an_ordinary_response_is_still_stored(self, scheduler):
        record = {
            "body": json.dumps({"result": "hi", "session_id": "s1"}),
            "messageAttributes": {"request_id": {"StringValue": "req-1", "DataType": "String"}},
        }
        with patch("agentkernel.deployment.aws.serverless.akresponsehandler.AKConfig") as config_cls:
            config_cls.get.return_value = MagicMock()
            with patch.object(ResponseHandler, "_get_response_store") as store:
                ResponseHandler.process_message(record)

        store.return_value.add_message.assert_called_once()
        scheduler.mark_run_completed.assert_not_called()
