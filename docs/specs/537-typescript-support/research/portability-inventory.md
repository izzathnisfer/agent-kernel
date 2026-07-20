# Portability inventory: language-neutral vs Python-coupled (verified 2026-07-20)

Assessment of how portable `ak-py` is to a TypeScript implementation, and which parts are already
cross-language contracts vs Python-coupled code. All `path:line` citations verified against
`develop` @ `7725b1a5`.

## 1. Repo layout — already a monorepo; `ak-ts/` slots in cleanly

The Python library is fully isolated in `ak-py/`; everything else at root is language-neutral or a
sibling asset. An `ak-ts/` sibling fits the existing convention with no restructuring.

| Top-level dir | Contents | Language coupling |
|---|---|---|
| `ak-py/` | The Python library (`src/agentkernel`), tests, `pyproject.toml` | Python (the thing being ported) |
| `ak-deployment/` | Terraform IaC per cloud: `ak-aws/`, `ak-azure/`, `ak-gcp/`, each with `common/` + `containerized/` + `serverless/` | **Language-neutral** — provisions infra, injects env vars |
| `docs/` | Docusaurus site, `docs/specs/**` | Content neutral; site tooling is JS |
| `examples/` | Runnable examples across `api/`, `cli/`, `aws-*`, `azure-*`, `gcp-*`, `containerized/`, `memory/` — each with its own `config.yaml` | Python app code + neutral `config.yaml` |
| `agent/`, `use-cases/` | Sample apps | Python |
| `.agents/skills/` | Developer skills | Neutral descriptions written against the Python impl |

No TypeScript/JS source exists outside the Docusaurus site. There is no shared schema package or
cross-language contract artifact today.

## 2. Feature surface — `ak-py/src/agentkernel` (~21k LOC of source)

LOC/file counts measured on `develop` @ `7725b1a5` (excluding `__pycache__`):

| Package | LOC | Files | Contents / external services |
|---|---|---|---|
| `core/` | 7,580 | 55 | Session/Agent/Runner/Module/Runtime/AKConfig/hooks/models; session stores (in-memory, Redis, Valkey, DynamoDB, Cosmos DB, Firestore); threads; multimodal (LiteLLM vision) |
| `framework/` | 2,489 | 11 | Adapters: `openai/`, `crewai/`, `langgraph/`, `adk/`, `smolagents/` |
| `integration/` | 3,090 | 15 | Slack, WhatsApp, Messenger, Instagram, Telegram, Teams, Gmail webhooks |
| `deployment/` | 4,107 | 41 | `common/` abstractions + AWS (ECS containerized, Lambda serverless), Azure Functions, GCP Cloud Run |
| `guardrail/` | 839 | 5 | OpenAI Guardrails, AWS Bedrock Guardrails, Walled AI |
| `trace/` | 736 | 17 | Langfuse, OpenLLMetry — each with per-framework traced runners |
| `knowledgebase/` | 824 | 6 | ChromaDB (vector), Neo4j (graph), Starburst/Trino (SQL) |
| `api/` | 663 | 9 | FastAPI REST, A2A server, MCP server |
| `cli/` | 382 | 3 | `ak` skill-management CLI (not an agent runner) |
| `test/` | 347 | 3 | Built-in eval framework (ragas/rapidfuzz) |
| `auth/` | 85 | 2 | PyJWT `AuthValidator` |

Per-provider optional dependency extras are declared in `ak-py/pyproject.toml`.

## 3. Language-neutral contracts (already cross-language)

### 3a. AKConfig YAML + env-var contract

The **schema source of truth is Python** (a Pydantic tree; `class AKConfig` at
`ak-py/src/agentkernel/core/config.py:374`), but the **serialized form** is language-neutral:

- Env prefix `AK_`, nested delimiter `__` (`core/util/config_yaml_util.py:187-188`), `.env` support.
  Example: `AK_EXECUTION__QUEUES__INPUT__URL`.
