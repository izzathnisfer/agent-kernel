---
sidebar_position: 4
---

# Agent-Initiated Conversations

Agent Kernel agents can **proactively start a conversation** with a user on a connected messaging
platform (Slack, WhatsApp, Telegram, ...) — and the user's reply continues that same conversation
instead of starting a context-less one. The binding lives in a **Session ID Mapping** table that
associates the new session with the platform's thread identifier (`thread_ts` for Slack).

## Overview

```mermaid
sequenceDiagram
    participant J as Requesting user
    participant AR as Agent Runner
    participant RSH as Response Handler
    participant MAP as Session ID Mapping
    participant Mo as Recipient

    J->>AR: "Inform Monroe that ..."
    AR->>AR: initiate_conversation tool: new session + prompt run
    AR->>RSH: InitiationMessage (session_id, message, target)
    RSH->>Mo: send via your override
    RSH->>MAP: bind session_id ↔ thread id
    Mo->>MAP: reply resolves thread id → session id
    Mo->>AR: continues the initiated session with full context
```

The Agent Runner never learns which messaging platform is used: the recipient `target` is opaque,
and the platform send happens only in the Response Handler role.

## Enabling

Queue-mode deployments (ECS containerized, Lambda serverless) **auto-enable** the feature — no
config needed beyond `execution.queues`. Single-process REST deployments need an explicit opt-in:

```yaml
conversation_initiation:
  enabled: true   # required for single-process REST; queue-mode auto-enables
  # store: my_pkg.my_module.MyMappingStore   # optional bring-your-own (dotted path)
```

The mapping store backend **follows your `session.type`** and reuses its connection settings; its
namespace (table/collection name or key prefix) is derived from the session store's own by
suffixing `-id-mapping` (table/collection) or `id-mapping:` (key prefix), and it reuses the session
store's TTL — no separate namespace config needed:

```yaml
session:
  type: redis
  redis:
    url: "redis://localhost:6379"
    prefix: "ak:sessions:"   # mapping keys land under "ak:sessions:id-mapping:"
```

With the feature enabled:

- the `initiate_conversation` system tool is registered on **all agents**;
- inbound platform messages resolve their thread id through the mapping before selecting a session;
- response handlers recognize initiation messages (`message_type=INITIATION` queue attribute).

Set `conversation_initiation.enabled: false` to force-disable in queue mode, or bring your own mapping store
entirely via `conversation_initiation.store` (a dotted path to a `SessionIdMappingStore` subclass) — bypassing
the `session.type` derivation.

## The `initiate_conversation` tool

`initiate_conversation(target, prompt, user_id="", agent="")`

The tool creates a fresh session, runs the owning agent (`agent` param, else the first registered)
with your `prompt`, and the reply becomes the outbound message — so the new session's history
already contains the exchange before the message is even sent. `user_id` (defaulting to `target`)
owns the conversation; with [thread support](threads.md) enabled, an AK thread is created for the
recipient, named by your configured naming strategy from the outbound text.

There is no fixed-text path: callers needing near-exact wording put it in the prompt
("send this message exactly as written: ..."); verbatim output is not guaranteed.

## Delivering the message

### Queue deployments (Lambda serverless / ECS containerized)

Initiation messages arrive on the **Output Queue** marked with the `message_type=INITIATION`
attribute. Stock response handlers log a warning and drop them — delivery belongs to your
`process_message` override (the same override that already delivers ordinary replies):

```python
from agentkernel.core.initiation import INITIATION_MESSAGE_TYPE, InitiationManager, InitiationMessage
from agentkernel.deployment.aws import ECSOutputConsumer
from agentkernel.deployment.aws.core.sqs_handler import SQSHandler


class MyOutputConsumer(ECSOutputConsumer):
    @classmethod
    def process_message(cls, record):
        attributes = SQSHandler.get_message_custom_attributes(record)
        if attributes.get("message_type") == INITIATION_MESSAGE_TYPE:
            initiation = InitiationMessage.model_validate_json(record["Body"])
            response = slack_client.chat_postMessage(channel=initiation.target, text=initiation.message)
            # Bind the mapping + initialize the AK thread — REQUIRED after a successful send
            InitiationManager.get().complete(initiation, response["ts"])
            return
        super().process_message(record)
```

Two rules to remember:

