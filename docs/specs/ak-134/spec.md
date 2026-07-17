# AK-134: Agent-initiated conversations — Implementation Spec

This spec details the implementation of the approved [design.md](design.md): a new `core/initiation/` package (mapping store, manager, system tool), a resolve hook on the request-handler surfaces, initiation delivery through the response handlers' existing `process_message` override point (no new send method), and a `mapping_table` config block plus Terraform table. The design idea: the tool creates the session platform-blind inside the Agent Runner, runs the owning agent with the caller's prompt so the outbound message and its context land in the new session's framework history naturally, and dispatches an `InitiationMessage`; the Response Handler sends it, learns the `messaging_integration_thread_id`, and binds the mapping that the Request Handler later resolves replies through.

## Design

### Feature gate

1. The entire feature is enabled by the **presence of the `mapping_table:` block** in config (mirrors the `thread:` pattern — `_ThreadStoreConfig` docstring, `ak-py/src/agentkernel/core/config.py`). When absent: `InitiationManager.get()` returns `None`, the tool is not registered, resolve hooks return the platform-derived id unchanged, and the response-handler initiation branch is never taken.
2. The core never imports from `deployment/`, `integration/`, or `api/` — dispatchers and senders are *registered into* the core by those layers (same direction as `RESTAPI.run(handlers=...)`).

### New package: `core/initiation/`

```
ak-py/src/agentkernel/core/initiation/
├── __init__.py          # exports: InitiationManager, InitiationSender, InitiationMessage,
│                        #          SessionIdMappingStore, InitiateConversationTool
├── model.py             # InitiationMessage
├── manager.py           # InitiationManager (singleton façade) + InitiationSender (ABC)
├── tools.py             # InitiateConversationTool (SystemTool) + _initiate_conversation()
└── mapping/
    ├── __init__.py      # SessionIdMappingStoreBuilder
    ├── base.py          # SessionIdMappingStore (ABC)
    ├── in_memory.py     # ClassVar dict, ephemeral
    ├── redis.py         # reuses session.redis connection config
    ├── valkey.py        # reuses session.valkey connection config (valkey extra)
    ├── dynamodb.py      # reuses session.dynamodb connection config
    ├── cosmosdb.py      # reuses session.cosmosdb connection config
    └── firestore.py     # reuses session.firestore connection config
```

```python
class InitiationMessage(BaseModel):
    session_id: str                      # created by the tool inside the runner
    message: str                         # agent-generated outbound text
    target: str                          # opaque recipient address — never interpreted by core
    target_details: dict | None = None   # opaque platform extras
    user_id: str                         # recipient id — owns the AK thread; defaults to target
    request_id: str                      # fresh uuid4, satisfies output-queue attribute contract
    type: Literal["initiation"] = "initiation"


class SessionIdMappingStore(ABC):
    @abstractmethod
    def get_session_id(self, messaging_integration_thread_id: str) -> str | None: ...
    @abstractmethod
    def get_messaging_integration_thread_id(self, session_id: str) -> str | None: ...
    @abstractmethod
    def save(self, session_id: str, messaging_integration_thread_id: str) -> None: ...
    @abstractmethod
    def clear(self) -> None: ...


class InitiationSender(ABC):
    """Single-process REST sender contract (queue deployments use the process_message
    override instead — there is no response handler process in single-process REST)."""
    @abstractmethod
    def send_initiation_message(self, target: str, message: str, target_details: dict | None = None) -> str:
        """Send to the platform; return the messaging_integration_thread_id."""


class InitiationManager:
    """Process-wide singleton façade (mirrors ConversationThreadManager.get() / RLock pattern,
    ak-py/src/agentkernel/core/thread/manager.py:78). None when mapping_table is absent."""
    @classmethod
    def get(cls) -> "InitiationManager | None": ...
    @classmethod
    def reset(cls) -> None: ...                                  # testing hook, mirrors thread manager

    # Request-handler direction
    def resolve_session_id(self, messaging_integration_thread_id: str) -> str: ...
        # mapping hit → session_id; miss/error → the given id unchanged

    # Response-handler direction
    def get_messaging_integration_thread_id(self, session_id: str) -> str | None: ...
        # reverse lookup for delivering later replies of an initiated conversation
    def bind(self, session_id: str, messaging_integration_thread_id: str) -> None: ...
        # save if absent (get_session_id first)
    def complete(self, initiation: InitiationMessage, messaging_integration_thread_id: str) -> None:
        """bind() + AK-thread initialization (when thread support is enabled):
        ConversationThreadManager.get_or_create_thread(session_id, user_id=initiation.user_id,
        first_prompt=initiation.message) — no group_id, no explicit name, so the configured
        naming strategy (with its truncation fallback) names it — then
        append_message(session_id, "assistant", initiation.message).
        NEVER raises: internal failures are caught and logged at ERROR, so a caller's
        queue message is not redelivered after a successful platform send (redelivery
        would message the user twice)."""

    # Runner direction
    def register_dispatcher(self, fn: Callable[[InitiationMessage], None]) -> None: ...
    def dispatch(self, initiation: InitiationMessage) -> None: ...  # raises ValueError if none registered
```

