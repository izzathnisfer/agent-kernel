# #545: Evaluate and Integrate Pydantic Logfire as an Observability Provider — Implementation Plan

Orders the build of [spec.md](spec.md) into five iterations. The change is **purely additive** — the
only edited source file outside the new `trace/logfire/` package is `trace/trace.py` (one factory
branch), plus the `logfire` extra in `pyproject.toml` and the pre-existing `_FakeTrace` fix in
`test_trace.py` — so the existing suite stays green at every iteration boundary. Steps reference
spec.md section names rather than restating them. (An implementation matching this plan already
exists on `feature/531-introduce-pydantic-logfire`.)

## Sequencing at a glance

| Iteration | Delivers | Depends on | Leaves working |
|---|---|---|---|
| 1. Provider + factory + extra | `trace/logfire/`, `_build` branch, `logfire` extra | — | `trace.type: logfire` traces all six adapters |
| 2. Example | `examples/cli/pydanticai-logfire/` | 1 | Runnable CLI demo emitting Logfire traces |
| 3. Tests | `test_trace_logfire.py` + `test_trace.py` edits | 1 | Full suite green incl. new coverage + `_FakeTrace` fix |
| 4. Docs | `traceability.md`, `README.md` | 1 | Docs + README show three providers |
| 5. Sync skills & docs | `.agents/skills/*`, `.claude/skills/*` | 1–4 | Repo guidance names three tracing providers |

Iteration 1 is the critical path; 2–4 depend only on it and could be parallelised.

---

## Iteration 1: Provider package + factory + extra

- **Goal:** `trace.type: logfire` builds a `Logfire` provider, configures the SDK once, and returns a
  traced runner for each of the six frameworks; `import agentkernel` is clean without the extra.
- **Files:** `ak-py/pyproject.toml`; `ak-py/src/agentkernel/trace/logfire/{__init__.py, logfire.py,
  openai.py, langgraph.py, crewai.py, adk.py, smolagents.py, pydanticai.py}`;
  `ak-py/src/agentkernel/trace/trace.py`.
- **Steps:**
  1. Add the `logfire` optional-dependency group (`logfire>=4.0.0`) and add
     `openinference-instrumentation-langchain>=0.1.29` to the existing `langgraph` extra (LangGraph is
     model-agnostic, ships no bundled instrumentor) — spec "Packaging". CrewAI/ADK reuse their extras'
     bundled OpenInference instrumentors. Run `uv lock` to co-resolve the new dep.
  2. Write `Logfire(BaseTrace)` per spec "`Logfire` main class": module-top `import logfire`,
     class-level `_configured`/`_init_lock`, `init()` → `logfire.configure(service_name=...,
     send_to_logfire="if-token-present", scrubbing=ScrubbingOptions(callback=_keep_session_id))`,
     the `_keep_session_id` allowlist function, six lazy framework methods.
  3. Write the six traced runners per spec "Per-framework traced runners" (native `instrument_*` for
     Pydantic AI / OpenAI; reused OpenInference instrumentors for CrewAI / ADK / LangGraph (LangChain);
     span-only for Smolagents). Guard span attrs with `result.prompt or ""`.
  4. Register `logfire` in `trace/trace.py`: add to `_BUILTIN_TRACERS` (`:8`) and the `_build` branch
     wrapped in `require_extra` (`:51-56`) — spec "Consumer changes".
- **Verify:** with the extra installed, from an example dir whose `config.yaml` has
  `trace.type: logfire`: `python -c "import demo"` constructs the module, configures Logfire, and
  the Pydantic AI runner instruments; a `run()` emits an `Agent Kernel Pydantic AI` span to the
  console (no token). Existing suite still green (`cd ak-py && uv run pytest`).

## Iteration 2: Example

- **Goal:** A runnable CLI demo that emits Logfire traces with no code difference from the plain
  Pydantic AI demo.
- **Files:** `examples/cli/pydanticai-logfire/{demo.py, config.yaml, pyproject.toml, build.sh,
  demo_test.py, README.md, .gitignore}`.
- **Steps (spec "Examples and docs"):** copy the `examples/cli/pydanticai/` agents unchanged; add
  `config.yaml` with `trace: {enabled: true, type: logfire}`; depend on
  `agentkernel[cli,pydanticai,logfire]` + `pydantic-ai-slim[openai]`; README documents the
  auto-detect (cloud via `logfire auth`/`LOGFIRE_TOKEN`, else console) flow.
- **Verify:** `./build.sh local` (against a freshly built `ak-py/dist` — the `pydanticai`/`logfire`
  extras are not on PyPI yet), then `.venv/bin/python demo.py` shows the nested span tree; `logfire
  auth` then a rerun streams to the dashboard.

## Iteration 3: Tests

- **Goal:** Full suite green, including the new provider coverage and the pre-existing `_FakeTrace`
  fix.
- **Files:** `ak-py/tests/test_trace_logfire.py` (new); `ak-py/tests/test_trace.py` (edit).
- **Steps (spec "Testing"):**
  1. Fix `_FakeTrace` in `test_trace.py`: add the `pydanticai()` method #531 made abstract — without
     it `test_trace_byo_dotted_path` fails to instantiate the class.
  2. Add `test_trace_logfire_missing_extra_raises_friendly_import_error` to `test_trace.py`,
     mirroring the langfuse/openllmetry cases; evict `agentkernel.trace.logfire.logfire` from
     `sys.modules` so the top-level `import logfire` re-executes.
  3. Add `test_trace_logfire.py` (guard `pytest.importorskip("logfire")`): complete-`BaseTrace`,
     `init()`-configures-once-with-`if-token-present`-and-`scrubbing`, `_keep_session_id` allowlist
     (pure-function), runner-instruments-on-construction, `run()`-wraps-in-span (patch `logfire.span`
     + `PydanticAIRunner.run`).