1. **Always call `InitiationManager.get().complete(initiation, thread_id)` after a successful
   send** — skipping it means replies can't resolve to the initiated session. `complete()` never
   raises, so calling it cannot redeliver the queue message (which would message the user twice).
2. When delivering an *ordinary* reply, look up
   `InitiationManager.get().get_messaging_integration_thread_id(session_id)` — a hit means the
   session was agent-initiated and the reply must be threaded under that platform thread id.

Delivering an *ordinary* reply also needs the platform's routing context (e.g. a Slack channel
id) that the outbound send requires but that `ChatService`'s response body has no room for. The
request handler that receives the inbound message can attach it as a custom queue-message
attribute when it enqueues (`SQSHandler.send_message_to_input_queue(...,
custom_message_attributes=[...])`); an Agent Runner subclass overriding
`_get_record_attributes`/`_send_to_output_queue` carries it from the Input Queue message to the
Output Queue message; your `process_message` override reads it back via
`SQSHandler.get_message_custom_attributes(record)`. See the runnable examples below for the full
pattern end to end — none of it requires changes to Agent Kernel itself, since every step is a
plain subclass of an existing extension point. The same recipe applies to any messaging
integration, not just Slack — only the SDK used for parsing/signing/sending changes.

### Single-process REST deployments

Implement `InitiationSender` on one of your handlers — `RESTAPI.run()` detects it and wires the
in-process dispatcher, which sends and then binds automatically:

```python
class SlackInitiationHandler(AgentSlackRequestHandler, InitiationSender):
    def send_initiation_message(self, target, message, target_details=None) -> str:
        response = ...  # chat_postMessage
        return response["ts"]  # the messaging_integration_thread_id
```

### Runnable examples

- `examples/api/slack-initiation/` — single-process REST deployment.
- `examples/aws-serverless/slack-initiation/` — AWS Lambda serverless deployment (three Lambdas:
  request handler, agent runner, response handler), demonstrating the queue-deployment pattern
  above.
- `examples/aws-containerized/slack-initiation/` — AWS ECS containerized deployment, the same
  pattern adapted to the two-service ECS shape.
- `examples/api/telegram-initiation/` — single-process REST deployment. Telegram has no
  per-message "thread" concept, so the resolved identifier is the chat id itself rather than a
  per-message thread id.
- `examples/api/gmail-initiation/` — single-process polling deployment (no `RESTAPI.run()`), so the
  dispatcher is registered manually via `InitiationManager.register_dispatcher()` instead of the
  automatic `InitiationSender` detection the REST examples get for free.

## Reply resolution

Every request-handler surface resolves inbound conversation ids through the mapping with an
identity fallback (unmapped ids behave exactly as before):

- all messaging integrations (Slack `thread_ts`; WhatsApp number, Telegram chat id, ...) — for Slack,
  a reply must be threaded to continue an initiated conversation: an un-threaded reply's own `ts`
  never matches a bound mapping, so it starts a new session rather than guessing which prior
  conversation it's answering;
- `POST /api/v1/chat` in queue deployments — note that a mapped `session_id` is **rewritten**, and
  the resolved id is returned in the response; poll with the returned `session_id`;
- override `resolve_session_id(messaging_integration_thread_id)` on any handler to customize the logic.

## Deployment scope

Applicable to any deployment with a messaging integration wired in: single-process REST, ECS
containerized, and AWS Lambda serverless (see the runnable examples above). **Not applicable to
CLI** — it's a synchronous local REPL with no messaging platform and no reply-later model, so
there's no delivery surface for a proactively sent message to arrive on.

## Deployment notes

- **ECS containerized Terraform**: the mapping store follows `session.type`, so set
  `conversation_initiation = true` only when the session store itself is DynamoDB
  (`create_dynamodb_memory_table = true`) — it provisions the `-session-id-mapping` DynamoDB table
  (hash key `map_key`, TTL attribute `expiry_time`) with IAM grants for the REST/IO service. The
  Agent Runner gets no grant — it never touches the table. Other session backends (Redis, Valkey,
  ...) need no extra resource; the mapping rides the same store.
- A reply arriving in the instant between the platform send and the mapping bind resolves to a
  platform-derived session (accepted limitation); the initiated session's history is complete
  before the send, so subsequent replies have full context.
