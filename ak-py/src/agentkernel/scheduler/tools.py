"""Scheduler system tools — the agent-facing surface of the scheduled-task capability.

Registered on an agent (via ``SystemToolFactory``) when ``scheduler.enabled`` is true and the
agent is in scope. They are the agent's equivalent of the chat endpoint's ``schedule`` block,
since an agent has no HTTP client into its own deployment. All go through the same
``ScheduledTaskService`` as the REST surfaces and return JSON strings, reporting failures as
``{"error": ...}`` rather than raising into the framework.
"""

import json
import logging
from typing import Any, Optional

from ..core.base import Session
from ..core.model import REQUEST_USER_ID_KEY, SystemTool
from ..core.tool import ToolContext
from .errors import SchedulerError
from .factory import SchedulerFactory
from .model import ScheduledTask, ScheduleMode, ScheduleSpec

_log = logging.getLogger("ak.scheduler.tools")

_DISABLED = json.dumps({"error": "scheduled task capability is disabled"})
_NO_OWNER = json.dumps({"error": "no authenticated owner available for this session"})


class _ToolSupport:
    """Shared plumbing for the scheduler tools: service lookup, owner binding, serialization."""

    @staticmethod
    def service():
        """Return the shared ``ScheduledTaskService``, or None when the capability is off."""
        return SchedulerFactory.service()

    @staticmethod
    def owner_id() -> Optional[str]:
        """Resolve the human identity that owns the invoking session.

        A tool cannot set an arbitrary owner. The id is the ``user_id`` of the request that
        started the invoking session, which ``ChatService`` binds to the session's volatile
        cache. On a scheduled fire that is the task's own owner, so a task created from a
        scheduled run stays with the same user.

        :return: The owning user id, or None when the session has no authenticated user.
        """
        try:
            session = ToolContext.get().session
        except RuntimeError:
            return None
        cache = session.get(Session.Keys.VOLATILE_CACHE.value) if session else None
        return cache.get(REQUEST_USER_ID_KEY) if cache else None

    @staticmethod
    def build_spec(
        cron: Optional[str],
        rate: Optional[str],
        at: Optional[str],
        mode: Optional[str],
        scheduled_task_id: Optional[str],
    ) -> ScheduleSpec:
        """Build a ``ScheduleSpec`` from the model's flat tool arguments.

        :param cron: Cron expression, or None.
        :param rate: Rate expression, or None.
        :param at: ISO-8601 one-time instant, or None.
        :param mode: ``per_run`` or ``continuous``. None leaves the field unset, which is how
            an update keeps the task's current mode instead of resetting it to the default.
        :param scheduled_task_id: Caller-chosen id, or None to have one generated.
        :return: The parsed schedule.
        :raises ValueError: The arguments do not form a valid schedule.
        """
        payload: dict[str, Any] = {"id": scheduled_task_id, "cron": cron, "rate": rate, "at": at}
        if mode is not None:
            payload["mode"] = ScheduleMode(mode)
        return ScheduleSpec.model_validate(payload)

    @staticmethod
    def summarize(task: ScheduledTask) -> dict[str, Any]:
        """Render a scheduled task for the model, omitting internal bookkeeping.

        :param task: The scheduled task to render.
        :return: The JSON-safe summary.
        """
        return {
            "scheduled_task_id": task.scheduled_task_id,
            "prompt": task.message.get("prompt"),
            "agent": task.message.get("agent"),
            "schedule": task.schedule.model_dump(mode="json", exclude_none=True, exclude={"id"}),
            "status": task.status.value,
            "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
            "last_run_status": task.last_run_status.value if task.last_run_status else None,
            "last_error": task.last_error,
        }

    @staticmethod
    def error(exc: Exception) -> str:
        """Serialize a failure into the tool ``{"error": ...}`` JSON contract.

        :param exc: The failure to report.
        :return: The JSON error string.
        """
        return json.dumps({"error": str(exc)})


def create_scheduled_task(
    prompt: str,
    cron: Optional[str] = None,
    rate: Optional[str] = None,
    at: Optional[str] = None,
    agent: Optional[str] = None,
    mode: str = "per_run",
    scheduled_task_id: Optional[str] = None,
) -> str:
    """
    Schedule a prompt to be sent to an agent later, once or repeatedly.

    Args:
        prompt: The prompt the agent receives on every run.
        cron: Cron expression, e.g. "0 9 * * ? *" for 09:00 daily. Six fields, no seconds.
        rate: Fixed interval, e.g. "30 minutes" or "1 day". Minimum 1 minute.
        at: ISO-8601 instant for a one-time run, e.g. "2026-08-09T09:00:00Z".
        agent: Agent to run; omit for the deployment's default.
        mode: "per_run" for a fresh conversation each run, "continuous" to keep one.
        scheduled_task_id: Choose the id; re-using one replaces that task's definition.

    Returns:
        JSON with scheduled_task_id, next_run_at and session_id; or {"error": ...} on failure.
    """
    service = _ToolSupport.service()
    if service is None:
        return _DISABLED
    owner_id = _ToolSupport.owner_id()
    if owner_id is None:
        return _NO_OWNER
    try:
        spec = _ToolSupport.build_spec(cron, rate, at, mode, scheduled_task_id)
        ack = service.create(spec=spec, prompt=prompt, agent=agent, owner_id=owner_id)
        return ack.model_dump_json(exclude_none=True)
    except (SchedulerError, ValueError) as exc:
        _log.warning("create_scheduled_task failed: %s", exc)
        return _ToolSupport.error(exc)


