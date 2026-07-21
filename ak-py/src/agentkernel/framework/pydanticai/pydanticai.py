from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from typing import Any, Callable, List

from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import BinaryContent, DocumentUrl, FunctionToolset, ImageUrl, Tool
from pydantic_ai.messages import ModelMessagesTypeAdapter, UserContent
from pydantic_core import to_jsonable_python

from ...core import Agent as BaseAgent
from ...core import Module, PostHook, PreHook
from ...core import Runner as BaseRunner
from ...core import Runtime, Session, ToolBuilder, ToolContext
from ...core.builder import A2ACardBuilder
from ...core.config import AKConfig
from ...core.model import (
    AgentReply,
    AgentReplyAny,
    AgentReplyText,
    AgentRequest,
    AgentRequestAny,
    AgentRequestFile,
    AgentRequestImage,
    AgentRequestText,
)
from ...core.util.error_util import user_facing_error_message
from ...trace import Trace

FRAMEWORK = "pydanticai"


class PydanticAISession:
    """
    PydanticAISession stores the running message history for Pydantic AI-based agents.

    Pydantic AI has no session protocol of its own; conversation state is a plain
    ``list[ModelMessage]`` passed as ``message_history=`` and read back via
    ``result.all_messages()``. This class holds that history in its jsonable form
    (``to_jsonable_python(messages)``) rather than the raw message objects, so a pickled
    session survives Pydantic AI's fast release cadence — the supported interchange format is
    JSON via ``ModelMessagesTypeAdapter``, not raw pickling of its message classes. The Runner,
    not this class, does the jsonable<->object conversion.
    """

    def __init__(self):
        """
        Initializes a PydanticAISession instance.
        """
        self._messages: list[dict] = []

    @property
    def messages(self) -> list[dict]:
        """
        Returns the stored message history in jsonable form.
        """
        return self._messages

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        """
        Sets the stored message history (jsonable form).
        :param value: The jsonable message history to store.
        """
        self._messages = value


class PydanticAIRunner(BaseRunner):
    """
    PydanticAIRunner class provides a runner for Pydantic AI-based agents.
    """

    def __init__(self):
        """
        Initializes a PydanticAIRunner instance.
        """
        super().__init__(FRAMEWORK)

    @staticmethod
    def _session(session: Session) -> PydanticAISession | None:
        """
        Returns the Pydantic AI session associated with the provided session.
        :param session: The session to retrieve the Pydantic AI session for.
        :return: PydanticAISession instance.
        """
        if session is None:
            return None
        return session.get(FRAMEWORK) or session.set(FRAMEWORK, PydanticAISession())

    @staticmethod
    def _process_requests(requests: list[AgentRequest]) -> tuple[str, list[UserContent]]:
        """
        Process requests and extract prompt text and Pydantic AI multi-modal content.
        :param requests: The requests to process.
        :return: Tuple of (prompt, content).
        """
        prompt = ""
        content: list[UserContent] = []

        for req in requests:
            if isinstance(req, AgentRequestAny):
                continue

            if isinstance(req, AgentRequestText):
                text = req.prompt
                prompt = prompt + "\n" + text if prompt else text
                content.append(text)

            elif isinstance(req, AgentRequestImage):
                if not req.image_data:
                    raise ValueError("no image input provided")

                if req.image_data.startswith(("http://", "https://", "s3://")):
                    content.append(ImageUrl(url=req.image_data))
                else:
                    if not req.mime_type:
                        raise ValueError("mime_type is missing for image input, either in the base64 or explicitly")
                    content.append(BinaryContent(data=base64.b64decode(req.image_data), media_type=req.mime_type))

            elif isinstance(req, AgentRequestFile):
                if not req.file_data:
                    raise ValueError("no file input provided")

                if req.file_data.startswith(("http://", "https://", "s3://")):
                    content.append(DocumentUrl(url=req.file_data))
                else:
                    if not req.mime_type:
                        raise ValueError("mime_type is missing for file input, either in the base64 or explicitly")
                    content.append(BinaryContent(data=base64.b64decode(req.file_data), media_type=req.mime_type))

        return prompt, content

    async def run(self, agent: Any, session: Session, requests: list[AgentRequest]) -> AgentReply:
        """
        Runs the Pydantic AI agent with the provided multi modal inputs.
        :param agent: The Pydantic AI agent to run.
        :param session: The session to use for the agent.
        :param requests: The requests to the agent.
        :return: The result of the agent's execution.
        """
        context: ToolContext | None = None
        prompt = ""
        try:
            context = ToolContext(Runtime.current(), agent, session, requests).set()
            prompt, content = self._process_requests(requests)

            if not content:
                return AgentReplyText(response="Sorry. No valid content found in the requests")

            fw_session = self._session(session)
            history = ModelMessagesTypeAdapter.validate_python(fw_session.messages) if fw_session and fw_session.messages else None

            result = await agent.agent.run(content, message_history=history)

            if fw_session is not None:
                fw_session.messages = to_jsonable_python(result.all_messages())

            structured = AgentReplyAny.from_output(result.output, prompt)
            if structured is not None:
                return structured

            reply_text = "" if result.output is None else str(result.output)
            return AgentReplyText(response=reply_text, prompt=prompt)
        except Exception as e:
            return AgentReplyText(response=user_facing_error_message(e), prompt=prompt)
        finally:
            if context is not None:
                context.reset()

    async def stream(self, agent: Any, session: Session, requests: list[AgentRequest]) -> AsyncGenerator[str, None]:
        """
        Streams the Pydantic AI agent response token by token.

        Note: a Pydantic AI streaming run stops at the first ``output_type`` match, so combining a
        structured ``output_type`` with AK's streaming mode truncates differently than the
        non-streaming ``run()`` path. AK's plain-text streaming contract is unaffected.

        :param agent: The Pydantic AI agent to run.
        :param session: The session to use for the agent.
        :param requests: The requests to the agent.
        :return: An async generator yielding string token deltas.
        """
        context: ToolContext | None = None
        try:
            context = ToolContext(Runtime.current(), agent, session, requests).set()
            prompt, content = self._process_requests(requests)

            if not content:
                return

            fw_session = self._session(session)
            history = ModelMessagesTypeAdapter.validate_python(fw_session.messages) if fw_session and fw_session.messages else None

            async with agent.agent.run_stream(content, message_history=history) as result:
                async for delta in result.stream_text(delta=True):
                    if delta:
                        yield delta

                if fw_session is not None:
                    fw_session.messages = to_jsonable_python(result.all_messages())
        finally:
            if context is not None:
                context.reset()


