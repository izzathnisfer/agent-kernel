import logging
from typing import Any

import logfire

from ...core import Session
from ...core.model import AgentReply, AgentRequest
from ...framework.langgraph.langgraph import LangGraphRunner


class LogfireLangGraphRunner(LangGraphRunner):

    def __init__(self):
        """
        Initializes a LogfireLangGraphRunner instance.
        """
        super().__init__()
        self._log = logging.getLogger("ak.trace.logfire.langgraph")
        # LangGraph has neither a Logfire-native nor a bundled OpenInference instrumentor. Its model
        # calls run through LiteLLM (shipped with the langgraph extra), which Logfire instruments
        # natively; combined with the AK-level span below this captures the LLM calls per session.
        logfire.instrument_litellm()

    async def run(self, agent: Any, session: Session, requests: list[AgentRequest]) -> AgentReply:
        """
        Runs the LangGraph agent with provided multi modal inputs.
        :param agent: The LangGraph agent to run.
        :param session: The session to use for the agent.
        :param requests: The requests to the agent.
        :return: The result of the agent's execution.
        """
        with logfire.span("Agent Kernel LangGraph", session_id=session.id) as span:
            result = await super().run(agent, session, requests)
            span.set_attributes({"input": result.prompt or "", "output": str(result)})
        return result
