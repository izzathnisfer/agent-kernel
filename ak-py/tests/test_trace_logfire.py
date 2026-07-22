"""Tests for the Pydantic Logfire tracing provider (trace/logfire/).

The factory-level resolution of ``type: logfire`` (built-in name + friendly missing-extra error)
lives in ``test_trace.py`` alongside the other tracers; this file covers the provider itself and
its traced runner.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

logfire = pytest.importorskip("logfire")

from agentkernel.core import Session
from agentkernel.core.model import AgentReplyText, AgentRequestText
from agentkernel.trace.base import BaseTrace
from agentkernel.trace.logfire.logfire import Logfire, _keep_session_id


def test_logfire_is_complete_basetrace():
    """All seven abstract methods are implemented, so Logfire is instantiable."""
    assert issubclass(Logfire, BaseTrace)
    isinstance(Logfire(), BaseTrace)  # constructing it would raise TypeError if a method were missing


def test_init_configures_logfire_with_auto_detect():
    """init() configures the SDK once with send_to_logfire='if-token-present' — the setting that
    ships spans to the cloud when a token is present and falls back to the console otherwise."""
    Logfire._configured = False
    try:
        with patch.object(logfire, "configure") as configure:
            Logfire().init()
            Logfire().init()  # second build's init() is a no-op: configuration happens once per process
        configure.assert_called_once()
        assert configure.call_args.kwargs["send_to_logfire"] == "if-token-present"
        assert "scrubbing" in configure.call_args.kwargs  # session_id allowlist wired in
    finally:
        Logfire._configured = False  # don't leak the guard into other tests


def test_keep_session_id_allowlists_only_session_id():
    """The scrubbing callback un-redacts the session_id correlation attribute and nothing else."""
    session_match = SimpleNamespace(path=("session_id",), value="sess-1")
    other_match = SimpleNamespace(path=("prompt",), value="secret prompt")

    assert _keep_session_id(session_match) == "sess-1"  # kept
    assert _keep_session_id(other_match) is None  # falls through to default scrubbing
    assert _keep_session_id(SimpleNamespace(path=(), value="x")) is None  # empty path is safe


def test_pydanticai_runner_instruments_on_construction():
    """Constructing the traced runner enables Logfire-native Pydantic AI instrumentation."""
    pytest.importorskip("pydantic_ai")
    from agentkernel.trace.logfire.pydanticai import LogfirePydanticAIRunner

    with patch.object(logfire, "instrument_pydantic_ai") as instrument:
        LogfirePydanticAIRunner()
    instrument.assert_called_once()


@pytest.mark.asyncio
async def test_pydanticai_runner_wraps_run_in_span():
    """The traced Pydantic AI runner opens an 'Agent Kernel Pydantic AI' span carrying the session
    id, delegates to the base runner, and records the input/output on the span."""
    pytest.importorskip("pydantic_ai")
    from agentkernel.trace.logfire.pydanticai import LogfirePydanticAIRunner

    reply = AgentReplyText(response="4", prompt="2+2?")
    span = MagicMock()
    span_cm = MagicMock()
    span_cm.__enter__.return_value = span

    with (
        patch.object(logfire, "instrument_pydantic_ai"),
        patch.object(logfire, "span", return_value=span_cm) as span_fn,
        patch(
            "agentkernel.framework.pydanticai.pydanticai.PydanticAIRunner.run",
            new=AsyncMock(return_value=reply),
        ) as base_run,
    ):
        runner = LogfirePydanticAIRunner()
        session = Session("sess-1")
        result = await runner.run(MagicMock(), session, [AgentRequestText(prompt="2+2?")])

    assert result is reply
    span_fn.assert_called_once_with("Agent Kernel Pydantic AI", session_id="sess-1")
    base_run.assert_awaited_once()
    span.set_attributes.assert_called_once_with({"input": "2+2?", "output": str(reply)})
