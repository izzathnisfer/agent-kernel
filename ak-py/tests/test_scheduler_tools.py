"""Agent-callable scheduling tools."""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from conftest_scheduler import enable_scheduler_config, reset_scheduler_config

from agentkernel.core.base import Session
from agentkernel.core.config import AKConfig
from agentkernel.core.model import REQUEST_USER_ID_KEY
from agentkernel.core.tool import SystemToolFactory, ToolContext
from agentkernel.core.util.factory import AKConfigError
from agentkernel.scheduler import tools as scheduler_tools
from agentkernel.scheduler.errors import SchedulerPermissionError
from agentkernel.scheduler.model import CreateAck, ScheduleMode, ScheduleSpec
from agentkernel.scheduler.testing import build_task

OWNER = "u1"


@pytest.fixture(autouse=True)
def _scheduler_config():
    enable_scheduler_config()
    yield
    reset_scheduler_config()


@pytest.fixture
def service(monkeypatch) -> MagicMock:
    """Replace the shared service so the tools are tested against their collaborator."""
    mock = MagicMock()
    mock.create.return_value = CreateAck(scheduled_task_id="a", scheduled_task_version="v1", request_id="r1")
    mock.update.return_value = build_task("a", owner_id=OWNER)
    mock.list.return_value = MagicMock(items=[build_task("a", owner_id=OWNER)], next_cursor=None)
    monkeypatch.setattr(scheduler_tools.SchedulerFactory, "service", staticmethod(lambda: mock))
    return mock


@pytest.fixture
def tool_context(monkeypatch):
    """Bind an invoking session that carries an authenticated user."""
    session = Session("schedule:a:2026-08-09T09:00:00Z")
    session.get(Session.Keys.VOLATILE_CACHE.value).set(REQUEST_USER_ID_KEY, OWNER)
    context = MagicMock()
    context.session = session
    monkeypatch.setattr(ToolContext, "get", classmethod(lambda cls: context))
    return context


class TestOwnerBinding:
    def test_the_owner_comes_from_the_invoking_session(self, service, tool_context):
        scheduler_tools.create_scheduled_task(prompt="hi", rate="1 hour")
        assert service.create.call_args.kwargs["owner_id"] == OWNER

    def test_the_owner_cannot_be_supplied_as_an_argument(self):
        """An agent is the mechanism; the human stays the principal."""
        import inspect

        for tool in (
            scheduler_tools.create_scheduled_task,
            scheduler_tools.update_scheduled_task,
            scheduler_tools.delete_scheduled_task,
            scheduler_tools.list_scheduled_tasks,
        ):
            assert "owner_id" not in inspect.signature(tool).parameters

    def test_a_session_with_no_authenticated_user_creates_nothing(self, service, monkeypatch):
        session = Session("s1")
        context = MagicMock()
        context.session = session
        monkeypatch.setattr(ToolContext, "get", classmethod(lambda cls: context))

        result = json.loads(scheduler_tools.create_scheduled_task(prompt="hi", rate="1 hour"))
        assert "no authenticated owner" in result["error"]
        service.create.assert_not_called()

    def test_no_tool_context_yields_an_error_rather_than_a_raise(self, service, monkeypatch):
        def no_context(cls):
            raise RuntimeError("No ToolContext is set in the current context")

        monkeypatch.setattr(ToolContext, "get", classmethod(no_context))
        assert "error" in json.loads(scheduler_tools.create_scheduled_task(prompt="hi", rate="1 hour"))


class TestRouting:
    def test_create_routes_through_the_service(self, service, tool_context):
        scheduler_tools.create_scheduled_task(prompt="hi", cron="0 9 * * ? *", agent="reporter", mode="continuous", scheduled_task_id="a")
        kwargs = service.create.call_args.kwargs
        assert kwargs["prompt"] == "hi"
        assert kwargs["agent"] == "reporter"
        assert kwargs["spec"] == ScheduleSpec(id="a", cron="0 9 * * ? *", mode=ScheduleMode.CONTINUOUS)

    def test_update_keeps_the_schedule_when_no_timing_is_given(self, service, tool_context):
        scheduler_tools.update_scheduled_task("a", prompt="new prompt")
        assert service.update.call_args.kwargs["spec"] is None

    def test_update_replaces_the_schedule_when_a_timing_is_given(self, service, tool_context):
        scheduler_tools.update_scheduled_task("a", rate="2 hours")
        assert service.update.call_args.kwargs["spec"].rate == "2 hours"

    def test_retiming_leaves_the_mode_for_the_service_to_carry_over(self, service, tool_context):
        """An unset mode is what tells the service to keep the task's current one."""
        scheduler_tools.update_scheduled_task("a", rate="2 hours")
        assert "mode" not in service.update.call_args.kwargs["spec"].model_fields_set

    def test_an_explicit_mode_is_passed_through(self, service, tool_context):
        scheduler_tools.update_scheduled_task("a", rate="2 hours", mode="continuous")
        assert service.update.call_args.kwargs["spec"].mode == ScheduleMode.CONTINUOUS
        # The spec carries the mode, so there is nothing left for the separate argument to apply.
        assert service.update.call_args.kwargs["mode"] is None

    def test_a_mode_only_change_reaches_the_service(self, service, tool_context):
        """Never a silent no-op: with no timing expression there is no spec to hang the mode on."""
        scheduler_tools.update_scheduled_task("a", mode="continuous")
        kwargs = service.update.call_args.kwargs
        assert kwargs["spec"] is None
        assert kwargs["mode"] == ScheduleMode.CONTINUOUS

    def test_an_unrecognised_mode_is_reported_rather_than_dropped(self, service, tool_context):
        assert "error" in json.loads(scheduler_tools.update_scheduled_task("a", mode="whenever"))
        service.update.assert_not_called()

    def test_delete_routes_through_the_service(self, service, tool_context):
        result = json.loads(scheduler_tools.delete_scheduled_task("a"))
        service.delete.assert_called_once_with("a", owner_id=OWNER)
        assert result == {"scheduled_task_id": "a", "deleted": True}

    def test_list_routes_through_the_service(self, service, tool_context):
        result = json.loads(scheduler_tools.list_scheduled_tasks(limit=5))
        service.list.assert_called_once_with(owner_id=OWNER, limit=5, cursor=None)
        assert result["tasks"][0]["scheduled_task_id"] == "a"


