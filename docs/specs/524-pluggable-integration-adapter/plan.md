# #524: Pluggable request/response adapter for integrations — Implementation Plan

Each iteration leaves the branch working and testable. Platforms migrate one at a time (per
spec.md's per-platform sections) so a partially-complete branch never has more than one platform
mid-migration; every other platform keeps working on its current synchronous path until its own
iteration lands.

## Iteration 1: Shared enqueue core + `ChatService` cleanup

- **Goal:** The generic REST path is unaffected but now routes through the extracted
  `enqueue_run_request()`; `ChatService`'s sync-path `session_id` check matches async/stream.
- **Files:** `deployment/common/queue_request_handler.py` (edit), `core/chat_service.py` (edit)
- **Steps:**
  1. Add module-level `enqueue_run_request()` per spec.md's "Shared enqueue core" section.
  2. Rewrite `QueueRequestHandler.get_router()`'s `enqueue_and_wait` to call it, keeping the
     `REST_SYNC`/`REST_ASYNC` branching unchanged.
  3. `chat_service.py:488`: `if req.session_id is None:` → `if not req.session_id:`.
- **Verify:** existing REST/queue tests pass unchanged; add the `session_id=""` sync-path case
  (spec.md Testing).

## Iteration 2: `integration/adapter/` package

- **Goal:** `InboundAdapter`/`OutboundAdapter` ABCs and `IntegrationAdapterFactory` exist,
  independently testable with fakes — no platform wired to them yet.
- **Files:** `integration/adapter/__init__.py`, `base.py`, `factory.py` (all new)
- **Steps:**
  1. `base.py`: `InboundAdapter`, `OutboundAdapter`, `handle_webhook()` per spec.md.
  2. `factory.py`: `IntegrationAdapterFactory.get_inbound`/`get_outbound`, if/elif + `resolve_dotted`
     fallback, matching `guardrail/guardrail.py`'s shape.
- **Verify:** `test_integration_adapter_base.py`, `test_integration_adapter_factory.py`.

## Iteration 3: Agent Runner + Response Handler / `ECSOutputConsumer` generalization

- **Goal:** The queue pipeline forwards arbitrary custom attributes and dispatches on an
  `integration` attribute end to end — provable with a fake adapter before any real platform uses
  it.
- **Files:** `deployment/aws/serverless/akagentrunner.py`, `deployment/aws/containerized/akagentrunner.py`,
  `deployment/aws/serverless/akresponsehandler.py`, `deployment/aws/containerized/akoutputconsumer.py`
- **Steps:**
  1. `ServerlessAgentRunner`/`ECSAgentRunner`: generalize `_get_record_attributes`/
     `_send_to_output_queue` to opaque passthrough (spec.md "Agent Runner" section). Leave
     `ServerlessStreamAgentRunner` untouched.
  2. `ResponseHandler`/`ECSOutputConsumer`: add the `integration`-attribute branch and
     `_dispatch_to_integration` (calls `IntegrationAdapterFactory.get_outbound`), ahead of the
     existing `ExecutionMode` branching. Mirror in `on_permanent_failure`.
- **Verify:** new `_get_record_attributes`/`_send_to_output_queue` forwarding tests; new
  `ResponseHandler`/`ECSOutputConsumer` integration-branch tests (mocked `IntegrationAdapterFactory`).
  Existing `test_akresponsehandler.py`, `test_ecs_sqs_consumer_parallel.py`,
  `test_akagentrunner_stream.py` still pass unchanged.

## Iteration 4: Slack

- **Goal:** Slack runs end-to-end through the adapter/queue path; old inline
  `AgentService.run_multi()` call is gone.
- **Files:** `integration/slack/adapter.py` (new), `integration/slack/slack_chat.py` (edit)
- **Steps:**
  1. `SlackInboundAdapter`/`SlackOutboundAdapter` per spec.md's Slack subsection (`verify()`
     no-op; `parse`/`native_request_id`/`reply_to`; chunking moved from `_split_reply`).
  2. Rewrite the Bolt listener to call the adapter methods + `enqueue_run_request` directly
     (not `handle_webhook`, per the Bolt exception).
  3. Acknowledgement stays a synchronous send from the listener; outbound delivery posts a new
     message instead of editing it (behavioral change 3).
- **Verify:** `test_slack_adapter.py`; manual/integration smoke test against a real or sandboxed
  Slack workspace if available.

