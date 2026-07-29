# AK-134: Agent-initiated conversations — Implementation Plan

Builds [spec.md](spec.md) in order; every iteration leaves the branch working and testable. Section references (§) point into spec.md.

**Post-review revision:** iterations below describe the original implementation (PR #362), which used a
`mapping_table:` config block as the feature gate. Following review feedback, that block was replaced with
`AKConfig.conversation_initiation_enabled` (auto-enabled in queue mode, explicit `session.initiation.enabled` opt-in for REST) and
namespace/TTL derivation from the session store's own settings — see the current [spec.md](spec.md) §Feature
gate and §Config changes for the as-built config shape. The iterations below are left as the historical
implementation record and are not updated line-by-line for the new config field names.

## Iteration 1: Config block + Session ID Mapping store

- **Goal:** `mapping_table:` config parses and `the session store.build()` returns the backend matching `session.type`; feature still inert everywhere else.
- **Files:** `ak-py/src/agentkernel/core/config.py`; new `ak-py/src/agentkernel/core/initiation/__init__.py`, `initiation/mapping/{__init__,base,in_memory,redis,valkey,dynamodb,cosmosdb,firestore}.py`; new `ak-py/tests/test_session_id_mapping.py`
- **Steps:**
  1. Add `_MappingTableConfig` and the optional `mapping_table` root field (§Config changes).
  2. Implement `MappingStore` ABC and the six backends with the two-records-per-mapping model, reusing the shared `core/util/driver/` drivers as the session/thread stores do (§Mapping store data model).
  3. Implement the builder following `session.type`, including the valkey ImportError pattern (§rule 4).
- **Verify:** `uv run pytest tests/test_session_id_mapping.py tests/test_config.py` (add the `mapping_table` default-None assertion to `test_config.py` here).

## Iteration 2: InitiationManager + InitiationMessage

- **Goal:** the core façade works end-to-end in isolation: resolve/bind/complete/dispatch.
- **Files:** new `initiation/model.py`, `initiation/manager.py`; manager cases added to `ak-py/tests/test_session_id_mapping.py`
- **Steps:** `InitiationMessage` (§New package) and `InitiationManager` singleton with `get()/reset()`, `resolve_session_id`, `bind`, `complete` (never raises), `register_dispatcher`/`dispatch`, plus the `SessionIdResolver` mixin and `InitiationSender` ABC (§New package, §Error handling).
- **Verify:** `uv run pytest tests/test_session_id_mapping.py`.

## Iteration 3: `initiate_conversation` system tool

- **Goal:** an agent run can create a fully-seeded session (via the prompt run) and dispatch an `InitiationMessage`.
- **Files:** new `initiation/tools.py`; `ak-py/src/agentkernel/core/tool.py`; new `ak-py/tests/test_initiation_tool.py`
- **Steps:**
  1. Implement `_initiate_conversation` per §Initiation tool (fresh uuid4 session, prompt run on a dedicated thread — reply becomes the outbound message and history lands naturally, dispatch, error-as-text).
  2. Gate registration in `SystemToolFactory.get_all()` on `mapping_table` presence.
- **Verify:** `uv run pytest tests/test_initiation_tool.py`.

## Iteration 4: Request-handler resolve wiring

- **Goal:** every inbound surface resolves `messaging_integration_thread_id → session_id` through the mixin, with identity fallback.
- **Files:** `ak-py/src/agentkernel/api/handler.py`; `deployment/common/queue_request_handler.py`; `deployment/aws/serverless/core/router/rest_lambda.py`; `integration/{slack/slack_chat,whatsapp/whatsapp_chat,telegram/telegram_chat,messenger/messenger_chat,instagram/instagram_chat,teams/teams_chat,gmail/gmail_chat}.py`; new `ak-py/tests/test_queue_request_handler_resolve.py`
- **Steps:** wire `resolve_session_id(...)` at the eight derivation points listed in §Consumer changes → Request handlers (QueueRequestHandler rewrites and returns the resolved id).
- **Verify:** `uv run pytest tests/test_queue_request_handler_resolve.py tests/test_api_http.py tests/test_serverless_request_handle.py`.

## Iteration 5: Response-handler guards + queue dispatchers

- **Goal:** initiation messages flow runner → output queue, stock handlers guard them, and the subclass contract (send + `complete()`) works.
- **Files:** `deployment/aws/serverless/{akresponsehandler,akagentrunner}.py`; `deployment/aws/containerized/{akoutputconsumer,akagentrunner}.py`; new `ak-py/tests/test_initiation_response_handlers.py`
- **Steps:**
  1. INITIATION guard in both `process_message` and both `on_permanent_failure` (§Consumer changes → Response handlers).
  2. Register the SQS dispatcher in `ECSAgentRunner.run()` and `ServerlessAgentRunner`/`ServerlessStreamAgentRunner` lazy init (§Dispatchers).
- **Verify:** `uv run pytest tests/test_initiation_response_handlers.py tests/test_akresponsehandler.py tests/test_akagentrunner_stream.py tests/test_ecs_sqs_consumer_parallel.py` — the pre-existing files must pass unchanged.

## Iteration 6: Single-process REST dispatch

- **Goal:** a REST deployment with an `InitiationSender` handler delivers and binds in-process.
- **Files:** `ak-py/src/agentkernel/api/http.py`
- **Steps:** `RESTAPI.run()` scans handlers for `InitiationSender`, registers the local dispatcher (send → `complete()`), warns on multiple matches (§Dispatchers).
- **Verify:** `uv run pytest tests/test_api_http.py` plus a local-dispatcher case added to `tests/test_initiation_tool.py`.

## Iteration 7: Example

- **Goal:** a runnable reference for the two user obligations: the `process_message` override contract and (REST mode) the `InitiationSender` handler.
- **Files:** new `examples/api/slack-initiation/` (server, custom output consumer subclass, config.yaml with `mapping_table` + `thread`, README)
- **Steps:** follow §Consumer changes user-contract steps 1–4 in the subclass; show the agent triggering `initiate_conversation`.
- **Verify:** example `build.sh` runs; manual smoke per its README.
- **Done — queue-deployment follow-up:** the REST example above was built first; the queue-deployment
  half (the `process_message` override contract on ECS/Lambda) was deferred and later completed as
  `examples/aws-serverless/slack-initiation/` (three Lambdas) and `examples/aws-containerized/slack-initiation/`
  (two ECS services) — both demonstrate the same two-agent scenario over the queue architecture, with
  channel/thread_ts routing context carried through custom SQS attributes (Agent Runner subclasses
  overriding `_get_record_attributes`/`_send_to_output_queue`). No `agentkernel` library changes were
  needed for either — every piece is a plain subclass of an existing extension point.

## Iteration 8: Terraform

- **Goal:** the mapping table exists in the containerized deployment when enabled.
- **Files:** `ak-deployment/ak-aws/containerized/{dynamodb.tf,iam.tf,variables.tf}`
- **Steps:** `aws_dynamodb_table.session_id_mapping` (hash key `map_key`, TTL `expiry_time`) gated by new `var.conversation_initiation`; extend the IAM statement for the IO/output and REST services only — not the agent-runner role (§Terraform changes).
- **Verify:** `terraform validate` in the module.
- **Post-review update:** the standalone `var.conversation_initiation` flag was removed — since every session store must pair a mapping store (`SessionStore.get_mapping_store()` is abstract), the mapping table and its IAM grant now gate directly on `var.create_dynamodb_memory_table`, the same flag that provisions the DynamoDB session store itself. No separate opt-in/opt-out exists.

## Iteration 9: Tests — consolidation

- **Goal:** full coverage green, formatting clean.
- **Steps:** finish any assertions deferred from iterations 1–6 (all four new test files from §Testing exist by now); confirm no existing patch target moved.
- **Verify:** `cd ak-py && uv run pytest` and `make lint-check-all`.

## Iteration 10: Sync docs and skills

- **Goal:** guidance surfaces match the implementation; superseded documents removed.
- **Steps:**
  1. Dev skills (`.agents/skills/` — `.claude/skills/` is a tracked symlink to it, not a separate copy): `ak-dev-architecture/SKILL.md` — add `core/initiation/` to the package map/directory tree, the `mapping_table` config section, and the response-handler initiation contract; `ak-dev-new-messaging-integration/SKILL.md` — add the `resolve_session_id` wiring step to the new-integration checklist; `ak-dev-testing-conventions/SKILL.md` — add the four new test files to the test-file table.
  2. Docs site: new page under `docs/docs/advanced/` (conversation initiation: config, tool, override contract, REST mode); update `docs/docs/deployment/` pages that describe response-handler customization; `ak-deployment/ak-aws/containerized/README.md` — new table + variable.
  3. Bundled user skills (`ak-py/src/agentkernel/skills/`): check for config-block references; update or record that none apply.
  4. Run the `ak-dev-sync-docs-from-branch` and `ak-dev-sync-skills-from-branch` flows to confirm nothing was missed before merge.
- **Verify:** sync flows report no remaining drift; `git grep send_initiation_message docs/ .agents/` shows only the REST-mode contract.

## Iteration 11: Fold the mapping store into the session store

Done after review. The mapping store had its own top-level config block and its own backend-selection
builder that mirrored `SessionStoreBuilder` — two parallel switches on `session.type` that had to stay
in agreement. Since the mapping store is the session store's companion table, it is now owned by it.

- **Files:** `core/config.py` (top-level `conversation_initiation:` replaced by a nested
  `session.initiation` block); `core/session/base.py` (gains the `MappingStore` ABC beside
  `SessionStore`, plus an abstract `get_mapping_store()`); `core/initiation/mapping/`
  moved to `core/session/mapping/` with `SessionIdMappingStore` renamed `MappingStore` and the
  builder replaced by `build_mapping_store()`; the six session backends;
  `core/initiation/manager.py`; `core/initiation/tools.py` renamed `tool.py`.
- **Steps:** each session store constructs its paired mapping store and returns it from
  `get_mapping_store()`, which is `@abstractmethod` so no backend can omit it; `InitiationManager`
  takes the store from `Runtime.current().sessions()` instead of building one.
- **Supersedes:** the auto-disable degrade path added for the lazy-build failure mode. An
  intermediate revision put a runtime gate in `SessionStoreBuilder.build()` (raise on an explicit
  opt-in, warn when auto-enabled) to catch a session store that supplied no mapping store; making
  `get_mapping_store()` abstract removed the need for it, so both the gate and `core/builder.py`
  are untouched in the final state.
- **Verify:** `uv run pytest` (`test_mapping_store.py`, renamed from `test_session_id_mapping.py`)
  and `make lint-check-all`.
- **Post-review update:** `core/session/mapping/` (one file per backend) was folded one level
  further, into the paired session-store module itself — e.g. `RedisMappingStore` now lives in
  `core/session/redis.py` beside `RedisSessionStore`, not a separate `mapping/redis.py`. The
  Redis/Valkey-shared `_RedisLikeMappingStore` moved into `core/session/redis_like.py` (a
  client-library-free module, so a `valkey`-only install does not need the `redis` extra). This
  removes the `core/session/mapping/` subpackage entirely, addressing review feedback that a
  dotted-path split between a session store and its own mapping store added indirection without
  benefit.
- **Post-review update 2:** the `session.initiation.store` config key and `build_mapping_store()`
  were both **removed** (amithad, `core/config.py:90`). With `get_mapping_store()` abstract, a
  bring-your-own session store necessarily brings its own mapping store, so the key was redundant —
  and misleading, since a BYO session store implements `get_mapping_store()` itself and would
  silently ignore it. Each backend now constructs its pair directly, and `core/session/base.py` is
  a pure ABC module again with no `AKConfig`/`resolve_dotted` imports.