class TestErrorContract:
    def test_a_service_error_is_returned_rather_than_raised(self, service, tool_context):
        service.create.side_effect = SchedulerPermissionError("not yours")
        assert json.loads(scheduler_tools.create_scheduled_task(prompt="hi", rate="1 hour"))["error"] == "not yours"

    def test_an_invalid_schedule_is_returned_rather_than_raised(self, service, tool_context):
        assert "error" in json.loads(scheduler_tools.create_scheduled_task(prompt="hi"))

    def test_a_disabled_capability_is_reported(self, monkeypatch, tool_context):
        monkeypatch.setattr(scheduler_tools.SchedulerFactory, "service", staticmethod(lambda: None))
        assert "disabled" in json.loads(scheduler_tools.create_scheduled_task(prompt="hi", rate="1 hour"))["error"]

    def test_an_infrastructure_failure_is_returned_rather_than_raised(self, service, tool_context):
        """An AccessDenied or a throttle is neither a SchedulerError nor a ValueError."""
        service.create.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "CreateSchedule")
        assert "error" in json.loads(scheduler_tools.create_scheduled_task(prompt="hi", rate="1 hour"))

    @pytest.mark.parametrize(
        "call",
        [
            lambda: scheduler_tools.update_scheduled_task("a", prompt="p"),
            lambda: scheduler_tools.delete_scheduled_task("a"),
            lambda: scheduler_tools.list_scheduled_tasks(),
        ],
    )
    def test_every_tool_survives_an_infrastructure_failure(self, service, tool_context, call):
        error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "UpdateSchedule")
        service.update.side_effect = error
        service.delete.side_effect = error
        service.list.side_effect = error
        assert "error" in json.loads(call())

    def test_a_failure_resolving_the_service_is_returned_rather_than_raised(self, monkeypatch, tool_context):
        """SchedulerFactory.build can raise AKConfigError or a boto3 construction error."""

        def _explode():
            raise AKConfigError("scheduler misconfigured")

        monkeypatch.setattr(scheduler_tools.SchedulerFactory, "service", staticmethod(_explode))
        assert "error" in json.loads(scheduler_tools.create_scheduled_task(prompt="hi", rate="1 hour"))


class TestRegistration:
    def test_the_tools_are_registered_when_scheduling_is_enabled(self):
        names = {tool.name for tool in SystemToolFactory.get_all()}
        assert {"create_scheduled_task", "update_scheduled_task", "delete_scheduled_task", "list_scheduled_tasks"} <= names

    def test_the_tools_are_absent_when_scheduling_is_disabled(self):
        AKConfig.get().scheduler.enabled = False
        assert not any(tool.name.endswith("_scheduled_task") for tool in SystemToolFactory.get_all())

    def test_agents_scoping_is_honoured(self):
        AKConfig.get().scheduler.agents = ["reporter"]
        assert any(tool.name == "create_scheduled_task" for tool in SystemToolFactory.get_all("reporter"))
        assert not any(tool.name == "create_scheduled_task" for tool in SystemToolFactory.get_all("other"))

    def test_an_empty_agents_list_scopes_the_tools_away_entirely(self):
        AKConfig.get().scheduler.agents = []
        assert not any(tool.name == "create_scheduled_task" for tool in SystemToolFactory.get_all("reporter"))

    def test_the_capability_guidance_rides_on_the_first_tool(self):
        """The sandbox convention: one description carries the whole prompt section."""
        tools = scheduler_tools.get_scheduler_tools()
        assert "[Scheduled tasks]" in tools[0].description
        assert all(tool.description == "" for tool in tools[1:])
