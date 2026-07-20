# TypeScript agent framework landscape (observed 2026-07-19/20)

Survey of TS/JS agent frameworks as candidates for Agent Kernel framework adapters.
GitHub stars read from the GitHub API 2026-07-19/20; npm downloads are the week
2026-07-12 → 2026-07-18 from the npm registry API. Download counts include transitive
and CI installs — treat all numbers as order-of-magnitude signals.

## Comparison table

| Framework (package) | Backer / License | Stars | npm dl/wk | Maturity & cadence | Core abstractions | Wrappability for an external adapter | Overlap with kernel features |
|---|---|---|---|---|---|---|---|
| **Vercel AI SDK** (`ai`) | Vercel / Apache-2.0 | 25.7k | 16.3M | v5 (2025) → v6 GA Dec 2025 → v7 GA Jun 2026; fast cadence | `Agent` interface + `ToolLoopAgent` default loop; `generateText`/`streamText`; Zod tools; v7 adds `WorkflowAgent` (durable) and `HarnessAgent`; MCP stable | **Excellent** — pure library, no server; `Agent` is an interface you can implement | Telemetry (OTel), tool approvals/HITL, sandbox support (v7); all opt-in |
| **@anthropic-ai/claude-agent-sdk** | Anthropic / **not OSI** — Anthropic Commercial ToS | 1.6k (TS repo) | 7.7M | v0.3.x, very frequent releases; TS repo created Sep 2025 | `query()` (one-shot streaming) + stateful client; built-in tools, subagents, hooks, permissions, sessions, MCP | **Moderate** — a harness around the Claude Code runtime (platform-specific native binaries, Node ≥18), not a composable loop; wrap as a black-box agent | Very high: owns sessions, permission hooks, tools, sandboxing; Claude models only (plus Bedrock/Vertex) |
| **LangGraph.js** (`@langchain/langgraph`) | LangChain / MIT | 3.1k (langgraphjs; langchainjs 18k) | 2.7M | 1.0 in Python + JS simultaneously (Oct 2025); stable | `StateGraph`, nodes/edges, `invoke()`/`stream()`, checkpointers (Memory/SQLite/Postgres/Redis), interrupts/HITL, `createAgent` on the LangGraph runtime | **Excellent** — library-style `graph.invoke(input, config)`; checkpointer injected; no server required (LangGraph Platform optional) | LangSmith tracing optional; memory via checkpointers is pluggable/replaceable |
| **@openai/agents** | OpenAI / MIT | 3.4k | 1.34M | GA Jun 2025; 0.x but frequent releases; new 2026 features land Python-first | `Agent`, `run(agent, input)`, handoffs, guardrails (input/output/tool), sessions, tracing, realtime voice agents | **Excellent** — literally `run(agent, input)`; `Model`/`ModelProvider` interface for non-OpenAI models; `setTracingDisabled()` | Built-in tracing (disable-able/exportable), guardrails, sessions — all replaceable; mirrors the Python SDK AK already wraps |
| **Mastra** (`@mastra/core`) | Mastra Inc. (ex-Gatsby, YC W25) / Apache-2.0 (+ proprietary `ee/`) | 26.3k | 1.15M | 1.0 Jan 2026; weekly changelogs; Node ≥ 22.13 | Agents (`agent.generate()`/`stream()`), durable graph workflows, memory, RAG, evals, MCP; built on AI SDK model layer | **Good with caveats** — agents usable as a library, but `mastra build` wants to emit its own Hono server; server adapters can mount it in an existing app | High: bundles memory/storage, OTel telemetry, evals, its own server + studio; components pluggable |
| **AWS Strands** (`@strands-agents/sdk`) | AWS / Apache-2.0 | 0.7k (TS repo) | 343k | TS preview Dec 2025 → 1.0 Apr 2026, now ~1.9 | model-driven `Agent` loop, tools, plugins, multi-agent + A2A, structured output (Zod), session persistence (file/S3, pluggable) | **Excellent** — plain library; Node, browser, Lambda, Bedrock AgentCore; AbortSignal cancellation | OTel integration and session persistence built in but pluggable |
| **Google ADK JS** (`@google/adk`) | Google / Apache-2.0 | 1.3k | 168k | Official TS ADK launched 2026 (repo Aug 2025); part of Py/TS/Go/Java rollout | Same code-first ADK model as Python: agents, tools, `Runner`, sessions, dev UI, `adk` CLI | **Excellent conceptually** — Runner/Session map 1:1 to the Python ADK AK already adapts; young API | Dev UI/CLI optional; deployment-agnostic |
| **VoltAgent** (`@voltagent/core`) | VoltAgent (startup) / MIT | 10.1k | 12.5k | 1.x (2026); active | Agents, supervisor + sub-agents, workflow engine, Zod tools + lifecycle hooks, MCP; guardrails (Jun 2026) | **Good** — 1.x split core from server; `@voltagent/server-hono` optional | VoltOps observability console (optional), memory, guardrails, sandbox providers |
| **Inngest AgentKit** (`@inngest/agent-kit`) | Inngest / Apache-2.0 | 0.9k | 15.3k | 0.x; `inngest` required peer dep since v0.9 | Agents → Networks with a Router and shared Network State; every model/tool call is a checkpointed Inngest step | **Moderate** — `network.run()` works standalone, but the durability value requires the Inngest engine | Durable execution, retries, state — coupled to Inngest infra |