- YAML path override via `AK_CONFIG_PATH_OVERRIDE` (`config_yaml_util.py:152`), default `config.yaml`.
- Secret injection: `<file:relative/path>` tokens replaced from `AK_SECRETS_PATH`
  (`config_yaml_util.py:41-66`), including partial replacement inside connection strings.
- Source precedence: init args → env → dotenv → YAML (with file secrets) → defaults
  (`settings_customise_sources` in `config_yaml_util.py`).
- Enum-constrained fields carry regex `pattern=` validators (e.g. `session.type`,
  `execution.mode`, `guardrail.*.type`, `trace.type`) — the de-facto spec a TS implementation
  must mirror.

### 3b. REST / webhook API surface (`api/handler.py`, `api/http.py`)

`GET /health`, `GET /api/v1/agents`, `POST /api/v1/chat` (JSON, or SSE when
`execution.mode=stream`), `POST /api/v1/chat-multipart`, `GET /api/v1/chat/{session_id}` (async
poll), plus thread routes, A2A, and MCP mounts.

### 3c. Core wire model (`core/model.py`)

- `BaseRunRequest` (`model.py:217-222`): prompt/agent/session_id/user_id/… plus
  `files`/`images`, `extra="allow"` (unknown fields preserved).
- `AgentRequest` / `AgentReply` unions discriminated by `type`; `ExecutionMode`
  (`rest_sync | rest_async | stream | async`).

### 3d. Queue message format (`deployment/aws/core/sqs_handler.py`)

- Body: `BaseRunRequest` JSON (input queue) / agent response dict (output queue).
- FIFO attributes: `MessageGroupId` defaults to `session_id` (`sqs_handler.py:346`),
  `MessageDeduplicationId` = request id (`sqs_handler.py:219-220`).
- Custom message attributes: `request_id`, `user_id`, and (async/WS) `endpoint_url`.
- Handler code accepts both boto3 PascalCase and Lambda camelCase attribute shapes.

### 3e. Response-store record (`deployment/common/response_store.py`)

`{ session_id, request_id, body }` keyed by `request_id`; poll validates `session_id` match.

### 3f. Terraform (`ak-deployment/`)

Entirely language-neutral. Provisions queues/tables/buckets and **injects their identifiers into
containers as `AK_*` env vars**. A TS runtime reading the same env vars is drop-in compatible with
the existing Terraform — zero IaC changes.

### 3g. Sandbox capability spec (branch `feature/sandbox_capability`, unmerged)

The ak-133 sandbox spec (`docs/specs/ak-133/` on that branch; **not on `develop`** as of
2026-07-20) is a pure language-neutral contract with no implementation in `ak-py/src` yet —
`SandboxBrokerRequest`/`SandboxCompletion` wire shapes, a `sandbox.*` config schema, and
completion events delivered as `BaseRunRequest`-shaped queue bodies. It is the template for
spec-first, per-language implementation.

## 4. Cloud / deployment variants

Targets: AWS (ECS containerized + Lambda serverless), Azure (Container Apps + Functions), GCP
(Cloud Run + Cloud Functions), local. The containerized runtime is queue-decoupled:

- Generic contracts in `deployment/common/`: `QueueConsumer` (`queue_consumer.py:5`),
  `QueueHandler` (`queue_handler.py:7`), `QueueRequestHandler` (`queue_request_handler.py:29`,
  explicitly provider-agnostic: enqueue-and-wait vs enqueue-and-return), `ResponseStore`
  (`response_store.py:9`), `ThreadRunner` (`thread_runner.py:11`).
- AWS-specific bindings: `ECSSQSConsumer`, `ECSAgentRunner`, `ECSOutputConsumer`, `ECSIOHandler`
  under `deployment/aws/containerized/` — thin boto3 + threading code over the generic contracts.

Takeaway: the `common/` abstractions + wire formats (3d/3e) are a portable spec; only the thin
cloud-SDK bindings need per-language reimplementation. Because the contracts are frozen at the
queue boundary, **mixed-language fleets** (one IO container, Python and TS agent-runner containers)
are feasible once serialization is language-neutral (see 5.1).