def update_scheduled_task(
    scheduled_task_id: str,
    prompt: Optional[str] = None,
    cron: Optional[str] = None,
    rate: Optional[str] = None,
    at: Optional[str] = None,
    agent: Optional[str] = None,
    mode: Optional[str] = None,
) -> str:
    """
    Change an existing scheduled task's prompt or timing. Does not create new tasks.

    Args:
        scheduled_task_id: Id of the task to change.
        prompt: Replacement prompt; omit to keep the current one.
        cron: Replacement cron expression.
        rate: Replacement fixed interval.
        at: Replacement ISO-8601 one-time instant. A one-time task that has already run needs
            a new future instant here before anything else about it can be changed.
        agent: Replacement agent; omit to keep the current one.
        mode: Replacement conversation mode; omit to keep the current one.

    Returns:
        JSON with the updated task; or {"error": ...} on failure.
    """
    service = _ToolSupport.service()
    if service is None:
        return _DISABLED
    owner_id = _ToolSupport.owner_id()
    if owner_id is None:
        return _NO_OWNER
    try:
        spec = _ToolSupport.build_spec(cron, rate, at, mode, scheduled_task_id) if (cron or rate or at) else None
        task = service.update(scheduled_task_id, owner_id=owner_id, spec=spec, prompt=prompt, agent=agent)
        return json.dumps(_ToolSupport.summarize(task))
    except (SchedulerError, ValueError) as exc:
        _log.warning("update_scheduled_task failed: %s", exc)
        return _ToolSupport.error(exc)


def delete_scheduled_task(scheduled_task_id: str) -> str:
    """
    Delete a scheduled task so it never runs again. Cannot be undone.

    Args:
        scheduled_task_id: Id of the task to delete.

    Returns:
        JSON with scheduled_task_id and deleted=true; or {"error": ...} on failure.
    """
    service = _ToolSupport.service()
    if service is None:
        return _DISABLED
    owner_id = _ToolSupport.owner_id()
    if owner_id is None:
        return _NO_OWNER
    try:
        service.delete(scheduled_task_id, owner_id=owner_id)
        return json.dumps({"scheduled_task_id": scheduled_task_id, "deleted": True})
    except (SchedulerError, ValueError) as exc:
        _log.warning("delete_scheduled_task failed: %s", exc)
        return _ToolSupport.error(exc)


def list_scheduled_tasks(limit: Optional[int] = None, cursor: Optional[str] = None) -> str:
    """
    List the scheduled tasks belonging to this conversation's user.

    Args:
        limit: Maximum number of tasks to return.
        cursor: Continuation token from a previous listing.

    Returns:
        JSON {"tasks": [...], "next_cursor": ...}; or {"error": ...} on failure.
    """
    service = _ToolSupport.service()
    if service is None:
        return _DISABLED
    owner_id = _ToolSupport.owner_id()
    if owner_id is None:
        return _NO_OWNER
    try:
        page = service.list(owner_id=owner_id, limit=limit, cursor=cursor)
        return json.dumps({"tasks": [_ToolSupport.summarize(task) for task in page.items], "next_cursor": page.next_cursor})
    except (SchedulerError, ValueError) as exc:
        _log.warning("list_scheduled_tasks failed: %s", exc)
        return _ToolSupport.error(exc)


def get_scheduler_tools() -> list[SystemTool]:
    """Build the scheduler system tools; called by ``SystemToolFactory`` when enabled.

    Following the sandbox pattern, the capability's whole system-prompt section sits on the
    first tool's ``description``, so agent authors never describe these tools in their own
    instructions. The rest have empty descriptions; their LLM-facing schemas come from the
    function docstrings when the tools are bound.

    :return: The four scheduler tools.
    """
    guidance = (
        "[Scheduled tasks]\n"
        "You can schedule a prompt to be sent to an agent later — once, or repeatedly on a cron "
        "or fixed-interval schedule. Use this when the user asks for something to happen at a "
        "time rather than now (a daily report, a reminder, a periodic check).\n"
        "Available tools:\n"
        "- create_scheduled_task(prompt, cron, rate, at, agent, mode, scheduled_task_id): register a "
        "prompt to run later. Give exactly one of cron, rate or at.\n"
        "- update_scheduled_task(scheduled_task_id, prompt, cron, rate, at, agent, mode): change an "
        "existing task. It never creates one. A one-time task that has already run needs a new "
        "future at before anything about it can be changed — ask the user when it should run next.\n"
        "- delete_scheduled_task(scheduled_task_id): stop a task from ever running again.\n"
        "- list_scheduled_tasks(limit, cursor): list the tasks belonging to this user.\n"
        "Timing: cron takes six fields (minute hour day-of-month month day-of-week year) and rate "
        'takes "<n> minutes|hours|days". The finest schedule is one minute; anything finer is '
        "rejected rather than rounded.\n"
        'mode="per_run" starts a fresh conversation each run; mode="continuous" keeps one running '
        "conversation across runs. Omitting mode on an update keeps whichever the task already "
        "uses, so retiming a task never moves its conversation.\n"
        "Every task belongs to the user of this conversation — you cannot schedule on someone "
        "else's behalf, and you never choose an owner.\n"
        'If a tool result contains an "error" field the operation FAILED: report the error to the '
        "user; never describe an unscheduled task as scheduled."
    )
    return [
        SystemTool(name="create_scheduled_task", description=guidance, func=create_scheduled_task),
        SystemTool(name="update_scheduled_task", description="", func=update_scheduled_task),
        SystemTool(name="delete_scheduled_task", description="", func=delete_scheduled_task),
        SystemTool(name="list_scheduled_tasks", description="", func=list_scheduled_tasks),
    ]
