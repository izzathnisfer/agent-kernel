from __future__ import annotations

import logging
import os
import threading

import logfire

from ...core import Runner
from ..base import BaseTrace

_SERVICE_NAME = "AgentKernel"


class Logfire(BaseTrace):
    """Pydantic Logfire tracing for Agent Kernel.

    Logfire is Pydantic's OpenTelemetry-based observability platform. ``init()`` configures the
    Logfire SDK once per process, which installs Logfire as the global OpenTelemetry tracer
    provider. From there the traced runners layer on framework instrumentation:

    * **Pydantic AI** and the **OpenAI Agents SDK** get first-class, Logfire-native instrumentation
      (``logfire.instrument_pydantic_ai`` / ``logfire.instrument_openai_agents``).
    * Frameworks that ship an OpenInference instrumentor with their extra (**CrewAI**, **Google
      ADK**) reuse it — those spans flow into Logfire because it owns the global tracer provider.
    * **LangGraph**'s model calls run through LiteLLM, captured via ``logfire.instrument_litellm``.
    * **Smolagents** emits OpenTelemetry spans natively into the global provider.

    **Auto-detect destination.** With ``send_to_logfire="if-token-present"`` Logfire ships spans to
    the hosted dashboard when a credential is available (``LOGFIRE_TOKEN`` env var or a
    ``logfire auth`` login), and otherwise falls back to printing spans to the console — so the
    provider runs with zero signup for a quick local evaluation. ``LOGFIRE_SERVICE_NAME`` overrides
    the service name; console output honours Logfire's own environment variables.
    """

    # Logfire must be configured exactly once per process. ``Trace.get()`` builds a fresh Logfire
    # instance on every call, so the guard is class-level (mirrors OpenLLMetry's TraceloopContext).
    _init_lock = threading.Lock()
    _configured = False

    def __init__(self):
        """
        Initializes a Logfire instance.
        """
        self._log = logging.getLogger("ak.trace.logfire")

    def init(self):
        """
        Configures the Logfire SDK once (thread-safe). Installs Logfire as the global OpenTelemetry
        tracer provider so framework instrumentation emits into it.
        """
        with Logfire._init_lock:
            if Logfire._configured:
                return

            logfire.configure(
                service_name=os.getenv("LOGFIRE_SERVICE_NAME", _SERVICE_NAME),
                # Send to the Logfire cloud only when a token/credential is present; otherwise emit
                # spans to the console. This is the one non-default setting — Logfire's own default
                # (``None``) raises when no credential is configured, which would break local runs.
                send_to_logfire="if-token-present",
            )
            Logfire._configured = True
            self._log.debug("Logfire configured (send_to_logfire=if-token-present)")

    def openai(self) -> Runner:
        """
        Returns the Logfire OpenAI runner instance.
        """
        from .openai import LogfireOpenAIRunner

        return LogfireOpenAIRunner()

    def langgraph(self) -> Runner:
        """
        Returns the Logfire LangGraph runner instance.
        """
        from .langgraph import LogfireLangGraphRunner

        return LogfireLangGraphRunner()

    def crewai(self) -> Runner:
        """
        Returns the Logfire CrewAI runner instance.
        """
        from .crewai import LogfireCrewAIRunner

        return LogfireCrewAIRunner()

    def adk(self) -> Runner:
        """
        Returns the Logfire ADK runner instance.
        """
        from .adk import LogfireADKRunner

        return LogfireADKRunner()

    def smolagents(self) -> Runner:
        """
        Returns the Logfire Smolagents runner instance.
        """
        from .smolagents import LogfireSmolagentsRunner

        return LogfireSmolagentsRunner()

    def pydanticai(self) -> Runner:
        """
        Returns the Logfire Pydantic AI runner instance.
        """
        from .pydanticai import LogfirePydanticAIRunner

        return LogfirePydanticAIRunner()