Sources: [mastra.ai](https://mastra.ai/), [Mastra 1.0 announcement](https://mastra.ai/blog/announcing-mastra-1), [mastra-ai/mastra](https://github.com/mastra-ai/mastra), [Mastra server adapters](https://mastra.ai/docs/server/server-adapters), [Building Mastra](https://mastra.ai/docs/deployment/building-mastra); [openai-agents-js docs](https://openai.github.io/openai-agents-js/), [repo](https://github.com/openai/openai-agents-js); [AI SDK 6](https://vercel.com/blog/ai-sdk-6), [AI SDK 7](https://vercel.com/blog/ai-sdk-7), [AI SDK agents docs](https://ai-sdk.dev/docs/agents/overview); [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview), [TS reference](https://platform.claude.com/docs/en/agent-sdk/typescript), [repo](https://github.com/anthropics/claude-agent-sdk-typescript); [langgraphjs](https://github.com/langchain-ai/langgraphjs), [LangChain+LangGraph 1.0](https://blog.langchain.com/langchain-langgraph-1dot0/); [VoltAgent](https://voltagent.dev/), [server architecture](https://voltagent.dev/docs/api/server-architecture/); [Strands TS 1.0](https://strandsagents.com/blog/strands-agents-typescript-v1/), [AWS announcement](https://aws.amazon.com/about-aws/whats-new/2025/12/typescript-strands-agents-preview/); [google/adk-js](https://github.com/google/adk-js), [Google ADK TS launch](https://developers.googleblog.com/introducing-agent-development-kit-for-typescript-build-ai-agents-with-the-power-of-a-code-first-approach/); [AgentKit](https://agentkit.inngest.com/), [networks](https://agentkit.inngest.com/concepts/networks); comparison surveys: [ayautomate](https://www.ayautomate.com/blog/best-typescript-ai-agent-frameworks), [speakeasy](https://www.speakeasy.com/blog/ai-agent-framework-comparison/), [particula](https://particula.tech/blog/mastra-vs-langgraph-vs-vercel-ai-sdk-typescript-agents).

## Caveats on the numbers

- `ai` (16.3M/wk) is the general LLM toolkit for the whole JS ecosystem, not just agents — it
  underlies Mastra's and VoltAgent's model layer, so its downloads overstate "agent framework" usage.
- `@anthropic-ai/claude-agent-sdk` (7.7M/wk) is inflated by the Claude Code ecosystem and CI installs.
- `@mastra/core`'s observed 1.15M/wk is well above the ~150–300k cited in spring-2026 articles —
  growth plus transitive installs.
- License flags: Claude Agent SDK is governed by Anthropic's Commercial Terms of Service
  ("SEE LICENSE IN README"), not an OSI license — relevant if Agent Kernel redistributes adapters.
  Mastra is Apache-2.0 with a proprietary `ee/` carve-out. Everything else in the table is MIT or
  Apache-2.0.
- There is no TypeScript Smolagents or CrewAI (CrewAI remains Python-only), so the TS adapter
  lineup cannot mirror the Python lineup 1:1. Conversely, TS has official ADK and OpenAI Agents
  SDKs that do mirror existing Python adapters.

## Adapter-fit ranking

1. **@openai/agents** — `run(agent, input)` is exactly the adapter contract; disable-able tracing;
   sessions and guardrails map onto AK capabilities; symmetric with the existing Python OpenAI adapter,
   so the TS core's shape gets validated against a known quantity.
2. **LangGraph.js** — `invoke/stream` + injected checkpointer, no lifecycle ownership; symmetric with
   the Python LangGraph adapter; 1.0-stable.
3. **Vercel AI SDK Agent/ToolLoopAgent** — pure library, biggest ecosystem by far; wrapping it also
   transitively future-proofs Mastra/VoltAgent users; watch overlap creep in v7 (WorkflowAgent,
   telemetry, sandbox).
4. **Google ADK JS** — near-zero conceptual porting cost from the existing ADK adapter; adoption
   still early (1.3k stars / 168k dl), so a fast-follow rather than a launch driver.
5. **Mastra** — largest TS-native community; wrap at the `Agent` level and use server adapters, but
   expect friction with its bundled storage/telemetry/server and Node ≥22.13 floor.
6. **Claude Agent SDK** — huge usage but architecturally a subprocess harness with non-OSI licensing;
   adapt as a black-box agent, not a loop AK controls.
7. **Strands TS / VoltAgent / Inngest AgentKit** — clean designs but smaller adoption. Strands is
   worth tracking given AK's AWS deployment story and Strands' OTel/A2A/S3-session alignment.

## Runtime / deployment notes

- **Node baseline is now 22/24 LTS.** Mastra 1.0 requires Node ≥ 22.13; Claude Agent SDK ≥ 18.
  Node 22/24's `require(esm)` support has largely defused the ESM/CJS dual-package problem
  ([letsbuildsolutions](https://letsbuildsolutions.com/blog/web-engineering/bun-vs-node-js-in-2026-runtime-performance-ecosystem-compatibility-and-migration-strategies/)) —
  an `ak-ts` package should ship ESM-first with CJS compat, targeting Node ≥ 20/22.
- **Bun is production-viable but a tracing liability.** APM agents and OTel auto-instrumentation
  patch V8/Node internals and only partially work under Bun
  ([strapi benchmark roundup](https://strapi.io/blog/bun-vs-nodejs-performance-comparison-guide)).
  Since tracing (Langfuse/OpenLLMetry) is a core AK capability, Node should be the supported runtime
  with Bun best-effort.
- **Serverless-vs-worker bias varies by framework.** Vercel AI SDK ergonomics center on
  Next.js/serverless routes (v7's `WorkflowAgent` exists precisely because plain loops don't survive
  restarts); Mastra's `mastra build` emits a self-contained Hono server that is container-friendly,
  and server adapters let it mount inside an existing app
  ([mastra docs](https://mastra.ai/docs/deployment/overview)); Inngest AgentKit assumes the Inngest
  engine for durability. LangGraph.js, @openai/agents, Strands, and ADK JS are runtime-neutral
  libraries that drop cleanly into AK's long-running ECS/SQS worker model.
- **Claude Agent SDK is the heavy outlier**: platform-specific native binaries, runs the Claude Code
  runtime as a managed subprocess — fine in an ECS container (match image arch to the optional
  dependency), unsuitable for edge/serverless; its filesystem/session state assumes a persistent
  working directory.
- **Edge runtimes** (Cloudflare Workers, browser): Strands TS and parts of @openai/agents support
  them; this matters little for AK's container-based deployment but constrains which Node APIs a
  shared TS kernel core may use if edge support is ever desired.

## Takeaway for the design (proposed, not decided)

Adapt **@openai/agents and LangGraph.js first** (cleanest APIs, direct symmetry with existing Python
adapters), **Vercel AI SDK's Agent interface second** (largest reach, transitively covers
Mastra/VoltAgent users), with **Google ADK JS** as a cheap fast-follow and **Mastra** as the
highest-demand but highest-friction integration; treat **Claude Agent SDK** as a special-case
black-box adapter.