3. **Governing rule — one choke point per direction:** every resolve goes through `InitiationManager.resolve_session_id()` and every bind through `complete()`/`bind()`, in all deployment shapes. The queue and single-process paths differ only in *who* performs the platform send (the user's `process_message` override vs. a registered `InitiationSender`).
4. **Governing rule — mapping store follows the session store:** `SessionIdMappingStoreBuilder.build()` switches on `AKConfig.get().session.type` exactly as `SessionStoreBuilder.build()` does (`ak-py/src/agentkernel/core/builder.py:132-162`), including the `valkey` ImportError message pattern (`builder.py:139-144`). Connection settings come from the existing `session.<backend>` config (`_SessionStoreConfig`, url/credentials); only namespace settings (table/collection name, prefix, TTL) come from `mapping_table`.

### Mapping store data model

Both lookup directions must be O(1) on key-value backends, so `save()` writes **two records per mapping**:

| Record key | Value | Consumer |
|---|---|---|
| `thread#<messaging_integration_thread_id>` | `session_id` | Request Handler: `resolve_session_id()` routes inbound replies |
| `session#<session_id>` | `messaging_integration_thread_id` | Response Handler: the user's reply-delivery override calls `InitiationManager.get_messaging_integration_thread_id(session_id)` to deliver later agent replies of an initiated conversation into the same platform thread (§Consumer changes) |

The backends **reuse the shared drivers in `core/util/driver/`** — the same ones every session store and thread store already instantiates with explicit constructor params (config reading stays in the stores, per the drivers' contract):

- **in_memory**: `ClassVar[dict]` (pattern: `InMemoryAttachmentStore`); no driver.
- **redis / valkey**: `RedisDriver` (`ak-py/src/agentkernel/core/util/driver/redis.py:8`) / `ValkeyDriver` (`driver/valkey.py:8`) — lazy connect, ping-reconnect, and retry lifecycle inherited from `_RedisLikeDriver` (`driver/redis_like.py:32`). Instantiated as the session stores do (e.g. `session/valkey.py:26`) but splitting the sources: `url` from `session.redis`/`session.valkey`, `prefix`/`ttl` from `mapping_table`. Records via `driver.set(key, value)` / `driver.get(key)`; `set(..., nx=True)` (`redis_like.py:112`) gives an atomic save-if-absent.
- **dynamodb**: `DynamoDBDriver(table_name=mapping_table.table_name, partition_key="map_key", ttl=mapping_table.ttl)` (`driver/dynamodb.py:23`) — `sort_key` stays `None`, which the driver supports (key built from the partition key alone, `driver/dynamodb.py:79-81`), fitting the hash-only table; TTL > 0 attaches the `expiry_time` attribute on put, matching the Terraform TTL attribute.
- **firestore**: `FirestoreDriver` (`driver/firestore.py:12`) with `collection_name` from `mapping_table`, `project_id`/`database_id` from `session.firestore`; one document per record key.
- **cosmosdb**: `CosmosDBDriver(connection_string=session.cosmosdb.connection_string, table_name=mapping_table.table_name)` (`driver/cosmosdb.py:23`); no TTL (matches `CosmosDBThreadStore`'s no-TTL support).
- `save()` is last-writer-wins and idempotent; no transactional coupling between the two records (a torn write is repaired by the next `bind()`, which re-saves when `get_session_id` misses).

### Initiation tool (`tools.py`)

Registered by `SystemToolFactory.get_all()` (`ak-py/src/agentkernel/core/tool.py:165-179`) when `AKConfig.get().mapping_table` is present, alongside the existing multimodal gate; the description joins the system-prompt suffix via `Agent._setup_system_prompt()` (`ak-py/src/agentkernel/core/base.py:336-353`).

```python
def _initiate_conversation(target: str, prompt: str,
                           user_id: str = "", agent: str = "") -> str:
    """Runs the owning agent with the prompt in a new session; the reply is the outbound
    message. Returns status text, never raises."""
```

Behavior (all failures returned as actionable text, mirroring `_analyze_attachments`, `ak-py/src/agentkernel/core/multimodal/tools.py`):

1. Validate: `target` and `prompt` required; `InitiationManager.get()` not None and a dispatcher registered.
2. `session_id = str(uuid.uuid4())` — always fresh, so it can never collide with the caller's current session (the v1 same-session guard is obsolete and intentionally dropped).
3. `session = Runtime.current().sessions().new(session_id)` (via `ToolContext.get().runtime`).
4. Select the agent (named `agent` param, else first registered — `AgentService.select` default, `ak-py/src/agentkernel/core/service.py:62-65`) and execute `Runtime.current().run(agent, session, [AgentRequestText(prompt)])` on a **dedicated thread with its own event loop** (`asyncio.run` inside `threading.Thread`, joined) — tool functions execute inside a running framework event loop, so `run_until_complete` on the current loop is not safe. The reply text is the outbound `message`; the prompt/reply exchange lands in the new session's framework history naturally (no injection mechanism needed — there is no fixed-message path), and `Runtime.run` already stores the session (`ak-py/src/agentkernel/core/runtime.py:217`).
5. Build `InitiationMessage(session_id, message, target, user_id=user_id or target, request_id=uuid4)` and call `InitiationManager.get().dispatch(...)`. `target_details` stays `None` from the tool: LLM tool schemas must be strict (the OpenAI Agents SDK rejects free-form `dict` parameters — `additionalProperties` is not allowed), so platform extras can only come from custom dispatch paths.
6. Return `f"Conversation initiated. session_id={session_id}"`.

Callers needing near-exact wording embed it in the prompt ("send this message exactly as written: ..."); verbatim output is not guaranteed (design non-goal).

### Dispatchers — who sends the InitiationMessage where

| Deployment | Dispatcher registered by | Dispatch action |
|---|---|---|
| ECS containerized | `ECSAgentRunner.run()` (`ak-py/src/agentkernel/deployment/aws/containerized/akagentrunner.py`) at startup, before the poll loop | `SQSHandler.send_message_to_output_queue(message_body=initiation.model_dump(), attributes={"message_group_id": initiation.session_id, "message_deduplication_id": initiation.request_id}, request_id=initiation.request_id, user_id=initiation.user_id, custom_message_attributes=[CustomAttribute(name="message_type", value="INITIATION", ...)])` (`ak-py/src/agentkernel/deployment/aws/core/sqs_handler.py:353`) |
| Lambda serverless | `ServerlessAgentRunner._get_chat_service()` lazy init (`ak-py/src/agentkernel/deployment/aws/serverless/akagentrunner.py:21-24`) — also from `ServerlessStreamAgentRunner` | same SQS send |
| Single-process REST | `RESTAPI.run()` (`ak-py/src/agentkernel/api/http.py:81`): scans `handlers` for instances of `InitiationSender`; registers a local dispatcher bound to the first match (warn and ignore additional matches) | in-process: `messaging_integration_thread_id = sender.send_initiation_message(target, message, target_details)` then `InitiationManager.get().complete(initiation, messaging_integration_thread_id)` |

When no dispatcher is registered (e.g. REST deployment without a sender handler), `dispatch()` raises `ValueError` and the tool returns the error as text.

### Consumer changes

**Response handlers — initiation delivery through the existing `process_message` override point**

No new send method is added. `QueueConsumer.process_message` (`ak-py/src/agentkernel/deployment/common/queue_consumer.py:29`) is already the overridable processing method, and platform delivery necessarily already lives in user overrides — the stock implementations can only write to the response store or broadcast via WebSocket. Initiation delivery joins the same override.

- **Stock-handler guard**: `ResponseHandler.process_message` (`ak-py/src/agentkernel/deployment/aws/serverless/akresponsehandler.py:94-113`) and `ECSOutputConsumer.process_message` (`ak-py/src/agentkernel/deployment/aws/containerized/akoutputconsumer.py:40-56`) gain one branch before their existing logic: if `SQSHandler.get_message_custom_attributes(record).get("message_type") == "INITIATION"`, log a WARNING ("initiation message received but this handler does not deliver initiation messages — override process_message; see docs") and return. Initiation messages are never written to the response store nor broadcast. `on_permanent_failure` gets the same guard (log only, no response-store error entry — no HTTP caller waits on an initiation).
- **User contract** (documented + shipped as an example): the subclass's `process_message` override
  1. detects the `message_type=INITIATION` attribute,
  2. parses `initiation = InitiationMessage.model_validate_json(<record body>)` (boto3 PascalCase `Body` vs Lambda `body`, per each class's existing record shape),
  3. sends via the platform API (e.g. Slack `chat.postMessage`) and captures the returned `messaging_integration_thread_id` (`ts`),
  4. calls `InitiationManager.get().complete(initiation, messaging_integration_thread_id)` — binds the mapping and initializes the AK thread.
- **Bind-after-send is a user obligation** under this contract: an override that sends but skips step 4 yields context-less replies (no mapping). This is called out in the docs and the example; `complete()` itself never raises (see its contract), so calling it cannot cause a redelivery-driven duplicate send.
- **Reply delivery (ordinary output messages)**: when the same override delivers a normal agent reply to the messaging platform, it calls `InitiationManager.get().get_messaging_integration_thread_id(reply_session_id)` — a hit means the session was agent-initiated and the reply must be threaded under the returned platform thread id; a miss means an ordinary reactive conversation (the session id *is* the platform-derived id, today's behavior). This is the consumer of the mapping table's reverse direction (§Mapping store data model).

**Request handlers — resolve hook**

New mixin in `core/initiation/manager.py` (kept in core so integrations and api can both inherit without new coupling):

```python
class SessionIdResolver:
    def resolve_session_id(self, messaging_integration_thread_id: str) -> str:
        manager = InitiationManager.get()
        return manager.resolve_session_id(messaging_integration_thread_id) if manager else messaging_integration_thread_id
```

- `RESTRequestHandler(SessionIdResolver, ABC)` (`ak-py/src/agentkernel/api/handler.py:15`) — every REST handler, integration handler, and `QueueRequestHandler` inherits the overridable method.
- `QueueRequestHandler.get_router` POST `/api/v1/chat` (`ak-py/src/agentkernel/deployment/common/queue_request_handler.py:60-90`): after validation, `body.session_id = self.resolve_session_id(body.session_id)`; the resolved id is what is enqueued as `message_group_id` and returned in the response (callers must poll with the returned `session_id`).
- Serverless REST router (`ak-py/src/agentkernel/deployment/aws/serverless/core/router/rest_lambda.py:137-141`): resolve via a module-level default (`InitiationManager` lookup identical to the mixin) before `send_message_to_input_queue` — this router is not a `RESTRequestHandler`, so the override point for Lambda deployments is `InitiationManager.resolve_session_id` itself (subclass + `reset()`/re-init not supported; documented limitation).
- Integration handlers wrap their existing derivation with `self.resolve_session_id(...)` at exactly these points: Slack `slack_chat.py:125` (resolve `thread_ts`), WhatsApp `whatsapp_chat.py:277`, Telegram `telegram_chat.py:183`, Messenger `messenger_chat.py:193`, Instagram `instagram_chat.py:211`, Teams `teams_chat.py:141` (resolve `conversation_id`), Gmail `gmail_chat.py:265` and `:411`. `AgentGmailRequestHandler` is not a `RESTRequestHandler` (`gmail_chat.py:23`), so it inherits `SessionIdResolver` directly.

**Verified unchanged:** `ChatService` (`core/chat_service.py`) needs no changes — the runner-side execution path is untouched; resolution happens before requests reach it, and initiation happens inside a tool during a normal run.

### Config changes

New classes in `ak-py/src/agentkernel/core/config.py`, and a new optional root field after `thread` (`config.py:393`):

```python
class _MappingTableConfig(BaseModel):
    """Configuration for the Session ID Mapping table. Presence of this block enables
    agent-initiated conversations. The store backend follows session.type."""

    table_name: str = Field(default="ak-session-id-mapping", description="Table name (DynamoDB / Cosmos DB)")
    collection_name: str = Field(default="ak-session-id-mapping", description="Collection name (Firestore)")
    prefix: str = Field(default="ak:session-map:", description="Key prefix (Redis / Valkey)")
    ttl: int = Field(default=0, description="Mapping TTL in seconds (0 disables; not supported on Cosmos DB)")

class AKConfig(...):
    mapping_table: Optional[_MappingTableConfig] = Field(
        default=None,
        description="Session ID Mapping table for agent-initiated conversations. Feature is enabled only when this block is present.",
    )
```

- No existing field, type, default, or description changes. Existing YAML files and `AK_*` env vars are unaffected; the new block is reachable as `AK_MAPPING_TABLE__TTL` etc. through the existing env mechanism.

### Terraform changes

- `ak-deployment/ak-aws/containerized/dynamodb.tf`: new `aws_dynamodb_table "session_id_mapping"` mirroring `response_store` (`dynamodb.tf:3-21`): `name = "${local.prefix}-session-id-mapping"`, `billing_mode = "PAY_PER_REQUEST"`, `hash_key = "map_key"` (S), TTL attribute `expiry_time`, gated by a new `var.conversation_initiation` flag (default `false`).
- `ak-deployment/ak-aws/containerized/iam.tf`: extend the existing DynamoDB policy statement (`iam.tf:52-63`) with the new table ARN for both task roles (agent runner reads config only — grants go to the IO/output and REST services; the runner container needs no mapping-table access, per the design's Agent Runner blindness rule).
- The serverless module currently defines no DynamoDB tables (verified: `grep aws_dynamodb_table ak-deployment/ak-aws` matches only `containerized/`), so no serverless Terraform change; serverless users provision the table with their session-store tables as today.

### Behavioural changes

All intentional; everything not listed is a non-change.

1. **`POST /api/v1/chat` (queue deployments) may rewrite `session_id`.** When `mapping_table` is enabled and the supplied `session_id` matches a mapped `messaging_integration_thread_id`, the run executes under the mapped session and the response's `session_id` is the mapped one. Callers must poll `GET /api/v1/chat/{session_id}` with the returned id (the existing session-match check at `queue_request_handler.py:161` makes polling with the original thread id return 404 — this is the documented contract, not a bug).
2. **Integration handlers perform one mapping lookup per inbound message** when the feature is enabled (hot path; O(1) key-value get; on store error the lookup falls back to the platform-derived id, so message handling never blocks on the mapping backend).
3. **Stock response handlers gain a guard branch on `message_type=INITIATION`**: such messages are never written to the response store nor broadcast — they are logged (WARNING) and dropped unless a user's `process_message` override delivers them and calls `complete()`.
4. **`initiate_conversation` joins the system tools** on all agents (and the system-prompt suffix grows by its description) in any process where `mapping_table` is configured.
5. **`RESTRequestHandler` gains a concrete base method** (`resolve_session_id`), changing it from a pure ABC to an ABC with one default method — no existing subclass overrides anything by that name (verified: no matches in `ak-py/src`).

**Non-changes:** session store data layout and `Session` serialization; thread store layout; `BaseChatRequest`/`BaseRunRequest` schemas; all existing config fields; `ChatService` behavior; the system pre-hook chain (`Runtime._get_system_pre_hooks()` is untouched — prompt-only initiation needs no injection hook); reactive conversations on every platform when `mapping_table` is absent (all guards and branches inert); public exports of `agentkernel.core` (new symbols are added under `agentkernel.core.initiation`, nothing moves).

## Error handling

| Failure | Where | Behavior |
|---|---|---|
| Mapping store read error (backend down) | `InitiationManager.resolve_session_id` | catch `Exception` at the manager (single choke point; backends raise driver-native errors — `redis.RedisError`, `botocore` `ClientError`, GCP/Azure SDK errors), log ERROR, return the platform-derived id — availability over continuity |
| Initiation message reaches a stock (un-overridden) response handler | `process_message` guard | log WARNING with remediation text, return — message is deleted, never stored/broadcast, no retry (retrying cannot succeed) |
| Platform send raises inside the user's `process_message` override (API error, policy window) | user override | propagate (like any processing failure) — existing SQS retry semantics apply, then `on_permanent_failure` (whose initiation guard logs only — no response-store error entry, since no HTTP caller waits on an initiation) |
| Bind or thread-init fails after a successful send | `InitiationManager.complete()` | `complete()` catches `Exception` internally, logs ERROR, never raises — raising from the user's override after the send would redeliver and **resend the platform message** (duplicate outreach is worse than a degraded mapping; the reply then falls back to the platform-derived session id) |
| No dispatcher registered / manager disabled / bad params | tool | return actionable error text, never raise into the framework (pattern: `_analyze_attachments`) |
| `valkey` extra missing with `session.type: valkey` | `SessionIdMappingStoreBuilder` | same `ImportError` message pattern as `SessionStoreBuilder` (`builder.py:139-144`) |
| Reply arrives between send and bind | — | accepted race: resolve falls back to the platform-derived id for that message; the initiated session's history is complete before the send, so subsequent correctly-resolved replies have full context; documented limitation |
| Thread-naming model unavailable | `complete()` → naming strategy | existing `ThreadNamingStrategy` truncation fallback; no new handling |

Concurrency: `InitiationManager` is a class-level singleton guarded by an `RLock` (identical to `ConversationThreadManager`); the mapping store instance is created once under that lock, so lazy init cannot race across ECS consumer threads. Store `save/get` calls rely on each backend's thread-safe client (same contract the session stores already assume).

## Testing

New test files (`ak-py/tests/`):

- `test_session_id_mapping.py` — `InMemorySessionIdMappingStore` bidirectional save/get/clear and save-if-absent idempotency; `SessionIdMappingStoreBuilder` follows `session.type` (monkeypatch `agentkernel.core.config.AKConfig.get` with a `FakeCfg` exposing `session.type` + `mapping_table`, per the established pattern); `InitiationManager.get()` returns `None` when `mapping_table` is absent and a manager when present (`InitiationManager.reset()` in an autouse fixture, mirroring thread-manager tests).
- `test_initiation_tool.py` — using `DummyAgent`/`DummyRunner` and an in-memory runtime: the tool creates the session in the store, runs the agent (DummyRunner reply becomes the outbound message, session history persisted), and dispatches an `InitiationMessage` with `user_id` defaulting to `target`; missing dispatcher / missing target / missing prompt return error text without raising.
- `test_initiation_response_handlers.py` — the riskiest consumers: for `ResponseHandler` (Lambda record shape) and `ECSOutputConsumer` (boto3 record shape), an `INITIATION`-attributed record hitting the **stock** handler is logged and dropped — no response-store write, no broadcast, no raise (same for `on_permanent_failure`); a **subclass** override following the documented contract (parse → fake send → `complete()`) binds the mapping (in-memory store via monkeypatched config) and creates the AK thread + first assistant message when `thread` config is present (`ConversationThreadManager.reset()` + `InitiationManager.reset()` between cases); `complete()` swallows a failing mapping store (logs, never raises); a `CHAT_RESPONSE`-attributed record still follows the existing store/broadcast path (patch targets unchanged from `test_akresponsehandler.py`: `ResponseDBHandler`, `BaseWSHandler`, `SQSHandler.get_message_custom_attributes` inputs).
- `test_queue_request_handler_resolve.py` — `POST /api/v1/chat` rewrites a mapped `session_id` before enqueue and returns the resolved id (patch `get_queue_handler`/`get_response_store` fakes); identity fallback when unmapped or disabled.

Existing test files: `test_akresponsehandler.py` must pass unchanged (the new branch triggers only on the new attribute); `test_config.py` gains an assertion that `mapping_table` defaults to `None`. No existing patch targets move.

Run: `cd ak-py && uv run pytest` (format check: `make lint-check`).
