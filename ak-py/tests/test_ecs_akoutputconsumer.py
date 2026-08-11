import json
from unittest.mock import MagicMock, patch

import pytest

from agentkernel.core.model import ExecutionMode
from agentkernel.deployment.aws.containerized.akoutputconsumer import ECSOutputConsumer
from agentkernel.deployment.aws.core.websocket_service import AWSWebSocketHandler


def _make_record(
    body: dict,
    message_id: str = "m1",
    message_group_id: str = "session-1",
    request_id: str = "req-1",
    user_id: str = "user-1",
    endpoint_url: str = "https://example.execute-api.us-east-1.amazonaws.com/prod",
):
    attrs = {}
    if request_id:
        attrs["request_id"] = {"StringValue": request_id, "DataType": "String"}
    if user_id:
        attrs["user_id"] = {"StringValue": user_id, "DataType": "String"}
    if endpoint_url:
        attrs["endpoint_url"] = {"StringValue": endpoint_url, "DataType": "String"}
    return {
        "MessageId": message_id,
        "Body": json.dumps(body),
        "Attributes": {"MessageGroupId": message_group_id},
        "MessageAttributes": attrs,
    }


@pytest.fixture(autouse=True)
def _reset_websocket_handler():
    ECSOutputConsumer._websocket_handler = None
    yield
    ECSOutputConsumer._websocket_handler = None


@pytest.fixture
def _stream_mode(monkeypatch):
    monkeypatch.setattr(ECSOutputConsumer._config.execution, "mode", ExecutionMode.STREAM)


@pytest.fixture
def _async_mode(monkeypatch):
    monkeypatch.setattr(ECSOutputConsumer._config.execution, "mode", ExecutionMode.ASYNC)


def test_broadcast_via_websocket_raises_when_endpoint_url_missing():
    record = _make_record({"delta": "hi"}, endpoint_url=None)
    with pytest.raises(ValueError, match="endpoint_url is required"):
        ECSOutputConsumer._broadcast_via_websocket(record, message_type=AWSWebSocketHandler.MessageType.STREAM_CHUNK)


def test_broadcast_via_websocket_raises_when_user_id_missing():
    record = _make_record({"delta": "hi"}, user_id=None)
    with pytest.raises(ValueError, match="user_id is required"):
        ECSOutputConsumer._broadcast_via_websocket(record, message_type=AWSWebSocketHandler.MessageType.STREAM_CHUNK)


def test_broadcast_via_websocket_uses_given_message_type():
    record = _make_record({"delta": "hi", "done": False})
    mock_ws_handler = MagicMock()
    with patch.object(ECSOutputConsumer, "_get_websocket_handler", return_value=mock_ws_handler):
        ECSOutputConsumer._broadcast_via_websocket(record, message_type=AWSWebSocketHandler.MessageType.STREAM_CHUNK)

    mock_ws_handler.broadcast.assert_called_once()
    kwargs = mock_ws_handler.broadcast.call_args.kwargs
    assert kwargs["message_type"] == AWSWebSocketHandler.MessageType.STREAM_CHUNK
    assert kwargs["message"] == {"delta": "hi", "done": False}
    assert kwargs["user_id"] == "user-1"
    assert kwargs["endpoint_url"] == "https://example.execute-api.us-east-1.amazonaws.com/prod"


def test_process_message_stream_mode_broadcasts_stream_chunk(_stream_mode):
    record = _make_record({"delta": "token", "done": False})
    mock_ws_handler = MagicMock()
    mock_store = MagicMock()

    with (
        patch.object(ECSOutputConsumer, "_get_websocket_handler", return_value=mock_ws_handler),
        patch.object(ECSOutputConsumer, "_get_response_store", return_value=mock_store),
    ):
        ECSOutputConsumer.process_message(record)

    mock_ws_handler.broadcast.assert_called_once()
    assert mock_ws_handler.broadcast.call_args.kwargs["message_type"] == AWSWebSocketHandler.MessageType.STREAM_CHUNK
    mock_store.add_message.assert_not_called()


def test_process_message_async_mode_still_broadcasts_chat_response(_async_mode):
    record = _make_record({"result": "ok"})
    mock_ws_handler = MagicMock()

    with patch.object(ECSOutputConsumer, "_get_websocket_handler", return_value=mock_ws_handler):
        ECSOutputConsumer.process_message(record)

    mock_ws_handler.broadcast.assert_called_once()
    assert mock_ws_handler.broadcast.call_args.kwargs["message_type"] == AWSWebSocketHandler.MessageType.CHAT_RESPONSE


def test_on_permanent_failure_async_mode_broadcasts_system_response(_async_mode):
    """Regression: ASYNC permanent-failure errors must stay SYSTEM_RESPONSE, not collapse to CHAT_RESPONSE."""
    record = _make_record({"result": "ok"})
    mock_ws_handler = MagicMock()

    with patch.object(ECSOutputConsumer, "_get_websocket_handler", return_value=mock_ws_handler):
        ECSOutputConsumer.on_permanent_failure(record)

    mock_ws_handler.broadcast.assert_called_once()
    kwargs = mock_ws_handler.broadcast.call_args.kwargs
    assert kwargs["message_type"] == AWSWebSocketHandler.MessageType.SYSTEM_RESPONSE
    assert kwargs["message"]["error"] is not None
    assert kwargs["message"]["session_id"] == "session-1"


