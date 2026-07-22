import logging
from typing import Any

import logfire
from openinference.instrumentation.crewai import CrewAIInstrumentor
from openinference.instrumentation.litellm import LiteLLMInstrumentor

from ...core import Session
from ...core.model import AgentReply, AgentRequest
from ...framework.crewai.crewai import CrewAIRunner


class LogfireCrewAIRunner(CrewAIRunner):

    def __init__(self):
        """
        Initializes a LogfireCrewAIRunner instance.
        """
        super().__init__()
        self._log = logging.getLogger("ak.trace.logfire.crewai")
        # CrewAI ships no Logfire-native instrumentor. Reuse the OpenInference instrumentors bundled
        # with the crewai extra — their OpenTelemetry spans flow into Logfire's global tracer provider.
        CrewAIInstrumentor().instrument(skip_dep_check=True)
        LiteLLMInstrumentor().instrument()

    async def run(self, agent: Any, session: Session, requests: list[AgentRequest]) -> AgentReply:
        """
        Runs the CrewAI agent with provided multi modal inputs.
        :param agent: The CrewAI agent to run.
        :param session: The session to use for the agent.
        :param requests: The requests to the agent.
        :return: The result of the agent's execution.
        """
        with logfire.span("Agent Kernel CrewAI", session_id=session.id) as span:
            result = await super().run(agent, session, requests)
            span.set_attributes({"input": result.prompt or "", "output": str(result)})
        return result
