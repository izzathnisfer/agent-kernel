import logging
from typing import Any

import logfire

from ...core import Session
from ...core.model import AgentReply, AgentRequest
from ...framework.openai.openai import OpenAIRunner


class LogfireOpenAIRunner(OpenAIRunner):

    def __init__(self):
        """
        Initializes a LogfireOpenAIRunner instance.
        """
        super().__init__()
        self._log = logging.getLogger("ak.trace.logfire.openai")
        # Logfire-native instrumentation for the OpenAI Agents SDK.
        logfire.instrument_openai_agents()

    async def run(self, agent: Any, session: Session, requests: list[AgentRequest]) -> AgentReply:
        """
        Runs the OpenAI agent with provided multi modal inputs.
        :param agent: The OpenAI agent to run.
        :param session: The session to use for the agent.
        :param requests: The requests to the agent.
        :return: The result of the agent's execution.
        """
        with logfire.span("Agent Kernel OpenAI", session_id=session.id) as span:
            result = await super().run(agent, session, requests)
            span.set_attributes({"input": result.prompt or "", "output": str(result)})
        return result
