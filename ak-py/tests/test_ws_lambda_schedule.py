"""The serverless WebSocket chat route's schedule branch.

Identity needs no new mechanism here: WebSocket connections are authenticated at $connect,
so the frame's user is the scheduled task's owner.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest_scheduler import enable_scheduler_config, install_scheduler, reset_scheduler_config

from agentkernel.core.config import AKConfig, _DynamoDBConfig
from agentkernel.core.model import ExecutionMode
from agentkernel.deployment.aws.serverless.core.router.ws_lambda import LambdaWSHandler, SystemRoutesHandler
from agentkernel.scheduler.errors import SchedulerNotFoundError
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore

OWNER = "u1"


@pytest.fixture(autouse=True)
def _scheduler_config():
    config = enable_scheduler_config()
    config.websocket_api.connection_table = _DynamoDBConfig(table_name="ak-connections")
    config.websocket_api.chat_route = "chat"
    config.execution.mode = ExecutionMode.ASYNC
    install_scheduler(InMemoryScheduledTaskStore())
    yield
    config.websocket_api.connection_table = None
    config.websocket_api.chat_route = None
    config.execution.mode = ExecutionMode.REST_SYNC
    reset_scheduler_config()


@pytest.fixture
def handler() -> SystemRoutesHandler:
    with patch("agentkernel.deployment.aws.serverless.core.router.ws_lambda.WebSocketConnectionStore"):
        handler = SystemRoutesHandler()
    handler.get_user_id = MagicMock(return_value=OWNER)
    handler.broadcast = MagicMock()
    return handler


def _event(body: dict) -> dict:
    return {
        "requestContext": {"connectionId": "c1", "domainName": "abc.execute-api.us-east-1.amazonaws.com", "stage": "prod"},
        "body": json.dumps({"request_id": "r1", "body": body}),
    }


def _schedule_body(**overrides) -> dict:
    body = {"prompt": "run the report", "schedule": {"rate": "1 hour", "id": "a"}}
    body.update(overrides)
    return body


def test_a_scheduled_frame_is_registered_and_never_enqueued(handler):
    with patch("agentkernel.deployment.aws.serverless.core.router.ws_lambda.SQSHandler") as sqs:
        status, body = handler._handle_queue_mode(_event(_schedule_body()))

    assert status == 201
    assert body["scheduled_task_id"] == "a"
    sqs.send_message_to_input_queue.assert_not_called()


def test_the_acknowledgement_travels_the_live_connection(handler):
    handler._handle_queue_mode(_event(_schedule_body()))

    kwargs = handler.broadcast.call_args.kwargs
    assert kwargs["message_type"] == LambdaWSHandler.MessageType.CHAT_RESPONSE
    assert kwargs["message"]["status"] == "SCHEDULED"
    assert kwargs["user_id"] == OWNER


def test_stream_mode_sends_one_terminal_frame(handler):
    """Nothing is generated at creation time, so there are no token deltas."""
    AKConfig.get().execution.mode = ExecutionMode.STREAM

    handler._handle_queue_mode(_event(_schedule_body()))

    kwargs = handler.broadcast.call_args.kwargs
    assert kwargs["message_type"] == LambdaWSHandler.MessageType.STREAM_CHUNK
    assert kwargs["message"]["done"] is True
    assert handler.broadcast.call_count == 1


def test_an_ordinary_frame_is_still_enqueued(handler):
    with patch("agentkernel.deployment.aws.serverless.core.router.ws_lambda.SQSHandler") as sqs:
        status, _ = handler._handle_queue_mode(_event({"prompt": "hi", "session_id": "s1"}))

    assert status == 200
    sqs.send_message_to_input_queue.assert_called_once()


def test_a_scheduled_frame_is_rejected_when_scheduling_is_disabled(handler):
    handler._schedule_service = None
    status, body = handler._handle_queue_mode(_event(_schedule_body()))

    assert status == 400
    assert "not enabled" in body["message"]


def test_a_too_fine_schedule_is_rejected(handler):
    status, _ = handler._handle_queue_mode(_event({"prompt": "hi", "schedule": {"rate": "10 seconds"}}))
    assert status == 400


def test_another_owners_live_row_is_403_not_400(handler):
    """The WS surface must not collapse ownership and state conflicts into a generic bad request."""
    handler._handle_queue_mode(_event(_schedule_body()))
    handler.get_user_id = MagicMock(return_value="someone-else")

    status, _ = handler._handle_queue_mode(_event(_schedule_body()))

    assert status == 403


def test_a_soft_deleted_id_is_409_not_400(handler):
    handler._handle_queue_mode(_event(_schedule_body()))
    handler._schedule_service.delete("a", owner_id=OWNER)

    status, _ = handler._handle_queue_mode(_event(_schedule_body()))

    assert status == 409


class TestDirectMode:
    """A deployment without queues refuses to schedule, because a fire would have nowhere to land.

    Scheduling is queue-mode-only: the timer's target is the input queue, and direct mode consumes
    no queue, so a registration here would be acknowledged and then never run. Refusing is a 400 —
    it must not run the prompt now either, which is what a caller asking for "later" least wants.
    """

    def test_direct_chat_refuses_to_register(self, handler):
        handler._chat_service = MagicMock()

        status, body = handler._handle_direct_chat(_event(_schedule_body()))

        assert status == 400
        assert "queue mode" in body["message"]
        handler._chat_service.process_chat_request.assert_not_called()
        # Nothing registered, so nothing to acknowledge.
        handler.broadcast.assert_not_called()

    def test_direct_chat_registers_nothing(self, handler):
        """A 400 that still wrote the row would leave a task nothing ever runs."""
        handler._chat_service = MagicMock()

        handler._handle_direct_chat(_event(_schedule_body()))

        with pytest.raises(SchedulerNotFoundError):
            handler._schedule_service.get("a", owner_id=OWNER)

    def test_direct_stream_refuses_to_register(self, handler):
        AKConfig.get().execution.mode = ExecutionMode.STREAM
        handler._chat_service = MagicMock()

        status, body = handler._handle_stream_direct(_event(_schedule_body()))

        assert status == 400
        assert "queue mode" in body["message"]
        handler._chat_service.process_stream_chat_sync.assert_not_called()

    def test_an_ordinary_direct_frame_still_reaches_the_agent(self, handler):
        handler._chat_service = MagicMock()
        handler._chat_service.process_chat_request.return_value = (200, {"result": "hi"})

        handler._handle_direct_chat(_event({"prompt": "hi", "session_id": "s1"}))

        handler._chat_service.process_chat_request.assert_called_once()
