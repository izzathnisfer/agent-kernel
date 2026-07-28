"""
Conversation-initiation system tool.

Registered on all agents when agent-initiated conversations are enabled (see
``AKConfig.conversation_initiation_enabled``). The tool creates a new session inside the
Agent Runner, runs the owning agent with the caller's prompt so the outbound
message and its context land in the new session's framework history
naturally, and dispatches an InitiationMessage toward the Response Handler —
the single send point.

``_initiate_conversation``'s own docstring is parsed by the framework adapters into the
LLM-visible tool schema (the OpenAI SDK's ``function_tool`` reads it), so it describes only
what the model needs; implementation rationale belongs here or on the nested helpers.

``InitiationMessage.target_details`` is deliberately absent from the tool signature: LLM
tool schemas must be strict, which rules out free-form dict parameters, so platform extras
can only be supplied by a custom dispatch path and never by the model.
"""

import asyncio
import logging
import threading
import uuid
from typing import Callable

from ..model import AgentRequestText, SystemTool
from ..tool import ToolContext
from .manager import InitiationManager
from .model import InitiationMessage

_log = logging.getLogger("ak.initiation.tool")


def _initiate_conversation(target: str, prompt: str, user_id: str = "", agent: str = "") -> str:
    """
    Start a new conversation with a user on the messaging platform.

    Args:
        target: Recipient address on the messaging platform (channel/user id, phone number, email).
        prompt: Instruction for composing the outbound message; the agent's reply to this prompt is what gets sent.
        user_id: Recipient's user id for conversation history; defaults to target.
        agent: Name of the agent that composes the message and owns the new conversation; defaults to the first registered agent.

    Returns:
        Status text with the new conversation's session id, or an error description.
    """
    try:
        if not target:
            return "Cannot initiate conversation: 'target' is required"
        if not prompt:
            return "Cannot initiate conversation: 'prompt' is required"

        manager = InitiationManager.get()
        if manager is None:
            return (
                "Cannot initiate conversation: conversation initiation is not enabled "
                "(set session.initiation.enabled: true, or deploy in queue mode)"
            )
        if InitiationManager._dispatcher is None:
            return "Cannot initiate conversation: no initiation dispatcher is registered in this process"

        runtime = ToolContext.get().runtime

        if agent:
            selected = runtime.agents().get(agent)
            if selected is None:
                return f"Cannot initiate conversation: no agent named '{agent}' is registered"
        else:
            agents = list(runtime.agents().values())
            selected = agents[0] if agents else None
            if selected is None:
                return "Cannot initiate conversation: no agents are registered"

        session_id = str(uuid.uuid4())
        session = runtime.sessions().new(session_id)

        outcome: dict = {}

        def _run() -> None:
            """
            Runs the nested agent on a dedicated thread, recording its reply or error in
            ``outcome``.

            This function is synchronous (LLM tool functions are), but composing the
            outbound message means awaiting ``runtime.run()``. Bridging sync to async needs
            ``asyncio.run()``, which raises if the calling thread already has a running event
            loop — and whether it does is adapter-dependent: the OpenAI SDK runs sync tools
            via ``asyncio.to_thread`` (no loop on that worker), while another adapter may
            call straight from the loop thread. A brand-new thread is guaranteed to have no
            loop, so starting one and joining it works under every adapter. It buys no
            concurrency, only a clean loop; the new session has its own lock and context, so
            it cannot deadlock the caller's run.

            ``BaseException`` is caught because an exception raised on this thread would
            otherwise be swallowed instead of reaching the tool's return value.
            """
            try:
                outcome["reply"] = asyncio.run(runtime.run(selected, session, [AgentRequestText(prompt=prompt)]))
            except BaseException as e:
                outcome["error"] = e

        runner_thread = threading.Thread(target=_run, name="ak-initiation-run")
        runner_thread.start()
        runner_thread.join()

        if "error" in outcome:
            _log.error(f"Initiation agent run failed: {outcome['error']}")
            return f"Cannot initiate conversation: composing the outbound message failed ({outcome['error']})"

        message = str(outcome["reply"])
        initiation = InitiationMessage(
            session_id=session_id,
            message=message,
            target=target,
            user_id=user_id or target,
            request_id=str(uuid.uuid4()),
        )
        manager.dispatch(initiation)
        return f"Conversation initiated. session_id={session_id}"

    except Exception as e:
        _log.exception("Error initiating conversation")
        return f"Cannot initiate conversation: {e}"


class InitiateConversationTool(SystemTool):
    name: str = "initiate_conversation"
    description: str = (
        "You can proactively start a NEW conversation with another user on the connected messaging platform.\n"
        "Available tool:\n"
        "- initiate_conversation(target, prompt, user_id='', agent=''): composes a message from "
        "`prompt` and sends it to `target`, starting a new, independent conversation that continues when the user replies.\n"
        "  - target: the recipient's platform address (channel/user id, phone number, or email) — required.\n"
        '  - prompt: instructions for the message to send (e.g. "Inform Monroe that her laptop is ready") — required.\n'
        "  - user_id: the recipient's user id for conversation history; defaults to target.\n"
        "  - agent: name of the agent that should own the new conversation; defaults to the first registered agent.\n"
        "Use this tool when asked to message, notify, or start a conversation with ANOTHER user. The new conversation "
        "is independent of the current one; the tool returns the new conversation's session id."
    )
    func: Callable = _initiate_conversation