class PydanticAIAgent(BaseAgent):
    """
    PydanticAIAgent class provides an agent wrapping for Pydantic AI-based agents.
    """

    def __init__(self, name: str, runner: PydanticAIRunner, agent: PydanticAgent):
        """
        Initializes a PydanticAIAgent instance.
        :param name: Name of the agent.
        :param runner: Runner associated with the agent.
        :param agent: The Pydantic AI agent instance.
        """
        super().__init__(name, runner)
        self._agent = agent
        self._attach_system_tools()
        self._setup_system_prompt()

    @property
    def agent(self) -> PydanticAgent:
        """
        Returns the Pydantic AI agent instance.
        """
        return self._agent

    def get_description(self) -> str:
        """
        Returns the description of the agent.

        Unlike the OpenAI adapter (where ``instructions`` is effectively mandatory), Pydantic AI's
        ``description`` is an optional constructor parameter that is commonly left unset — a wrapped
        agent constructed without ``description=`` reports an empty string here (and an empty A2A
        card summary). Pydantic AI's ``instructions`` is a write-only decorator method with no
        public getter, so it cannot serve as the description source.
        """
        return self.agent.description or ""

    def override_system_prompt(self, prompt: str) -> None:
        """
        Appends additional instructions to the Pydantic AI agent's system prompt.
        Called by the base Agent._setup_system_prompt() at init when multimodal is enabled.

        Pydantic AI's instructions have no public read path, so the OpenAI adapter's
        "read current string, check membership, concatenate" pattern has no equivalent. The only
        supported way to add instruction content is the public ``agent.instructions(func)``
        decorator API, which registers ``func`` as one more contributor to the system prompt.
        No de-duplication guard is possible (nothing to read back against); this is safe because
        ``_setup_system_prompt()`` runs exactly once per Agent.__init__().
        """
        if prompt:
            self._agent.instructions(lambda: prompt)

    def attach_tool(self, tool: Any) -> None:
        """
        Accepts a raw Callable, wraps it with PydanticAIToolBuilder, and registers it on the
        agent's function toolset. Called by the base Agent._attach_system_tools() at init to
        register system tools (e.g., the multimodal attachment-analysis tool).
        :param tool: Raw Python callable to attach.
        """
        wrapped = PydanticAIToolBuilder.bind([tool])
        # Register on the agent's own FunctionToolset. AK never uses the ``toolsets=`` constructor
        # parameter, so the agent always exposes exactly one FunctionToolset (its own), reachable
        # via the public ``toolsets`` property.
        function_toolset = next((ts for ts in self._agent.toolsets if isinstance(ts, FunctionToolset)), None)
        if function_toolset is None:
            return
        for w in wrapped:
            if w.name not in function_toolset.tools:
                function_toolset.add_tool(w)

    def get_a2a_card(self) -> Any:
        """
        Returns the A2A AgentCard associated with the agent.
        """
        from a2a.types import AgentSkill

        skills = []

        def visitor(ts: Any) -> None:
            # Only FunctionToolset (and subclasses) expose a public synchronous ``tools`` dict; the
            # general async ``get_tools(ctx)`` path needs a RunContext that isn't available here, so
            # non-FunctionToolset leaves (e.g. MCP servers) are silently skipped. AK never attaches
            # those to a wrapped agent, so this is complete in practice.
            if isinstance(ts, FunctionToolset):
                for name, tool in ts.tools.items():
                    skills.append(AgentSkill(id=name, name=name, description=tool.description or "", tags=[]))

        # ``apply()`` recurses into CombinedToolset/WrapperToolset members, so every real leaf
        # toolset is reached regardless of nesting. Do not index ``[0]`` — the toolset count varies.
        for toolset in self.agent.toolsets:
            toolset.apply(visitor)

        return A2ACardBuilder.build(name=self.name, description=self.get_description(), skills=skills)


