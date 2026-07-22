# #545: Evaluate and Integrate Pydantic Logfire as an Observability Provider — Implementation Spec

Details how the design in [design.md](design.md) is built: a new `trace/logfire/` provider package
(`Logfire(BaseTrace)` + six traced runners), one `Trace._build()` branch, one pyproject extra, a CLI
example, tests, and docs. The one design idea — `logfire.configure()` installs Logfire as the global
OpenTelemetry tracer provider, so framework instrumentation (native where it exists, the extras'
bundled OpenInference instrumentors otherwise) emits into it. `design.md` is the requirements source;
every requirement there maps to a section here. The change is purely additive: no framework adapter,
`BaseTrace`, `AKConfig` field, or existing provider is modified.

## Design

### Package layout

```
ak-py/src/agentkernel/trace/logfire/
├── __init__.py        # from .logfire import Logfire; __all__ = ["Logfire"]
├── logfire.py         # Logfire(BaseTrace) — configure-once + six framework methods
├── openai.py          # LogfireOpenAIRunner(OpenAIRunner)
├── langgraph.py       # LogfireLangGraphRunner(LangGraphRunner)
├── crewai.py          # LogfireCrewAIRunner(CrewAIRunner)
├── adk.py             # LogfireADKRunner(GoogleADKRunner)
├── smolagents.py      # LogfireSmolagentsRunner(SmolagentsRunner)
└── pydanticai.py      # LogfirePydanticAIRunner(PydanticAIRunner)
```

