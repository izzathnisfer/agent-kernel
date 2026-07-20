# Cross-language parity: prior art (observed 2026-07-19/20)

How major projects maintain feature parity between Python and TypeScript (and other) SDKs.
Five distinct strategies, in ascending order of engineering cost.

## 1. Independent reimplementation, same team, shared API shape and docs

*OpenAI Agents SDK, LangChain/LangGraph, Google ADK, AWS Strands.*

- OpenAI keeps the TS SDK's primitives (handoffs, guardrails, sessions, tracing) name-for-name with
  Python, but new capability ships **Python-first**: the April 2026 harness/sandbox/code-mode/
  subagents update was Python-only with TS "planned"
  ([openai.com](https://openai.com/index/the-next-evolution-of-the-agents-sdk/),
  [team400 analysis](https://team400.ai/blog/2026-03-openai-agents-sdk-practical-guide)).
- LangChain lifted this to a process: **1.0 released simultaneously in Python and JS with one unified
  doc site and parallel examples** ([blog.langchain.com](https://blog.langchain.com/langchain-langgraph-1dot0/)),
  though practitioners still measure a 4–8-week JS lag per minor release
  ([crewship](https://www.crewship.dev/learn/langgraph-vs-langgraphjs)).
- Parity is enforced by discipline and docs, not shared code.

## 2. Shared wire/persistence contract

- LangGraph's checkpointer backends (Postgres/Redis/SQLite) exist in both languages with compatible
  schemas — the persisted format, not the library, is the parity guarantee.
- MCP (tools) and A2A (agent-to-agent) are becoming the cross-language contracts at the protocol
  level; a framework that speaks them interoperates regardless of implementation language.

## 3. Spec-driven with a compliance matrix

*OpenTelemetry.*

- A language-neutral specification plus a per-language
  [spec-compliance-matrix.md](https://github.com/open-telemetry/opentelemetry-specification/blob/main/spec-compliance-matrix.md)
  tracking which SDK implements which feature; OTLP is the wire contract.
- Parity gaps are explicit and public rather than discovered by users. This is what lets OTel scale
  to 11+ language SDKs maintained by different groups.

## 4. Codegen from an API spec

*Stainless for OpenAI/Anthropic client SDKs* (OpenAPI → TS/Python/Go/Java/…).

- Works well for client SDKs; does not fit in-process framework abstractions (runners, hooks,
  adapters) that have no OpenAPI shape.
- Notable inflection: Stainless announced in May 2026 it is joining Anthropic and winding down its
  hosted SDK generator
  ([stainless.com](https://www.stainless.com/blog/stainless-in-2025-building-the-api-platform-we-always-wanted/),
  [TechCrunch](https://techcrunch.com/2024/12/10/stainless-helps-build-sdks-for-openai-anthropic-and-meta/)).

## 5. Shared native core via FFI

*Temporal `sdk-core` in Rust.*

- The correctness-critical logic (durable-execution state machines, task polling) lives once in
  Rust; language SDKs are thin bindings (neon for TS, PyO3 for Python)
  ([temporal.io](https://temporal.io/blog/why-rust-powers-core-sdk),
  [InfoQ/QCon SF 2025](https://www.infoq.com/news/2025/11/temporal-rust-polygot-sdk/)).
- Strongest consistency guarantee, highest cost — async-model bridging and cross-boundary memory
  management are the documented pain points.

## Applicability to Agent Kernel (proposed, not decided)

Agent Kernel is a wrapper library, so codegen (#4) doesn't apply to the core abstractions and a
shared native core (#5) is overkill. The realistic combination is:

- **#1** — independent TS reimplementation of the Agent/Runner/Module/Session/Runtime contract;
- **#3** — a written adapter-contract spec with a language-neutral conformance fixture suite and a
  public parity matrix (`docs/parity-matrix.md` or similar), so gaps are explicit;
- **#2** — frozen wire/persistence formats as the hard cross-language contract: AKConfig YAML +
  `AK_*` env schema, the REST surface (OpenAPI), the queue message format, the response-store
  record, and session/attachment/thread store schemas (see
  [portability-inventory.md](portability-inventory.md) for their current state).

This is the pattern LangChain (docs + discipline) and OTel (matrix + wire format) converge on.
Codegen (#4) still applies narrowly to the *contract artifacts*: JSON Schema → Zod/TS types for
config and wire models is cheap and keeps the two implementations from drifting.

Plan for a Python-first lag and declare `ak-py` the reference implementation — even OpenAI accepts
one; the matrix makes it honest.

Framework adapters should be explicitly exempt from parity: CrewAI and Smolagents have no TS
equivalents, and TS-only frameworks (Mastra, Vercel AI SDK) have no Python equivalents. The parity
matrix should model adapters as per-language columns, not as gaps.
