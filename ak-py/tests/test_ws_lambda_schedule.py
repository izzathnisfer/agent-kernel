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
