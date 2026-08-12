"""The ECS WebSocket chat route's schedule branch.

Containerized supports async/stream under queue mode, so without this branch a frame
carrying a schedule would be enqueued and executed immediately rather than scheduled — the
one outcome this feature must never produce.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest_scheduler import enable_scheduler_config, install_scheduler, reset_scheduler_config
from fastapi.testclient import TestClient

from agentkernel.core.config import AKConfig, _DynamoDBConfig
from agentkernel.core.model import BaseRequest, ExecutionMode
from agentkernel.deployment.aws.containerized.core.api.websocket_api import AWSWebsocketAPI, ECSWebSocketRequestHandler
from agentkernel.deployment.aws.core.websocket_service import AWSWebSocketHandler
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore

OWNER = "u1"
ENDPOINT_URL = "https://abc.execute-api.us-east-1.amazonaws.com/prod"


@pytest.fixture(autouse=True)
def _scheduler_config():
    config = enable_scheduler_config()
    config.websocket_api.connection_table = _DynamoDBConfig(table_name="ak-connections")
    config.execution.mode = ExecutionMode.ASYNC
    install_scheduler(InMemoryScheduledTaskStore())
    yield
    config.websocket_api.connection_table = None
    config.execution.mode = ExecutionMode.REST_SYNC
    reset_scheduler_config()


@pytest.fixture
def handler() -> ECSWebSocketRequestHandler:
    return ECSWebSocketRequestHandler()


def _frame(body: dict) -> BaseRequest:
    return BaseRequest.from_payload({"request_id": "r1", "body": body})


@pytest.fixture
def broadcast(handler):
    """Stub the push channel and return the broadcast mock."""
    websocket_handler = MagicMock()
    handler._ws_handler = websocket_handler
    return websocket_handler.broadcast


async def _handle(handler, body: dict):
    """Drive _handle_chat with a resolved route context."""
    context = handler.WSRouteContext(message=_frame(body), user_id=OWNER, connection_id="c1", endpoint_url=ENDPOINT_URL)
    with patch.object(ECSWebSocketRequestHandler, "build_route_context", AsyncMock(return_value=context)):
        with patch("agentkernel.deployment.aws.containerized.core.api.websocket_api.SQSHandler") as sqs:
            response = await handler._handle_chat(MagicMock())
    return response, sqs


@pytest.mark.asyncio
async def test_a_scheduled_frame_is_registered_and_never_enqueued(handler, broadcast):
    response, sqs = await _handle(handler, {"prompt": "run the report", "schedule": {"rate": "1 hour", "id": "a"}})

    assert response.status_code == 201
    sqs.send_message_to_input_queue.assert_not_called()
    assert handler._schedule_service.get("a", owner_id=OWNER).owner_id == OWNER


@pytest.mark.asyncio
async def test_the_acknowledgement_is_broadcast_as_a_chat_response(handler, broadcast):
    await _handle(handler, {"prompt": "hi", "schedule": {"rate": "1 hour", "id": "a"}})

    kwargs = broadcast.call_args.kwargs
    assert kwargs["message_type"] == AWSWebSocketHandler.MessageType.CHAT_RESPONSE
    assert kwargs["message"]["status"] == "SCHEDULED"
    assert "done" not in kwargs["message"]


@pytest.mark.asyncio
async def test_stream_mode_sends_one_terminal_frame(handler, broadcast):
    """Nothing is generated at creation time, so there are no token deltas."""
    AKConfig.get().execution.mode = ExecutionMode.STREAM

    await _handle(handler, {"prompt": "hi", "schedule": {"rate": "1 hour", "id": "a"}})

    kwargs = broadcast.call_args.kwargs
    assert kwargs["message_type"] == AWSWebSocketHandler.MessageType.STREAM_CHUNK
    assert kwargs["message"]["done"] is True
    assert broadcast.call_count == 1


@pytest.mark.asyncio
async def test_an_ordinary_frame_is_still_enqueued(handler, broadcast):
    response, sqs = await _handle(handler, {"prompt": "hi", "session_id": "s1"})

    assert response.status_code == 200
    sqs.send_message_to_input_queue.assert_called_once()


@pytest.mark.asyncio
async def test_a_scheduled_frame_is_rejected_when_scheduling_is_disabled(handler, broadcast):
    handler._schedule_service = None
    response, sqs = await _handle(handler, {"prompt": "hi", "schedule": {"rate": "1 hour"}})

    assert response.status_code == 400
    sqs.send_message_to_input_queue.assert_not_called()


@pytest.mark.asyncio
async def test_a_too_fine_schedule_is_rejected(handler, broadcast):
    response, _ = await _handle(handler, {"prompt": "hi", "schedule": {"rate": "10 seconds"}})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_an_unauthenticated_connection_is_rejected_before_the_branch(handler):
    """build_route_context already raises 401 when the connection has no user."""
    error = ECSWebSocketRequestHandler.WSRouteError(401, "No user found for connection_id: c1")
    with patch.object(ECSWebSocketRequestHandler, "build_route_context", AsyncMock(side_effect=error)):
        response = await handler._handle_chat(MagicMock())

    assert response.status_code == 401


def test_construction_needs_no_authoriser():
    """A WebSocket deployment authenticates at $connect and has no Authoriser object."""
    ECSWebSocketRequestHandler()  # must not raise


def test_a_scheduling_enabled_websocket_deployment_boots_without_the_schedule_routes():
    """The REST management routes need an Authoriser, which this deployment has none of, so
    auto-mounting them would raise AKConfigError before uvicorn ever binds."""
    AWSWebsocketAPI.set_auth_handler(MagicMock())
    try:
        with patch("uvicorn.run") as mock_uvicorn:
            AWSWebsocketAPI.run()
    finally:
        AWSWebsocketAPI._ws_auth_validator = None

    client = TestClient(mock_uvicorn.call_args.kwargs["app"])
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/schedule").status_code == 404