class PydanticAIModule(Module):
    """
    PydanticAIModule class provides a module for Pydantic AI-based agents.
    """

    def __init__(self, agents: list[PydanticAgent], runner: PydanticAIRunner = None):
        """
        Initializes a PydanticAIModule instance.
        :param agents: List of agents in the module.
        :param runner: Custom runner associated with the module.
        """
        super().__init__()
        if runner is not None:
            self.runner = runner
        elif AKConfig.get().trace.enabled:
            self.runner = Trace.get().pydanticai()
        else:
            self.runner = PydanticAIRunner()
        self.load(agents)

    def _wrap(self, agent: PydanticAgent, agents: List[PydanticAgent]) -> BaseAgent:
        """
        Wraps the provided agent in a PydanticAIAgent instance.
        :param agent: Agent to wrap.
        :param agents: List of agents in the module.
        :return: PydanticAIAgent instance.
        :raises ValueError: If the agent has no explicit name.
        """
        if agent.name is None:
            raise ValueError(
                "Pydantic AI agents passed to PydanticAIModule must have an explicit name= — "
                "AK registers agents by name immediately, before any run triggers Pydantic AI's "
                "call-frame name inference."
            )
        return PydanticAIAgent(agent.name, self.runner, agent)

    def load(self, agents: list[PydanticAgent]) -> PydanticAIModule:
        """
        Loads the specified agents into the module. By replacing the current agents.
        :param agents: List of agents to load.
        :return: PydanticAIModule instance.
        """
        super().load(agents)
        return self

    def pre_hook(self, agent: PydanticAgent, hooks: list[PreHook]) -> "PydanticAIModule":
        """
        Attaches pre-execution hooks to the agent.
        :param agent: The agent to attach hooks to.
        :param hooks: List of pre-execution hooks to attach.
        :return: PydanticAIModule instance.
        """
        super().get_agent(agent.name).pre_hooks.extend(hooks)
        return self

    def post_hook(self, agent: PydanticAgent, hooks: list[PostHook]) -> "PydanticAIModule":
        """
        Attaches post-execution hooks to the agent.
        :param agent: The agent to attach hooks to.
        :param hooks: List of post-execution hooks to attach.
        :return: PydanticAIModule instance.
        """
        super().get_agent(agent.name).post_hooks.extend(hooks)
        return self


class PydanticAIToolBuilder(ToolBuilder):
    """
    Tool builder for Pydantic AI.

    Wraps generic tool functions into Pydantic AI ``Tool`` objects. No ``deps_type``/``RunContext``
    dependency injection is used — every AK tool reaches execution context via ``ToolContext.get()``
    so one tool function stays portable across every adapter.
    """

    @classmethod
    def bind(cls, funcs: list[Callable]) -> list[Tool]:
        """
        Bind generic tool functions to Pydantic AI ``Tool`` definitions.

        :param funcs: List of generic tool functions to bind.
        :return: List of Pydantic AI ``Tool`` definitions.
        :raises TypeError: If any item in funcs is not callable.
        """
        tools = []
        for func in funcs:
            if not callable(func):
                raise TypeError(f"Expected a callable, got {type(func).__name__}")
            tools.append(Tool(func))
        return tools