def test_on_permanent_failure_stream_mode_broadcasts_error_chunk(_stream_mode):
    record = _make_record({"delta": "token"})
    mock_ws_handler = MagicMock()

    with patch.object(ECSOutputConsumer, "_get_websocket_handler", return_value=mock_ws_handler):
        ECSOutputConsumer.on_permanent_failure(record)

    mock_ws_handler.broadcast.assert_called_once()
    kwargs = mock_ws_handler.broadcast.call_args.kwargs
    assert kwargs["message_type"] == AWSWebSocketHandler.MessageType.STREAM_CHUNK
    assert kwargs["message"]["error"] is not None
    assert kwargs["message"]["done"] is True
    assert kwargs["message"]["session_id"] == "session-1"


def test_on_permanent_failure_stream_mode_warns_when_endpoint_url_missing(_stream_mode, caplog):
    record = _make_record({"delta": "token"}, endpoint_url=None)
    mock_ws_handler = MagicMock()

    with patch.object(ECSOutputConsumer, "_get_websocket_handler", return_value=mock_ws_handler):
        with caplog.at_level("WARNING"):
            ECSOutputConsumer.on_permanent_failure(record)

    mock_ws_handler.broadcast.assert_not_called()
    assert any("endpoint_url or user_id missing" in r.message for r in caplog.records)


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

    def _record(self, **body_overrides):
        body = {"result": "the report", "session_id": "schedule:schedule_a:2026-08-09T09:00:00Z", "scheduled_run": self.SCHEDULED_RUN}
        body.update(body_overrides)
        return _make_record(body)

    def test_a_successful_run_is_recorded_and_not_stored(self, scheduler):
        with patch.object(ECSOutputConsumer, "_get_response_store") as store:
            ECSOutputConsumer.process_message(self._record())

        store.assert_not_called()
        kwargs = scheduler.mark_run_completed.call_args.kwargs
        assert kwargs["scheduled_task_id"] == "schedule_a"
        assert kwargs["status"].value == "COMPLETED"

    def test_an_errored_run_is_recorded_as_failed_with_its_error(self, scheduler):
        record = _make_record({"error": "agent blew up", "scheduled_run": self.SCHEDULED_RUN})
        ECSOutputConsumer.process_message(record)

        kwargs = scheduler.mark_run_completed.call_args.kwargs
        assert kwargs["status"].value == "FAILED"
        assert kwargs["last_error"] == "agent blew up"

    @pytest.mark.parametrize("mode", [ExecutionMode.ASYNC, ExecutionMode.STREAM])
    def test_a_timer_originated_message_is_not_broadcast(self, scheduler, monkeypatch, mode):
        """The regression guard: a fire carries no endpoint_url, so a broadcast would raise."""
        monkeypatch.setattr(ECSOutputConsumer._config.execution, "mode", mode)
        record = _make_record(
            {"result": "the report", "scheduled_run": self.SCHEDULED_RUN},
            endpoint_url=None,
        )

        ECSOutputConsumer.process_message(record)  # must not raise

        scheduler.mark_run_completed.assert_called_once()

    def test_an_ordinary_response_is_still_stored(self, scheduler):
        with patch.object(ECSOutputConsumer, "_get_response_store") as store:
            ECSOutputConsumer.process_message(_make_record({"result": "hi", "session_id": "s1"}))

        store.return_value.add_message.assert_called_once()

    def test_a_retry_exhausted_run_gets_one_last_recording_attempt(self, scheduler):
        """on_permanent_failure is the last code to see the message, so the outcome is written
        there or lost — and the status still comes from the body, not from the failure path."""
        with patch.object(ECSOutputConsumer, "_get_response_store") as store:
            ECSOutputConsumer.on_permanent_failure(self._record())

        store.assert_not_called()
        kwargs = scheduler.mark_run_completed.call_args.kwargs
        assert kwargs["scheduled_task_id"] == "schedule_a"
        assert kwargs["status"].value == "COMPLETED"

    def test_a_retry_exhausted_run_that_cannot_be_recorded_is_logged_not_raised(self, scheduler, caplog):
        scheduler.mark_run_completed.side_effect = RuntimeError("dynamodb unavailable")

        with caplog.at_level("ERROR"):
            ECSOutputConsumer.on_permanent_failure(self._record())  # must not raise

        assert "Lost the outcome of a scheduled run" in caplog.text
        assert "schedule_a" in caplog.text
        assert "exec-1" in caplog.text

    def test_an_ordinary_permanent_failure_still_reaches_the_response_store(self, scheduler):
        with patch.object(ECSOutputConsumer, "_get_response_store") as store:
            ECSOutputConsumer.on_permanent_failure(_make_record({"result": "hi", "session_id": "s1"}))

        store.return_value.add_message.assert_called_once()
        scheduler.mark_run_completed.assert_not_called()
        scheduler.mark_run_completed.assert_not_called()
