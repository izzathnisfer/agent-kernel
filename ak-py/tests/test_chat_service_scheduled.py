"""Scheduling-adjacent behaviour: ChatService's response echo and known-fields guard,
plus ThreadRecorder keeping scheduled runs out of the owner's conversation history.

Thread recording lives in the thread integration, not ChatService, so the suppression
rule is asserted against ThreadRecorder.
"""

from unittest.mock import MagicMock

from agentkernel.core.base import Session
from agentkernel.core.chat_service import AgentHandler, RequestBuilder, ResponseBuilder
from agentkernel.core.model import REQUEST_USER_ID_KEY, AgentReplyText, AgentRequestAny, BaseRunRequest, ScheduledRunMetadata
from agentkernel.integration.thread.recorder import ThreadRecorder

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


class TestRequestUserBinding:
    """The seam the agent tools' ownership model rests on.

    Every execution entry point binds the request's user to the session's volatile cache, and
    ``_ToolSupport.owner_id`` reads it back from there. A regression makes every scheduling tool
    call refuse with "no authenticated owner", so the binding is asserted directly.
    """

    @staticmethod
    def _handler_with_cache(cache) -> AgentHandler:
        session = MagicMock()
        session.get.return_value = cache
        handler = AgentHandler()
        handler.service = MagicMock()
        handler.service.session = session
        return handler

    def test_the_request_user_lands_in_the_sessions_volatile_cache(self):
        cache = MagicMock()

        self._handler_with_cache(cache).bind_request_user("u1")

        cache.set.assert_called_once_with(REQUEST_USER_ID_KEY, "u1")

    def test_the_volatile_cache_is_the_key_read_from_the_session(self):
        cache = MagicMock()
        handler = self._handler_with_cache(cache)

        handler.bind_request_user("u1")

        handler.service.session.get.assert_called_once_with(Session.Keys.VOLATILE_CACHE.value)

    def test_an_unauthenticated_request_binds_nothing(self):
        """A tool must refuse rather than act for a forged or absent owner."""
        cache = MagicMock()

        self._handler_with_cache(cache).bind_request_user(None)

        cache.set.assert_not_called()


class TestThreadSuppression:
    """Scheduled activity is kept out of the owner's regular conversation history."""

    def test_a_scheduled_session_creates_no_thread(self):
        manager = MagicMock()

        requests, attachments = ThreadRecorder(manager).pre_run(_fire(), ["request"])

        assert requests == ["request"]
        assert attachments == []
        manager.store_attachments.assert_not_called()
        manager.get_or_create_thread.assert_not_called()
        manager.append_message.assert_not_called()

    def test_a_scheduled_session_appends_no_assistant_message(self):
        manager = MagicMock()
        ThreadRecorder(manager).post_run(_fire(), "the report")
        manager.append_message.assert_not_called()

    def test_an_ordinary_session_still_creates_its_thread(self):
        manager = MagicMock()
        manager.store_attachments.return_value = (["request"], [])

        ThreadRecorder(manager).pre_run(BaseRunRequest(prompt="hi", user_id="u1", session_id="s1"), ["request"])

        manager.get_or_create_thread.assert_called_once()