This mirrors `trace/openllmetry/`'s layout (main class + one runner file per framework). `logfire.py`
imports the SDK at module top (`import logfire`), so selecting the provider without the extra
installed fails at the factory's `_build()` import, where `require_extra` converts it (see
[Error handling](#error-handling)).

### `Logfire` main class (`trace/logfire/logfire.py`)

Configures the SDK once per process and returns a traced runner per framework.

```python
import logfire            # module-top import: a missing SDK surfaces at Trace._build()

_SERVICE_NAME = "AgentKernel"

class Logfire(BaseTrace):
    # Class-level guard: Trace.get() builds a fresh instance every call (trace.py:24-35),
    # so per-instance state would not prevent re-configuration. Mirrors OpenLLMetry's
    # TraceloopContext._initialized + _init_lock (openllmetry.py:29-55).
    _init_lock = threading.Lock()
    _configured = False

    def init(self):
        with Logfire._init_lock:
            if Logfire._configured:
                return
            logfire.configure(
                service_name=os.getenv("LOGFIRE_SERVICE_NAME", _SERVICE_NAME),
                send_to_logfire="if-token-present",   # cloud w/ credential, else console
                scrubbing=logfire.ScrubbingOptions(callback=_keep_session_id),  # allowlist session_id
            )
            Logfire._configured = True

# module-level: allowlist AK's own session-correlation attribute; default-scrub everything else
def _keep_session_id(match):
    path = getattr(match, "path", None)
    return match.value if path and path[-1] == "session_id" else None

    def pydanticai(self) -> Runner:                    # + openai/langgraph/crewai/adk/smolagents
        from .pydanticai import LogfirePydanticAIRunner
        return LogfirePydanticAIRunner()
```

Governing rules:

1. **`send_to_logfire="if-token-present"` is mandatory, not cosmetic.** Logfire's default (`None`)
   raises when no credential is configured; the literal makes the console fallback work with no
   signup (verified against `logfire==4.38.0`).
2. **Configure exactly once, thread-safely.** `init()` runs on every `Trace.get()`
   (`trace.py:33-34`); the class-level `_configured`/`_init_lock` prevents duplicate
   `logfire.configure()` calls (which warn and re-init).
3. **No `console=` argument.** Omitting it (leaving `console=None`) makes Logfire read its own
   `LOGFIRE_CONSOLE_*` env vars — AK invents no console default.
4. **`scrubbing` allowlists `session_id`.** `_keep_session_id` returns the original value only for
   the `session_id` attribute (an identifier, not a secret) and `None` for everything else, so
   Logfire's default scrubber stops redacting the correlation attribute while still scrubbing
   prompts/tool-args/tokens (verified). The allowlist is by attribute key, so it applies to any
   `session_id` attribute process-wide — intentional, since that key is always an identifier.
5. **Framework methods lazily import their runner module**, so importing `Logfire` never imports a
   framework SDK that may be absent (matches `openllmetry.py:103-149`).

### Per-framework traced runners

Every runner is the same shape: subclass the framework's base `Runner`, instrument in `__init__`,
wrap `super().run()` in an AK span carrying `session_id`. Interface sketch (Pydantic AI, the
showcase):

```python
# trace/logfire/pydanticai.py
class LogfirePydanticAIRunner(PydanticAIRunner):
    def __init__(self):
        super().__init__()
        logfire.instrument_pydantic_ai()               # native; idempotent (verified)

    async def run(self, agent, session, requests) -> AgentReply:
        with logfire.span("Agent Kernel Pydantic AI", session_id=session.id) as span:
            result = await super().run(agent, session, requests)
            span.set_attributes({"input": result.prompt or "", "output": str(result)})
        return result
```

Instrumentation call per runner (verified: `logfire==4.38.0` has `instrument_pydantic_ai`,
`instrument_openai_agents`, `instrument_litellm`; it has **no** `instrument_langchain`/`_crewai`/
`_adk`/`_smolagents`):

| Runner | `__init__` instrumentation | Span name |
|---|---|---|
| `LogfirePydanticAIRunner` | `logfire.instrument_pydantic_ai()` | `Agent Kernel Pydantic AI` |
| `LogfireOpenAIRunner` | `logfire.instrument_openai_agents()` | `Agent Kernel OpenAI` |
| `LogfireCrewAIRunner` | `CrewAIInstrumentor().instrument(skip_dep_check=True)` + `LiteLLMInstrumentor().instrument()` | `Agent Kernel CrewAI` |
| `LogfireADKRunner` | `GoogleADKInstrumentor().instrument()` | `Agent Kernel ADK` |
| `LogfireLangGraphRunner` | `LangChainInstrumentor().instrument()` | `Agent Kernel LangGraph` |
| `LogfireSmolagentsRunner` | none (native OTel) | `Agent Kernel Smolagents` |

- The CrewAI/ADK OpenInference imports (`openinference.instrumentation.crewai`, `.litellm`,
  `.google_adk`) are the **same imports** the langfuse runners use (`trace/langfuse/crewai.py:5-6`,
  `trace/langfuse/adk.py:5`) and ship in the `crewai`/`adk` extras. The LangGraph runner's
  `openinference.instrumentation.langchain` import is a **new** dependency added to the `langgraph`
  extra (LangGraph ships no bundled instrumentor). None are added to the `logfire` extra — each
  runner module is imported only when its `Trace.get().<framework>()` method runs, i.e. when that
  framework (hence its extra) is in use.
- `span.set_attributes({"input": result.prompt or "", "output": str(result)})` uses `result.prompt or ""`
  because the base runner's early-return replies set no prompt; `set_attributes` rejects `None`
  values.

### Interface note: `logfire.span` context manager

`with logfire.span(name, **attrs) as span:` yields a `LogfireSpan` exposing `set_attribute` /
`set_attributes` (verified). The runners never catch inside the `with`: the framework base runners
already convert exceptions to `AgentReplyText(user_facing_error_message(e))` (e.g.
`framework/pydanticai/pydanticai.py:165-166`), so `super().run()` does not raise and the span closes
normally.

## Consumer changes

### `trace/trace.py` (the only edited source file outside the new package)

- `_BUILTIN_TRACERS` gains `"logfire"` (`:8`).
- `Trace._build()` gains a branch after the `openllmetry` one (`:51-56`):

  ```python
  if trace_type == "logfire":
      with require_extra("logfire", "trace.type: logfire"):
          from .logfire.logfire import Logfire
      return Logfire()
  ```
- `Trace`'s six framework delegate methods (`openai`/…/`pydanticai`, `:62-108`) are unchanged — they
  already delegate to `self._instance.<framework>()`.

### Verified unchanged

- **`trace/base.py`** — `pydanticai()` is already `@abstractmethod` (`:49-50`, added by #531);
  `Logfire` implements all six framework methods + `init`, so no base change.
- **Framework `Module`s** — each already selects the traced runner via `Trace.get().<framework>()`
  when `trace.enabled` (e.g. `framework/pydanticai/pydanticai.py:315-316`); a new `trace.type` value
  needs no adapter edit.
- **The `langfuse`/`openllmetry` providers** — untouched.

## Config changes

None. `_TraceConfig` (`config.py:265-270`) is:

```python
class _TraceConfig(BaseModel):
    enabled: bool = Field(default=False, description="Enable tracing")
    type: str = Field(default="langfuse", description="Tracing backend: a built-in short name ... or a dotted path ...")
```

`type` is a free-form `str` with no `pattern=`, so `trace.type: logfire` validates unchanged. YAML
files and `AK_TRACE__*` env vars written before this change keep working; the new value is simply
now recognized by the factory.

## Behavioural notes

Numbered, each intentional:

1. **`session_id` scrubbing is allowlisted (resolved — design.md Decisions).** Logfire's default
   scrubber redacts values whose key matches a sensitive pattern, and `session_id` matches
   `session`. The provider passes a `scrubbing` callback (`_keep_session_id`) that returns the value
   for the `session_id` attribute and `None` otherwise, so the correlation attribute is visible in
   console and dashboard while all other scrubbing stays on. The `session_id` key is retained (not
   renamed) to match the langfuse/openllmetry runners.
2. **Console output is on by default.** With `console=None`, Logfire prints spans to stderr during
   runs (its own default); users silence or tune it via `LOGFIRE_CONSOLE*` env vars. This is
   desirable for the no-signup evaluation path.
3. **LangGraph uses the OpenInference LangChain instrumentor (model-agnostic).**
   `LangChainInstrumentor` hooks LangChain's callback system — which LangGraph runs on — so it
   captures graph nodes and model calls regardless of the model backend (langchain-openai,
   langchain-litellm, ...). This replaces an earlier LiteLLM-only approach that missed the common
   `ChatOpenAI` path the langgraph example uses; it also removes the double-LiteLLM-instrumentation
   risk with the CrewAI runner.
4. **Native vs. reused instrumentation is asymmetric across runners** (table above) — deliberate:
   use Logfire-native where it exists (Pydantic AI, OpenAI Agents, LiteLLM), reuse the extras'
   OpenInference instrumentors otherwise, all feeding the one global OTel provider.

**Non-changes:** no `AKConfig` field/default/description changes; no change to `BaseTrace`, the
`Trace` delegate methods, or any framework adapter; no new dependency in any extra other than the
new `logfire` group; existing `config.yaml` / `AK_TRACE__*` semantics preserved.

## Error handling

- **Missing `logfire` extra.** Because `trace/logfire/logfire.py` imports `logfire` at module top,
  selecting `trace.type: logfire` without the extra raises `ImportError` at the `_build()` import;
  `require_extra("logfire", "trace.type: logfire")` (`core/util/factory.py:49-64`) re-raises it as
  `trace.type: logfire requires the 'logfire' extra: pip install "agentkernel[logfire]"`.
- **Missing framework instrumentor** (e.g. CrewAI runner without the `crewai` extra) — the runner
  module's top-level `from openinference... import ...` raises `ImportError`; this can only occur if
  the CrewAI framework is selected without its own extra, which is already a broken install.
- **No credential.** Not an error: `send_to_logfire="if-token-present"` falls back to console.
- **Run-time exceptions** inside an agent are handled by the framework base runner (converted to an
  error `AgentReplyText`), so the Logfire span always closes cleanly.

## Testing

Per `.agents/skills/ak-dev-testing-conventions`; unit tests only (no live LLM/cloud calls).

### `ak-py/tests/test_trace.py` (edit)

- **Fix `_FakeTrace`**: add a `pydanticai()` method. `_FakeTrace` (`:15-37`) implements only the
  original five framework methods; once `BaseTrace` carries #531's abstract `pydanticai()` (as this
  branch does), `test_trace_byo_dotted_path` raises `TypeError: Can't instantiate abstract class
  _FakeTrace without an implementation for abstract method 'pydanticai'` (verified locally). The
  breakage is inherited from #531, not introduced by #545, but this change touches the file and must
  land the fix.
- **Add `test_trace_logfire_missing_extra_raises_friendly_import_error`** mirroring the langfuse/
  openllmetry cases (`:81-97`): `monkeypatch.setitem(sys.modules, "logfire", None)` **and**
  `monkeypatch.delitem(sys.modules, "agentkernel.trace.logfire.logfire", raising=False)` (evict the
  cached provider module so its top-level `import logfire` re-executes), assert the raised
  `ImportError` contains `agentkernel[logfire]`.

### `ak-py/tests/test_trace_logfire.py` (new)

Guarded by `logfire = pytest.importorskip("logfire")` at module top. Asserts:

- `Logfire` is a complete `BaseTrace` (instantiates — all seven abstract methods present).
- `init()` calls `logfire.configure(...)` **once** with `send_to_logfire="if-token-present"` and a
  `scrubbing` argument; reset the class-level `_configured` guard around the test.
- `_keep_session_id` allowlists only `session_id`: a pure-function unit test asserts it returns the
  value for a match whose `path` ends `session_id` and `None` for any other path (no Logfire
  internals needed).
- `LogfirePydanticAIRunner()` calls `logfire.instrument_pydantic_ai()` on construction
  (`pytest.importorskip("pydantic_ai")`; patch `logfire.instrument_pydantic_ai`).
- `LogfirePydanticAIRunner.run()` opens `logfire.span("Agent Kernel Pydantic AI", session_id=...)`,
  awaits `super().run()`, and calls `span.set_attributes({"input": ..., "output": ...})` — patch
  `logfire.span` (a `MagicMock` context manager) and
  `agentkernel.framework.pydanticai.pydanticai.PydanticAIRunner.run` (an `AsyncMock`).

### Run

`cd ak-py && uv run pytest tests/test_trace.py tests/test_trace_logfire.py`, then full
`uv run pytest` + `make lint-check-all` (black + isort, line length 150).

## Examples and docs

- **`examples/cli/pydanticai-logfire/`** — `demo.py` reuses the `examples/cli/pydanticai/` triage/
  math/weather agents unchanged; the only additions are `config.yaml` (`trace: {enabled: true,
  type: logfire}`), a `pyproject.toml` depending on `agentkernel[cli,pydanticai,logfire]` +
  `pydantic-ai-slim[openai]`, `build.sh`, `demo_test.py`, `.gitignore`, and a `README.md` covering
  the auto-detect (cloud via `logfire auth`/`LOGFIRE_TOKEN`, else console) flow. Demonstrates that
  tracing is transparent to agent code.
- **`docs/docs/advanced/traceability.md`** — add a "Getting Started with Pydantic Logfire" section
  (install `agentkernel[logfire]`, config, credentials/auto-detect, features), a Logfire entry in
  the supported-platforms list, a "Viewing Traces → In Pydantic Logfire" subsection, a Logfire
  troubleshooting subsection, a data-handling line, a Related-Resources link, and update the summary
  to three platforms.
- **`README.md`** — add Pydantic Logfire to the two observability lines.