## Iteration 5: WhatsApp

- **Goal:** WhatsApp migrated.
- **Files:** `integration/whatsapp/adapter.py` (new), `integration/whatsapp/whatsapp_chat.py` (edit)
- **Steps:** Move `_verify_signature`, parsing, and `_send_message`/chunking into
  `WhatsAppInboundAdapter`/`WhatsAppOutboundAdapter`; route body becomes verify→`handle_webhook`.
- **Verify:** `test_whatsapp_adapter.py`.

## Iteration 6: Messenger + Instagram

- **Goal:** Both migrated together — near-identical shape (shared Graph API pattern, HMAC
  verification, GET challenge handshake).
- **Files:** `integration/messenger/adapter.py`, `integration/instagram/adapter.py` (new),
  `integration/messenger/messenger_chat.py`, `integration/instagram/instagram_chat.py` (edit)
- **Steps:** Same shape as Iteration 5, per platform; preserve Instagram's `is_echo` filter and
  Messenger's `service.run()` (non-multi) fallback inside `parse()`/the adapter, whichever owns
  request construction.
- **Verify:** `test_messenger_adapter.py`, `test_instagram_adapter.py`.

## Iteration 7: Telegram

- **Goal:** Telegram migrated, including its `BackgroundTasks`-deferred dispatch calling
  `handle_webhook` instead of `_process_webhook_body`.
- **Files:** `integration/telegram/adapter.py` (new), `integration/telegram/telegram_chat.py` (edit)
- **Steps:** Move secret-token compare, parsing, and chunked `_send_message` into
  `TelegramInboundAdapter`/`TelegramOutboundAdapter`; new use of `update_id` for
  `native_request_id`.
- **Verify:** `test_telegram_adapter.py`.

## Iteration 8: Teams (+ `_TeamsConfig`)

- **Goal:** Teams migrated and, for the first time, actually constructible (today's
  `Config.get().teams.*` reads raise `AttributeError`).
- **Files:** `core/config.py` (edit — add `_TeamsConfig`), `integration/teams/adapter.py` (new),
  `integration/teams/teams_chat.py` (edit)
- **Steps:**
  1. Add `_TeamsConfig` and mount it on `AKConfig` per spec.md.
  2. `TeamsInboundAdapter`/`TeamsOutboundAdapter`: `verify()` no-op (Bot Framework validates);
     `reply_to()` returns a serialized `ConversationReference`; `deliver()` uses
     `BotFrameworkAdapter.continue_conversation`.
  3. Rewrite `bot_logic` callback to call the adapter methods + `enqueue_run_request` directly
     (same Bot-Framework-fuses-verification shape as Slack's Bolt exception).
- **Verify:** `test_config.py` (`_TeamsConfig` defaults/env override), `test_teams_adapter.py`.

## Iteration 9: Full-suite regression pass

- **Goal:** Everything from Iterations 1-8 verified together.
- **Steps:** `cd ak-py && uv run pytest`; `make lint-check-all`.
- **Verify:** full suite green; no leftover references to the deleted inline
  `AgentService.run_multi()` calls in any of the 6 migrated handlers (`grep -rn "run_multi"
  integration/`).

## Iteration 10: Sync docs and skills

- Update `.agents/skills/ak-dev-new-messaging-integration`: its Step 2 template
  (`Agent<Platform>RequestHandler` parsing inline and sending inline) no longer matches the new
  pattern — rewrite the walkthrough around an `InboundAdapter`/`OutboundAdapter` pair plus a thin
  route shell, and update its "Exception: Gmail" note to also cover Slack/Teams's
  `handle_webhook`-bypass exception.
- Update `.agents/skills/ak-dev-architecture`: add `integration/adapter/` to the directory
  structure listing, next to the existing `integration/` entry; note the house factory pattern
  now also covers integration adapters (alongside guardrail/sandbox/store/trace) in the
  "Plugin architecture" bullet.
- `docs/docs/integrations/*.md` (Slack/WhatsApp/Messenger/Instagram/Telegram/Teams pages): confirm
  whether any page documents the current synchronous-handler behavior or the acknowledgement
  edit-in-place UX (Slack) — update or confirm no change needed, per each page.
- Confirm with the `ak-dev-sync-docs-from-branch` / `ak-dev-sync-skills-from-branch` flows before
  merge.
