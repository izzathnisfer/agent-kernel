import logging
from typing import Any

from pydantic_ai import Agent

from ...core import Session
from ...core.model import AgentReply, AgentRequest
from ...framework.pydanticai.pydanticai import PydanticAIRunner
from .openllmetry import TraceloopContext


class OpenLLMetryPydanticAIRunner(PydanticAIRunner):

    def __init__(self):
        """
        Initializes an OpenLLMetryPydanticAIRunner instance.
        """
        super().__init__()
        self._log = logging.getLogger("ak.trace.openllmetry.pydanticai")

        # Diverges from every sibling OpenLLMetry runner, which self-instrument nothing and rely
        # entirely on Traceloop.init()'s bundled auto-instrumentors. Whether traceloop-sdk bundles a
        # Pydantic AI auto-instrumentor is unconfirmed, so we self-instrument: Pydantic AI's native
        # OTel emission only needs an active TracerProvider (Traceloop.init() installs one globally) —
        # not a framework-specific patch. instrument_all() is an idempotent class-variable toggle, so
        # this is harmless even if Traceloop's bundle turns out to cover Pydantic AI too.
        Agent.instrument_all()

    async def run(self, agent: Any, session: Session, requests: list[AgentRequest]) -> AgentReply:
        """
        Runs the Pydantic AI agent with provided multi modal inputs.
        :param agent: The Pydantic AI agent to run.
        :param session: The session to use for the agent.
        :param requests: The requests to the agent.
        :return: The result of the agent's execution.
        """

        with TraceloopContext(app_name="AgentKernel Pydantic AI", association_properties={"session_id": session.id}):
            result = await super().run(agent, session, requests)
        return result
