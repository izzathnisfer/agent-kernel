"""ChatService's scheduling-adjacent behaviour: the response echo, the known-fields
guard, and keeping scheduled runs out of the owner's conversation history."""

from unittest.mock import MagicMock

import pytest

from agentkernel.core.chat_service import ChatService, RequestBuilder, ResponseBuilder
from agentkernel.core.model import AgentReplyText, AgentRequestAny, BaseRunRequest, ScheduledRunMetadata

SCHEDULED_RUN = ScheduledRunMetadata(
    scheduled_task_id="a",
    scheduled_task_version="v1",
    scheduled_time="2026-08-09T09:00:00Z",
    run_id="exec-1",
)


def _fire(**overrides) -> BaseRunRequest:
    payload = {
        "prompt": "run the report",
        "user_id": "u1",
        "session_id": "schedule:a:2026-08-09T09:00:00Z",
        "scheduled_run": SCHEDULED_RUN,
    }
    payload.update(overrides)
    return BaseRunRequest(**payload)


class TestKnownFields:
    def test_the_correlation_block_never_reaches_the_agent_as_context(self):
        """Leaking scheduling metadata into the agent's context would be worse."""
        requests = RequestBuilder.from_base_request_sync(_fire())
        assert not any(isinstance(request, AgentRequestAny) and request.name == "scheduled_run" for request in requests)

    def test_a_schedule_block_never_reaches_the_agent_as_context(self):
        request = BaseRunRequest(prompt="hi", session_id="s1", schedule={"rate": "1 hour"})
        requests = RequestBuilder.from_base_request_sync(request)
        assert not any(isinstance(entry, AgentRequestAny) and entry.name == "schedule" for entry in requests)

    def test_genuinely_unknown_fields_are_still_forwarded(self):
        request = BaseRunRequest(prompt="hi", session_id="s1", tenant="acme")
        requests = RequestBuilder.from_base_request_sync(request)
        assert any(isinstance(entry, AgentRequestAny) and entry.name == "tenant" for entry in requests)


class TestResponseEcho:
    def test_a_successful_run_echoes_the_block(self):
        response = ResponseBuilder.build_response(200, "s1", True, result=AgentReplyText(response="ok"), scheduled_run=SCHEDULED_RUN)
        assert response["scheduled_run"]["run_id"] == "exec-1"

    def test_an_errored_run_still_carries_its_correlation_metadata(self):
        status, response = ResponseBuilder.build_response(500, "s1", False, error=RuntimeError("boom"), scheduled_run=SCHEDULED_RUN)
        assert status == 500
        assert response["scheduled_run"]["scheduled_task_id"] == "a"

    def test_ordinary_traffic_carries_no_such_key(self):
        response = ResponseBuilder.build_response(200, "s1", True, result=AgentReplyText(response="ok"))
        assert "scheduled_run" not in response


class TestThreadSuppression:
    """Scheduled activity is kept out of the owner's regular conversation history."""

    def test_a_scheduled_session_creates_no_thread(self):
        service = ChatService()
        manager = MagicMock()

        requests = service._thread_pre_run(manager, _fire(), ["request"])

        assert requests == ["request"]
        manager.get_or_create_thread.assert_not_called()
        manager.append_message.assert_not_called()

    def test_a_scheduled_session_appends_no_assistant_message(self):
        manager = MagicMock()
        ChatService._thread_post_run(manager, _fire(), "the report")
        manager.append_message.assert_not_called()

    def test_an_ordinary_session_still_creates_its_thread(self):
        service = ChatService()
        manager = MagicMock()
        manager.store_attachments.return_value = (["request"], [])

        service._thread_pre_run(manager, BaseRunRequest(prompt="hi", user_id="u1", session_id="s1"), ["request"])

        manager.get_or_create_thread.assert_called_once()
