import logging
from typing import Any

import logfire

from ...core import Session
from ...core.model import AgentReply, AgentRequest
from ...framework.pydanticai.pydanticai import PydanticAIRunner


class LogfirePydanticAIRunner(PydanticAIRunner):

    def __init__(self):
        """
        Initializes a LogfirePydanticAIRunner instance.
        """
        super().__init__()
        self._log = logging.getLogger("ak.trace.logfire.pydanticai")
        # First-class, Logfire-native Pydantic AI instrumentation: captures each model request,
        # tool call, and structured output as child spans. Idempotent across runner construction.
        logfire.instrument_pydantic_ai()

    async def run(self, agent: Any, session: Session, requests: list[AgentRequest]) -> AgentReply:
        """
        Runs the Pydantic AI agent with provided multi modal inputs.
        :param agent: The Pydantic AI agent to run.
        :param session: The session to use for the agent.
        :param requests: The requests to the agent.
        :return: The result of the agent's execution.
        """
        with logfire.span("Agent Kernel Pydantic AI", session_id=session.id) as span:
            result = await super().run(agent, session, requests)
            span.set_attributes({"input": result.prompt or "", "output": str(result)})
        return result