- **Verify:** `cd ak-py && uv run pytest tests/test_trace.py tests/test_trace_logfire.py`, then full
  `uv run pytest` + `make lint-check-all`.

## Iteration 4: Docs

- **Goal:** User-facing docs and README present Logfire as a third first-class provider.
- **Files:** `docs/docs/advanced/traceability.md`, `README.md`.
- **Steps (spec "Examples and docs"):**
  1. `traceability.md`: add Logfire to the supported-platforms list and the mermaid sink node; add a
     "Getting Started with Pydantic Logfire" section (install, config, auto-detect credentials,
     features), a "Viewing Traces → In Pydantic Logfire" subsection, a troubleshooting subsection, a
     data-handling line, a Related-Resources link, and update the summary to three platforms.
  2. `README.md`: add Pydantic Logfire to the two observability lines.
- **Verify:** docs site builds; `grep -rn "ogfire" docs/docs/advanced/traceability.md README.md`
  shows the new entries.

## Iteration 5: Sync skills and docs

- **Goal:** Repo developer/user guidance names three tracing providers, not two.
- **Files (verified this pass — expected touch points):**
  - `.agents/skills/ak-dev-architecture/SKILL.md:369-373`: add `logfire/` to the `trace/` directory
    tree (currently lists only `langfuse/` and `openllmetry/`).
  - `.agents/skills/ak-dev-new-tracing-provider/SKILL.md`: the "beyond Langfuse and
    OpenLLMetry/Traceloop" line (`:6`), `_BUILTIN_TRACERS` example (`:191`), and the
    "(langfuse, openllmetry)" description (`:230`) now include `logfire`.
  - `.agents/skills/ak-dev-new-framework-integration/SKILL.md:285,331`: "There are **two** tracing
    backends … under both `trace/langfuse` and `trace/openllmetry`" becomes three (a new framework
    also needs a `trace/logfire/<name>.py` runner).
  - `.agents/skills/ak-dev-testing-conventions/SKILL.md:84`: add a `test_trace_logfire.py` row to the
    test-file table.
  - `.claude/skills/` mirror: rely on the `chore(auto): sync skills/docs` automation, or copy the
    changed dev skills in the same PR to keep the diff self-contained.
- **Steps:** run `ak-dev-sync-skills-from-branch` and `ak-dev-sync-docs-from-branch`; the list above
  is the expected surface set to confirm against.
- **Verify:** `grep -rn "openllmetry" .agents/skills` — every two-provider enumeration now also names
  `logfire`; both sync flows report clean.

---

## Definition of done

- [ ] `trace.type: logfire` traces all six adapters transparently (no agent-code change); without
      the extra, selecting it raises the actionable `agentkernel[logfire]` `ImportError` (spec
      "Error handling"); base install unaffected.
- [ ] Auto-detect destination works: cloud with `LOGFIRE_TOKEN`/`logfire auth`, console otherwise.
- [ ] `Logfire` implements all seven `BaseTrace` methods; `logfire.configure()` runs once per
      process (thread-safe guard).
- [ ] Full `ak-py` suite green (minus credential-gated e2e per AGENTS.md), including the `_FakeTrace`
      fix; `make lint-check-all` passes.
- [ ] CLI example builds via `./build.sh local` and emits traces (console and, with a token,
      dashboard).
- [ ] `traceability.md`, `README.md`, and the four dev-skill surfaces updated; no existing adapter,
      provider, `AKConfig` field, or `BaseTrace` method changed.
- [ ] `session_id` is visible (not scrubbed) via the `_keep_session_id` allowlist callback, with all
      other scrubbing intact (design.md Decisions).
- [ ] Commits/PR follow `ak-dev-code-quality`; PR targets `develop`.

## Risks and notes

- **`session_id` scrubbing is resolved** (design.md Decisions, spec "Behavioural notes" #1): a
  `scrubbing` callback allowlists the `session_id` attribute (Logfire would otherwise redact it for
  matching "session"); the key is kept, not renamed. Do not drop the callback when touching
  `configure()` — the attribute silently re-scrubs.
- **`_FakeTrace` fix is not optional.** It is a pre-existing breakage in `test_trace.py`
  (`test_trace_byo_dotted_path` cannot instantiate `_FakeTrace` since #531 made `pydanticai()`
  abstract); this change must include the fix or that test stays red.
- **Instrumentation asymmetry is deliberate** (spec "Behavioural notes" #4): native `instrument_*`
  where Logfire has it (Pydantic AI, OpenAI), reused OpenInference instrumentors for CrewAI/ADK and
  the model-agnostic LangChain instrumentor for LangGraph, span-only for Smolagents. Do not
  "normalise" the runners to one mechanism.
- **Pre-publish example build.** The `pydanticai`/`logfire` extras are not on PyPI, so the example
  must be built with `./build.sh local` against a locally built `ak-py/dist`; plain PyPI installs
  silently omit the `logfire` package. Same-version (`0.6.1`) local vs PyPI wheels require pinning
  the local wheel — a build-script concern, not a provider defect.
- **Purely additive.** Only `trace/trace.py` (one branch), `pyproject.toml` (one extra), and the
  `_FakeTrace` test fix touch existing files; everything else is new. Keep the diff reviewable on
  that basis.