## 5. Python-coupled hotspots (would NOT translate directly)

1. **Pickle-based session serialization — the single biggest interop blocker.**
   `BinarySerde` uses `pickle.dumps/loads` (`core/session/serde.py:2,24,36`) and is used by every
   persistent session store: `redis.py`, `valkey.py`, `dynamodb.py`, `cosmosdb.py`, `firestore.py`
   (verified by grep for `BinarySerde`). No other language can read these records; a shared-store
   deployment (Py + TS reading the same Redis/DynamoDB) is impossible without replacing serde with
   a language-neutral codec. Caveat: even with JSON, framework-native session state (CrewAI memory,
   LangGraph checkpoints) is framework-shaped — cross-language *session resumption* is only
   realistic where the framework itself has compatible state across languages.

2. **Pydantic as the only config schema.** `AKConfig` (`core/config.py:374`) is the canonical
   schema; there is no standalone JSON Schema artifact. Regex validators, `default_factory`, and
   cross-field `model_validator` logic live in Python. A TS port must either re-derive and manually
   sync a parallel schema (Zod), or the project must first extract a shared JSON Schema.

3. **Python-only framework adapters.** `crewai` and `smolagents` have no TS equivalent;
   `langgraph`/`adk` have TS cousins with different APIs; only `openai` has a near-mirror TS analog.
   `framework/` (~2,489 LOC) is the least portable package, and `trace/` inherits this via
   per-framework traced runners.

4. **Dynamic imports / dotted-path plugin registration.** `Runtime.load()` uses
   `importlib.import_module` (`core/runtime.py:118`); capability factories use lazy per-provider
   imports. The "config string → Python class path" mechanism needs a TS equivalent
   (dynamic `import()` + a registry); the *capability names and config keys* should be the
   cross-language contract, resolution being per-language.

5. **asyncio/threading concurrency idioms.** Async `Runner.run`, `contextvars`-based
   `Session.current()`, `ThreadRunner`'s `threading` + `os._exit(1)` shutdown semantics — all need
   deliberate re-mapping to the Node event-loop/worker model rather than direct translation.

6. **`pydantic-settings` layering + `<file:...>` secret injection** — the multi-source precedence
   behavior is defined by Python code (`core/util/config_yaml_util.py`), not by a spec; must be
   written down for a second implementation to match.

7. **Misc:** LiteLLM as the model gateway (thread naming, multimodal describe/analyze) has no
   perfectly equivalent Node library; Gmail OAuth persists a `token.pickle`; packaging/publish is
   `uv`-specific.

## 6. Existing docs/specs state

- **No portability/TypeScript/multi-language spec exists** on `develop` (searches for
  `typescript|ak-ts|multi-language|language-neutral|polyglot|portab` return only unrelated uses;
  README's "portability" means across frameworks/clouds, not languages).
- `docs/specs/` on `develop` contains: `ak-16/`, `ak-52/`, `ak-63/`, `ak-166/`,
  `213-generic-tool-binding.md`, `agent-skills.md`, `conversation-thread-support.md`,
  `integration-tests.md`. Several define behavior in a reasonably language-neutral way and are
  portability seeds; the sandbox spec (3g) is the best template but is unmerged.

## Bottom line

- **Portable as-is:** config format + env convention, REST surface, wire models, queue +
  response-store formats, `deployment/common/` contracts, all Terraform, the (unmerged) sandbox spec.
- **Needs spec extraction first:** AKConfig (Pydantic → shared JSON Schema) and session-store
  serialization (pickle → versioned JSON envelope) — both valuable to `ak-py` on their own
  (debuggability; pickle is also a security liability).
- **Expect per-language reimplementation:** framework adapters and traced runners, concurrency
  orchestration, plugin loading.
- **Monorepo readiness: high** — `ak-ts/` drops in beside `ak-py/`, reusing `docs/`, example
  configs, and `ak-deployment/` unchanged.
