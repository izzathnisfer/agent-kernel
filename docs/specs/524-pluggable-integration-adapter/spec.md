# #524: Pluggable request/response adapter for integrations — Implementation Spec

This implements `design.md`: a pluggable **inbound adapter** and **outbound adapter** per
messaging platform, so Slack, WhatsApp, Messenger, Instagram, Telegram, and Teams stop calling
`AgentService.run_multi()` synchronously inside their webhook handlers and instead run through
the existing two-SQS-queue pipeline. This spec details the new `integration/adapter/` package,
the exact changes to the Agent Runner / Response Handler / `ECSOutputConsumer` needed to forward
and dispatch on adapter-declared attributes generically, the per-platform adapter design
(including two platforms — Slack, Teams — whose current SDK-driven request handling doesn't map
onto the adapter contract 1:1), and the config/test/error-handling details `design.md` left to
this stage.

Gmail stays out of scope (design.md Non-goals); it keeps its current polling implementation.

## Design

### Shared enqueue core (`deployment/common/queue_request_handler.py`)

Today `QueueRequestHandler.get_router()`'s nested `enqueue_and_wait` (queue_request_handler.py:60-125)
inlines validation, `request_id` minting, and the SQS send. Per design.md's Queue routing
section ("the enqueue core ... is factored out"), extract the validate-mint-send steps into a
module-level function both the REST route and every `InboundAdapter` call:

```python
# deployment/common/queue_request_handler.py

def enqueue_run_request(
    queue_handler: type[QueueHandler],
    body: BaseRunRequest,
    request_id: Optional[str] = None,
    custom_message_attributes: Optional[list] = None,
) -> str:
    """Validate session_id/prompt, mint a request_id if none is supplied, and send to the Input Queue.

    Shared by QueueRequestHandler's POST /api/v1/chat route and every InboundAdapter — the one
    enqueue path, so integrations stop hand-rolling their own queue sends.

    :raises ValueError: body.session_id or body.prompt is empty/None
    :return: the request_id used (the supplied one, or a newly minted uuid4)
    """
```

1. Validates `not body.session_id` / `not body.prompt` (same conditions as today,
   queue_request_handler.py:70-73) and raises `ValueError` — callers translate it to their own
   transport error (`HTTPException(400, ...)` for the REST route; see each adapter's webhook
   route for the integration case).
2. `request_id = request_id or str(uuid.uuid4())` — the one behavioral difference from today:
   `QueueRequestHandler`'s route always minted a fresh uuid4 (queue_request_handler.py:76); the
   shared function accepts a caller-supplied id so `InboundAdapter` can pass a platform-native
   id (see Adapter abstractions).
3. Calls `queue_handler.send_message_to_input_queue(message_body=body.model_dump(), attributes={"message_group_id": body.session_id, "message_deduplication_id": request_id}, request_id=request_id, custom_message_attributes=custom_message_attributes)` — identical shape to today's call (queue_request_handler.py:83-88), plus the new `custom_message_attributes` passthrough.

`QueueRequestHandler.get_router()`'s `enqueue_and_wait` becomes a thin wrapper: call
`enqueue_run_request(self.get_queue_handler(), body)` inside the existing `try/except
HTTPException` block (translating `ValueError` to `HTTPException(400, ...)`), then keep the
unchanged `REST_SYNC`/`REST_ASYNC` branching (queue_request_handler.py:93-119) using the returned
`request_id`. No behavior change for the generic REST client.

### Adapter abstractions (`integration/adapter/`)

New package, framework-agnostic, importing only from `core/` and `deployment/common/` (never
from a specific platform's own module — that direction is factory-resolved, see below):

```
ak-py/src/agentkernel/integration/adapter/
├── __init__.py     # exports InboundAdapter, OutboundAdapter, IntegrationAdapterFactory
├── base.py         # InboundAdapter, OutboundAdapter ABCs
└── factory.py       # IntegrationAdapterFactory — house factory pattern
```

```python
# integration/adapter/base.py
class InboundAdapter(ABC):
    """Normalizes one platform's webhook event into an enqueued BaseRunRequest.

    Instantiated by IntegrationAdapterFactory with the deployment's configured QueueHandler
    (e.g. SQSHandler) — the same handler get_queue_handler() resolves for QueueRequestHandler.
    """

    integration_name: ClassVar[str]                 # e.g. "slack" — carried as the "integration"
                                                     # custom SQS attribute (see Queue routing)

    def __init__(self, queue_handler: type[QueueHandler]):
        self._queue_handler = queue_handler

    @abstractmethod
    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        """Raise on an invalid signature/secret. Must run against raw_body, not a re-serialized
        dict — HMAC verification needs the exact bytes received (see Slack/Teams exception below
        for platforms whose SDK already does this before the adapter is invoked)."""

    @abstractmethod
    def parse(self, event: dict) -> BaseRunRequest:
        """Map the platform event into BaseRunRequest's standard fields: prompt, files, images,
        agent (from the platform's own AKConfig section), user_id, and session_id (this platform's
        current, bare derivation — thread_ts, from_number, etc. — unchanged from today)."""

    @abstractmethod
    def native_request_id(self, event: dict) -> Optional[str]:
        """Return the platform's own idempotency id (see the per-platform table below), or None
        to fall back to a minted uuid4."""

    @abstractmethod
    def reply_to(self, event: dict) -> dict[str, str]:
        """Flat platform identifiers the paired OutboundAdapter needs to address the reply."""

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> str:
        """Template method: verify -> parse JSON -> parse -> enqueue with 'integration' + reply_to
        as custom SQS attributes. Returns the request_id used. Not used by Slack/Teams — see below."""


class OutboundAdapter(ABC):
    """Formats and delivers one platform's reply; paired with an InboundAdapter of the same
    integration_name under IntegrationAdapterFactory."""

    integration_name: ClassVar[str]

    @abstractmethod
    def format(self, reply_body: dict) -> list[str]:
        """Render the agent reply payload into this platform's message chunks (its existing
        chunk size/splitter, moved from the current handler — see Consumer changes)."""

    @abstractmethod
    def deliver(self, chunks: list[str], reply_to: dict[str, str]) -> None:
        """Send the formatted chunks via the platform's API using the reply_to identifiers."""
```

`handle_webhook`'s body:

```python
def handle_webhook(self, headers, raw_body: bytes) -> str:
    self.verify(headers, raw_body)
    event = json.loads(raw_body)
    body = self.parse(event)
    attrs = [self._queue_handler.CustomAttribute(
        name="integration", value=self.integration_name, datatype=self._queue_handler.AttributeDataType.STRING
    )]
    attrs += [
        self._queue_handler.CustomAttribute(name=k, value=v, datatype=self._queue_handler.AttributeDataType.STRING)
        for k, v in self.reply_to(event).items()
    ]
    return enqueue_run_request(self._queue_handler, body, request_id=self.native_request_id(event), custom_message_attributes=attrs)
```

**Rule**: `InboundAdapter` assumes its injected `queue_handler` exposes `CustomAttribute` /
`AttributeDataType` nested types shaped like `SQSHandler`'s (`deployment/aws/core/sqs_handler.py:28-48`)
— today's only concrete `QueueHandler`. `QueueHandler.send_message_to_input_queue`'s
`custom_message_attributes: Optional[List[Any]]` (queue_handler.py:44) is already untyped for
exactly this reason. A future non-SQS `QueueHandler` implementation would need to match this
shape, or `InboundAdapter` gains a small per-backend override — not designed here since AWS is
the only backend this change targets (see Non-goals).

### `IntegrationAdapterFactory` (`integration/adapter/factory.py`)

House factory pattern, matching guardrail/sandbox/session-store precedent
(`core/util/factory.py`, `guardrail/guardrail.py:37,55`): built-in short names via `if/elif` +
real imports, dotted-path bring-your-own via `resolve_dotted`.

```python
class IntegrationAdapterFactory:
    @classmethod
    def get_inbound(cls, integration: str, queue_handler: type[QueueHandler]) -> InboundAdapter:
        """Resolve integration -> InboundAdapter, built-in if/elif else resolve_dotted(bring-your-own)."""

    @classmethod
    def get_outbound(cls, integration: str) -> OutboundAdapter:
        """Resolve integration -> OutboundAdapter, same shape as get_inbound (no queue_handler needed)."""
```

Built-in branches: `"slack"` → `integration.slack.adapter.SlackInboundAdapter` /
`SlackOutboundAdapter`, and likewise `"whatsapp"`, `"messenger"`, `"instagram"`, `"telegram"`,
`"teams"`. Anything else resolves via `resolve_dotted(integration, base=InboundAdapter)` /
`resolve_dotted(integration, base=OutboundAdapter)` — a bring-your-own integration passes its
adapter's dotted path as the `integration` value instead of a built-in short name.

`get_inbound` is called once per handler construction (see Consumer changes — each
`Agent<Platform>RequestHandler.__init__` resolves its own inbound adapter by its own fixed
`integration_name`, it does not need the factory's dynamic lookup at request time).
`get_outbound` is called by the Response Handler / `ECSOutputConsumer` per message, keyed by the
`integration` custom attribute the message carries — this is the dynamic lookup the registry
requirement is for, since the consumer process doesn't know ahead of time which integrations are
active in the deployment.

### Per-platform inbound adapter data (verified against current handlers)

| Platform | `session_id` (unchanged) | `native_request_id` source (**new** — none of these are read for this purpose today) | `reply_to` keys |
|---|---|---|---|
| Slack | `thread_ts` (slack_chat.py:71) | Events API envelope `event_id`, falling back to the inner event's `client_msg_id`, then uuid4 | `channel`, `thread_ts` |
| WhatsApp | `from_number` (whatsapp_chat.py:277) | `message["id"]` (whatsapp_chat.py:153, already read today for `reply_to_message_id` context — reused, not newly parsed) | `to_number` |
| Messenger | `sender_id` (messenger_chat.py:193) | `message["mid"]` (messenger_chat.py:148, already read, reused) | `recipient_id` |
| Instagram | `sender_id` (instagram_chat.py:211) | `message["mid"]` (instagram_chat.py:156, already read, reused) | `recipient_id` |
| Telegram | `str(chat_id)` (telegram_chat.py:183) | top-level `update["update_id"]` (not read today — Telegram's own per-update, per-bot monotonic id) | `chat_id` |
| Teams | `activity.conversation.id` (teams_chat.py:121) | `activity.id` (Bot Framework `Activity.id`, not read today) | `conversation_reference` (see Teams below) |

### Queue routing: `integration` + reply-to as flat custom SQS attributes

Matches design.md's Queue routing section exactly: `handle_webhook` attaches one `integration`
custom attribute plus the platform's `reply_to` dict, each as its own flat string-valued
`CustomAttribute` (not a JSON blob) — e.g. Slack's message carries `integration=slack`,
`channel=C0123`, `thread_ts=1699999999.000100`, alongside the existing `request_id`/`user_id`
attributes `send_message_to_input_queue` already adds (sqs_handler.py:290-292).

### Agent Runner: opaque, generic attribute forwarding

Today only `ServerlessAgentRunner._get_record_attributes`/`_send_to_output_queue`
(serverless/akagentrunner.py:39-41,82-86) special-case `endpoint_url`; `ECSAgentRunner`
(containerized/akagentrunner.py:45-70,72-82) forwards **nothing** beyond `request_id`/`user_id` —
there is no existing special-case to generalize on the ECS side, this is new. Replace the
`endpoint_url`-specific read/attach in both classes with a generic passthrough of every custom
attribute except the two handled separately:

```python
# ServerlessAgentRunner._get_record_attributes (and ECSAgentRunner's, same change)
@classmethod
def _get_record_attributes(cls, raw_queue_message: dict) -> dict:
    ...
    forwarded = {k: v for k, v in message_attributes.items() if k not in ("request_id", "user_id")}
    return {..., "forwarded_attributes": forwarded}   # e.g. {"endpoint_url": ...} or {"integration": ..., "channel": ..., "thread_ts": ...}

@classmethod
def _send_to_output_queue(cls, message_body: dict, record_attributes: dict) -> None:
    custom_attributes = [
        SQSHandler.CustomAttribute(name=k, value=v, datatype=SQSHandler.AttributeDataType.STRING)
        for k, v in record_attributes["forwarded_attributes"].items()
    ]
    SQSHandler.send_message_to_output_queue(..., custom_message_attributes=custom_attributes)
```

This removes `ServerlessAgentRunner`'s `AKConfig`/`ExecutionMode`-gated read of `endpoint_url`
(serverless/akagentrunner.py:39-41) — forwarding is now unconditional and attribute-name-agnostic,
so `endpoint_url` (WS clients), `integration`+reply-to (integrations), and any future
attribute all ride through the same path with no runner code change per addition. `ExecutionMode`
becomes unused in `ServerlessAgentRunner` after this change (still used elsewhere in the module by
`ServerlessStreamAgentRunner`, which is untouched — see Non-goals: no streaming for integrations,
so its mandatory-`endpoint_url` STREAM-mode contract at serverless/akagentrunner.py:183-188 stays
exactly as-is).

### Response Handler / `ECSOutputConsumer`: registry-dispatch generalization

Both classes gain one new branch, checked **before** the existing `ExecutionMode` branching (so
it applies regardless of what `execution.mode` the deployment happens to run — integrations are
queue-only/async by design, independent of the REST/WS execution mode):

```python
# ResponseHandler.process_message (serverless/akresponsehandler.py) — new branch
@classmethod
def process_message(cls, record):
    message_attributes = SQSHandler.get_message_custom_attributes(record)
    integration = message_attributes.get("integration")
    if integration:
        cls._dispatch_to_integration(record, integration, message_attributes)
        return
    if AKConfig.get().execution.mode == ExecutionMode.ASYNC:
        ...   # unchanged
    ...       # unchanged STREAM / else branches

@classmethod
def _dispatch_to_integration(cls, record, integration: str, message_attributes: dict) -> None:
    """Deliver via the integration's OutboundAdapter, then dual-write to the Response Store."""
    reply_to = {k: v for k, v in message_attributes.items() if k not in ("request_id", "user_id", "integration")}
    message = cls._construct_message_for_store(record)
    adapter = IntegrationAdapterFactory.get_outbound(integration)
    chunks = adapter.format(message["body"])
    adapter.deliver(chunks, reply_to)
    cls._get_response_store().add_message(message)
```

`ECSOutputConsumer.process_message` (containerized/akoutputconsumer.py:39-56) gets the identical
`integration`-attribute check ahead of its existing unconditional DynamoDB write — that existing
write becomes the `else` path, unchanged. **Note**: `ECSOutputConsumer._broadcast_via_websocket`
(akoutputconsumer.py:110-131) is pre-existing dead code — never called, and calls
`cls._get_websocket_handler()`, which doesn't exist on `ECSSQSConsumer`/`ECSOutputConsumer` and
would raise `AttributeError` if it ever were. This is a pre-existing gap unrelated to this
change (ECS has no working ASYNC/STREAM WebSocket dispatch today); this spec does not fix it —
only the new `integration` branch is added, matching what design.md scopes.

`on_permanent_failure` on both classes gains the mirrored branch: build the same
`{"error": ..., "request_id": ...}` payload as today, and — for the integration case only —
best-effort `adapter.deliver([error_text], reply_to)` wrapped in its own `try/except` (matching
the existing outer `try/except` in `on_permanent_failure` that already swallows failures so a
permanently-failed message doesn't get treated as a fresh retry), then write the error to the
Response Store as today. This mirrors the existing ASYNC/STREAM branches in
`ResponseHandler.on_permanent_failure` (akresponsehandler.py:136-166), which also attempt a
best-effort WebSocket notification before falling back to the store.

### Per-platform: `Agent<Platform>RequestHandler` becomes a thin route shell

For WhatsApp, Messenger, Instagram, Telegram (the four platforms with plain JSON webhook
handling, no framework SDK):

```
integration/<platform>/
├── <platform>_chat.py   # Agent<Platform>RequestHandler(RESTRequestHandler) — routes only
└── adapter.py            # <Platform>InboundAdapter(InboundAdapter), <Platform>OutboundAdapter(OutboundAdapter)  — new
```

`get_router()` keeps every existing route exactly as today (GET challenge handshake, POST
webhook path) — those are unrelated to the adapter split. The POST route body changes from
"parse → run agent → send reply inline" to:

```python
@router.post("/<platform>/webhook")
async def webhook(request: Request):
    if not self._verify(...):            # existing per-platform check, unchanged
        raise HTTPException(401, ...)
    raw_body = await request.body()
    try:
        self._inbound_adapter.handle_webhook(request.headers, raw_body)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"status": "ok"}
```

`Agent<Platform>RequestHandler.__init__` constructs its adapters once:
`self._inbound_adapter = <Platform>InboundAdapter(SQSHandler)` (AWS is the only queue backend
this change targets — see Non-goals). The signature-verification call (`_verify_signature` for
WhatsApp/Messenger/Instagram, the plain secret-token compare for Telegram) stays in the handler,
not the adapter's `verify()` — see the note below on why `verify()` is redundant for these four
and is implemented as a thin delegate rather than duplicated logic.

Actually: to avoid two copies of verification logic (one in the handler's route, one in
`<Platform>InboundAdapter.verify()`), the handler's route calls `self._inbound_adapter.verify(...)`
directly (not its own private method) — `<Platform>InboundAdapter.verify()` is where the HMAC /
secret-token check itself lives, moved from today's handler methods
(`_verify_signature` at whatsapp_chat.py:130-144, messenger_chat.py:124-138,
instagram_chat.py:132-146; the plain compare at telegram_chat.py:55-60), so there is exactly one
implementation per platform, in the adapter.

`<Platform>OutboundAdapter.format()` moves each platform's existing chunk splitter verbatim
(WhatsApp `max_length=4096`, Messenger `max_length=2000`, Instagram `max_length=1000`, Telegram
`max_length=4096` with `reply_markup` only on the last chunk); `.deliver()` moves the existing
`_send_message` HTTP call verbatim, now taking `reply_to` (`to_number` / `recipient_id` /
`chat_id`) as an explicit parameter instead of reading it off `self`/a closure.

**Acknowledgement-message behavioral change (all platforms with `agent_acknowledgement`
configured — Slack, WhatsApp, Messenger, Instagram, Teams; Telegram/Instagram config has no
such field per the table above so unaffected there)**: today Slack posts an acknowledgement
placeholder message and later edits that exact message in place with the final answer
(slack_chat.py:118-124 posts, :160-163 edits by ts). Splitting inbound (posts the ack) and
outbound (delivers the final answer) into two separate processes means the outbound side no
longer has the ack message's own `ts` to edit — carrying it would require a `reply_to`
attribute keyed to that one internal message id and is not requested by design.md. This spec
resolves it as: **the acknowledgement stays a synchronous send from the inbound adapter (or the
route, before enqueueing) exactly as today, and the outbound adapter always posts the final
answer as a new message** (threaded under `thread_ts` for Slack) rather than editing the ack
message in place. This is a deliberate, stated behavior change for Slack specifically (the only
platform whose current code edits the ack in place); the other platforms already post the ack and
the final answer as two separate messages today, so they are unaffected.

### Slack: Bolt fuses verify+dispatch — `handle_webhook` is not used

`AgentSlackRequestHandler` delegates its whole request to `slack_bolt`'s
`AsyncSlackRequestHandler.handle(req)` (slack_chat.py:51-55) — Bolt performs signature
verification and the URL-verification challenge internally, then invokes the registered
listener with the already-verified, already-parsed event. Bolt's `.handle(req)` already fuses
`verify()` + JSON-parse into one call, so `SlackInboundAdapter` does not implement
`handle_webhook()` as its entry point:

```python
# integration/slack/adapter.py
class SlackInboundAdapter(InboundAdapter):
    integration_name = "slack"

    def verify(self, headers, raw_body) -> None:
        """No-op: AsyncSlackRequestHandler.handle() already verified before the listener fires."""

    def parse(self, event: dict) -> BaseRunRequest: ...     # unchanged mapping from today's _handle_message
    def native_request_id(self, event: dict) -> Optional[str]: ...
    def reply_to(self, event: dict) -> dict[str, str]: ...  # {"channel": ..., "thread_ts": ...}
```

`AgentSlackRequestHandler` today registers `@slack_app.event("message")` → `handle_messages(message,
say)` → `self.handle(message, say)` (slack_chat.py:37-39,59), where Bolt injects `message` as only
the inner event payload (`user`/`text`/`channel`/`ts`/`thread_ts`/`files`) — no `event_id`, which
lives on the outer Events API envelope Bolt does not pass to this listener today. Getting
`native_request_id()`'s `event_id` requires adding `body` to the listener's injected args (Bolt
supports arbitrary named args — `body`, `message`, `say`, `client`, etc. — resolved by parameter
name), so the listener becomes:

```python
@slack_app.event("message")
async def handle_messages(body, message, say):    # `body` added: the full envelope, for event_id
    run_request = self._inbound_adapter.parse(message)
    request_id = self._inbound_adapter.native_request_id(body)
    reply_to = self._inbound_adapter.reply_to(message)
    ... build custom attributes, call enqueue_run_request(...) directly ...
```

`self.handle(...)`'s current signature/body (slack_chat.py:59) is replaced by this new listener —
the bot-message self-filter (`user == self._bot_id`, slack_chat.py:76-77) and the acknowledgement
send (Behavioral change 3) stay inline here, before the enqueue call.

i.e., the listener inlines what `InboundAdapter.handle_webhook()` does for the other four
platforms, minus the `verify()` call (Bolt already did it) — `parse`/`native_request_id`/`reply_to`
are still the adapter's methods, just invoked from Bolt's callback instead of from the shared
template method. This is the one platform where the adapter's four methods are called directly
rather than through `handle_webhook`; flagged here explicitly rather than silently varying
`InboundAdapter`'s contract.

### Teams: Bot Framework proactive messaging, not a bare `conversation_id`

Bot Framework's reply mechanism (`turn_context.send_activity(...)`, teams_chat.py:297) only works
within the synchronous `process_activity(...)` callback that constructed the `TurnContext` —
it cannot be reconstructed in a separate process (the Response Handler) from a bare
`conversation_id` string. Proactive messaging (replying outside the original turn) needs a
serialized `ConversationReference` — Bot Framework's `TurnContext.get_conversation_reference(activity)`
captures `service_url`, `channel_id`, `conversation.id`, and the bot/user identifiers needed by
`BotFrameworkAdapter.continue_conversation(reference, callback, app_id)`. This spec's `reply_to`
for Teams is therefore `{"conversation_reference": <json.dumps(get_conversation_reference(activity).__dict__ or equivalent)>}`
— one flat string attribute (well within the SQS attribute size limit, matching design.md's
Resolved Questions on reply-to size), not the bare `conversation_id` a first glance at the other
five platforms' pattern would suggest. `TeamsOutboundAdapter.deliver()` reconstructs the reference
and calls `self._adapter.continue_conversation(reference, callback, self._app_id)`, where
`callback` sends the formatted chunks via `turn_context.send_activity(...)`.

Like Slack, `AgentTeamsRequestHandler`'s route delegates to `self._adapter.process_activity(req_dict, auth_header, bot_logic)`
(Bot Framework validates auth internally, teams_chat.py:99-100) — `TeamsInboundAdapter.verify()`
is a no-op for the same reason as Slack's, and the `bot_logic` callback (today `_handle_teams_message`)
calls `parse`/`native_request_id`/`reply_to` directly and then `enqueue_run_request`, mirroring
Slack's shape.

### `_TeamsConfig`: new config section closing an existing gap

`teams_chat.py:40-44` already reads `Config.get().teams.agent`, `.agent_acknowledgement`,
`.app_id`, `.app_password`, `.tenant_id` — none of which exist on `AKConfig` today (confirmed:
no `_TeamsConfig` class and no `teams` field anywhere in `core/config.py`), so constructing
`AgentTeamsRequestHandler` today raises `AttributeError` before reaching its own `ValueError`
check at teams_chat.py:47-49. This spec adds the missing section (`core/config.py`, following the
`_TelegramConfig` idiom at :167-171):

```python
class _TeamsConfig(BaseModel):
    agent: str = Field(default="", description="Default agent to use for Teams interactions")
    agent_acknowledgement: str = Field(default="", description="The message to send as an acknowledgement when a Teams message is received")
    app_id: str = Field(default="", description="Microsoft Bot Framework application (client) ID")
    app_password: str = Field(default="", description="Microsoft Bot Framework application password/secret")
    tenant_id: str = Field(default="", description="Azure AD tenant ID (empty = multi-tenant)")
```

mounted as `teams: _TeamsConfig = Field(description="Microsoft Teams related configurations", default_factory=_TeamsConfig)`
in `AKConfig`, alongside the existing platform sections (`core/config.py:601-606`).

### Consumer changes

- **Slack** (`integration/slack/`): `slack_chat.py` keeps `get_router()`'s route and the Bolt
  `AsyncApp`/`AsyncSlackRequestHandler` setup; its listener is rewritten per the Slack subsection
  above. New `adapter.py`: `SlackInboundAdapter`, `SlackOutboundAdapter` (chunking moves from
  `_split_reply`, slack_chat.py:186-215, verbatim: 3000-char chunks, 5-chunk cap + truncation
  notice). The ack-message pattern changes per the Behavioral change above.
- **WhatsApp / Messenger / Instagram / Telegram** (`integration/<platform>/`): `<platform>_chat.py`
  keeps its GET-challenge route (WhatsApp/Messenger/Instagram) or its background-task dispatch
  (Telegram, `background_tasks.add_task`, telegram_chat.py:63-64 — unchanged, still defers
  processing off the request thread, now calling `handle_webhook` instead of `_process_webhook_body`).
  New `adapter.py` per platform holding the moved verify/parse/chunk/send logic.
- **Teams** (`integration/teams/`): `teams_chat.py` keeps its route and `BotFrameworkAdapter`
  construction; `_handle_teams_message`/`_send_reply`/`_process_attachments` logic moves into
  `TeamsInboundAdapter.parse()`/`TeamsOutboundAdapter.deliver()` per the Teams subsection above.
  New `_TeamsConfig` per above.
- **Gmail**: no change (Non-goal).
- **`ChatService`** (`core/chat_service.py:488`): `if req.session_id is None:` becomes
  `if not req.session_id:`, matching the async/stream paths' `if not session_id:`
  (chat_service.py:366,401,450) — see Behavioural changes.
- **`QueueRequestHandler`** (`deployment/common/queue_request_handler.py`): `get_router()`'s route
  calls the new `enqueue_run_request()` instead of inlining validation/minting/send — see Shared
  enqueue core. No change to its public routes or response shapes.
- **`ServerlessAgentRunner`** (`deployment/aws/serverless/akagentrunner.py`): `_get_record_attributes`/
  `_send_to_output_queue` generalized to opaque passthrough — see Agent Runner subsection.
  `ServerlessStreamAgentRunner` unchanged (Non-goal: no streaming for integrations).
- **`ECSAgentRunner`** (`deployment/aws/containerized/akagentrunner.py`): same generalization,
  newly added (today forwards nothing beyond request_id/user_id).
- **`ResponseHandler`** (`deployment/aws/serverless/akresponsehandler.py`) and
  **`ECSOutputConsumer`** (`deployment/aws/containerized/akoutputconsumer.py`): both gain the
  `integration`-attribute dispatch branch — see Response Handler subsection. Both classes' existing
  branches (ASYNC/STREAM WS broadcast, else store-write; unconditional DynamoDB write) are
  unchanged for non-integration messages.
- **`ak-py/pyproject.toml`**: no new optional-dependency groups — every platform's SDK dependency
  (`slack_bolt`, `httpx`, `python-telegram-bot`-equivalent, `botbuilder-core`, `msal`) is unchanged,
  since the adapter split moves code, not dependencies.

### Config changes

| Section | Change |
|---|---|
| `AKConfig.teams` | **New** — `_TeamsConfig` (agent, agent_acknowledgement, app_id, app_password, tenant_id), per above. Closes the pre-existing gap where `teams_chat.py` reads a config path that doesn't exist. |
| All other integration sections (`slack`, `whatsapp`, `messenger`, `instagram`, `telegram`, `gmail`) | **Unchanged** — field names, types, and defaults untouched; YAML files and `AK_<PLATFORM>__*` env vars are unaffected. |
| `execution.queues.*`, `execution.response_store.*` | **Unchanged** — the adapter path reuses `SQSHandler`/`ResponseDBHandler` exactly as configured today; no new knobs (design.md's "any new knobs go through AKConfig" bullet is satisfied by adding none). |
| Active-integrations selection | **No new config.** Per design.md's Configuration section, "a mechanism selects which integrations are active" — this is already satisfied by which `Agent<Platform>RequestHandler()` instances a deployment's `server.py` passes to `RESTAPI.run(handlers=[...])` / mounts under `ECSIOHandler`, exactly as today. The `IntegrationAdapterFactory` registry resolves *which adapter* handles a given message by its `integration` attribute; it does not need a separate "enabled integrations" list. |

### Behavioural changes

Numbered, each intentional:

1. **Integrations stop running the agent synchronously inside the webhook handler.** Every
   webhook now acks (HTTP 200) immediately after enqueueing; the reply arrives asynchronously via
   the outbound adapter. This is the core change design.md specifies (Migration section) — not a
   side effect.
2. **`request_id` prefers the platform's native id over a minted uuid4**, for all 6 platforms (see
   the per-platform table). Consequence: SQS FIFO dedup now collapses a platform's own webhook
   retries (e.g. WhatsApp redelivering the same `wamid...` message) into one enqueue instead of
   creating a duplicate `request_id` each time — this was not possible before since nothing read
   these ids.
3. **Slack's acknowledgement-then-edit-in-place UX changes to acknowledgement-then-new-message.**
   See the dedicated Behavioral change note above — the only platform whose current code edits a
   prior message.
4. **`ChatService`'s sync-path `session_id` check tightens from `is None` to falsy**, matching the
   async/stream paths. An explicit `session_id=""` on the generic REST route (not otherwise
   reachable through today's integrations, which all derive a non-empty bare key) now rejects with
   400 instead of proceeding with an empty session key.
5. **`ServerlessAgentRunner` forwards every custom message attribute opaquely** instead of only
   `endpoint_url`. Consequence: any attribute an `InboundAdapter` (or a future non-integration
   caller) attaches now survives the Agent Runner hop with zero runner code change — this is the
   generalization design.md's Queue routing section calls for, not a WS-path behavior change
   (`endpoint_url` still rides through, unconditionally now rather than mode-gated, but the
   `ServerlessStreamAgentRunner`'s STREAM path — the only consumer that currently reads
   `endpoint_url` — is untouched, so no observable change for existing WS clients).
6. **`ECSAgentRunner` gains attribute forwarding it never had.** No prior behavior regresses since
   nothing depended on the absence of forwarding; this only enables the new integration path on
   ECS.
7. **`ResponseHandler`/`ECSOutputConsumer` gain an `integration`-keyed dispatch branch evaluated
   before the existing `ExecutionMode` branch.** Non-integration messages (generic REST/WS
   traffic) are unaffected — they never carry an `integration` attribute, so they fall through to
   the existing unchanged branches exactly as today.
8. **Every integration reply is dual-written to the Response Store**, per design.md — a
   `GET /api/v1/chat/{session_id}`-style audit/poll fallback now exists for integration replies
   that never existed before (the old synchronous handlers only sent the platform reply, never
   touched the store). Cost: one extra store write per integration reply.

**Non-changes**: `BaseRunRequest`/`AgentRequestText`/`AgentRequestImage`/`AgentRequestFile` field
shapes; every platform's `session_id` derivation (still bare, no namespacing, per design.md's
Resolved Questions); the REST `/api/v1/chat` and `/api/v1/chat/{session_id}` routes' request/response
shapes; `SessionStoreBuilder`/`ThreadStoreBuilder`/`AttachmentStorageManager` and all non-integration
factories; Gmail.

## Error handling

- **`verify()` failure** (WhatsApp/Messenger/Instagram HMAC mismatch, Telegram secret-token
  mismatch): the webhook route raises `HTTPException(401, ...)` before any parse/enqueue attempt
  — unchanged from today's per-platform behavior, just relocated into the adapter.
- **`parse()`/`enqueue_run_request()` failure** (malformed payload; missing `session_id`/`prompt`
  after mapping): `ValueError` propagates to the route, which returns `HTTPException(400, ...)` —
  matching `QueueRequestHandler`'s existing translation of the same `ValueError` shape
  (queue_request_handler.py:70-73's checks, now inside `enqueue_run_request`).
- **Queue send failure** (`send_message_to_input_queue` raising, e.g. `ValueError` for an
  unconfigured queue URL, or a boto3 error): propagates as `HTTPException(500, ...)` from the
  route — same shape as `QueueRequestHandler`'s existing outer `except Exception` handling
  (queue_request_handler.py:123-125); the platform's webhook retry (all 6 platforms retry on a
  non-2xx response) naturally retries the whole verify→enqueue attempt, and since `request_id` is
  now derived deterministically from the platform-native id (item 2 above), a retried enqueue
  after a transient queue failure produces the *same* `request_id` — safe under FIFO dedup.
- **Outbound `deliver()` failure** (platform API error/timeout): propagates out of
  `_dispatch_to_integration`, so `process_message` raises — the message is not deleted from the
  Output Queue and is redelivered per the existing SQS retry/DLQ mechanics
  (`max_receive_count`, default 3, `execution.queues.output` — unchanged). On the final retry,
  `on_permanent_failure`'s integration branch best-effort delivers an error message to the
  platform (swallowing its own exceptions, matching the existing pattern) and always writes the
  error to the Response Store.
- **Missing/malformed `reply_to` data** (e.g. a platform payload without the fields `reply_to`
  needs): `reply_to()` raises `ValueError`/`KeyError` from inside `parse`/`handle_webhook`'s call
  chain, surfacing as the same 400 as a parse failure — the request is never enqueued rather than
  being enqueued undeliverable.
- **Unknown `integration` attribute at dispatch** (a message from an integration whose adapter was
  removed/misconfigured since it was enqueued): `IntegrationAdapterFactory.get_outbound` raises
  `AKConfigError` (unknown short name and not a resolvable dotted path) — propagates the same as
  any other `process_message` exception (retry, then permanent-failure store write with no
  platform delivery attempt, since there is no adapter to deliver through).
- **Missing `_TeamsConfig` fields**: `AgentTeamsRequestHandler.__init__`'s existing
  `if not self._app_id or not self._app_password: raise ValueError(...)` (teams_chat.py:47-49) is
  unchanged and now actually reachable (today it's unreachable — construction already raises
  `AttributeError` first).

## Testing

New test files (`ak-py/tests/`, no existing platform test files exist for any of the 6 — see
evidence below):

- `test_integration_adapter_base.py`: `InboundAdapter.handle_webhook` template method — calls
  `verify`→`parse`→enqueues with `integration` + `reply_to` custom attributes via a
  `DummyInboundAdapter`/fake `QueueHandler`; asserts `request_id` prefers `native_request_id()`
  over a minted uuid4, and falls back to uuid4 when `native_request_id()` returns `None`.
- `test_integration_adapter_factory.py`: `IntegrationAdapterFactory.get_inbound`/`get_outbound` —
  all 6 built-in short names resolve; an unknown short name that is also not a valid dotted path
  raises `AKConfigError`; a dotted-path bring-your-own subclass of `InboundAdapter`/`OutboundAdapter`
  resolves (mirrors `test_factory.py`'s and `test_store_builders.py`'s existing BYO-resolution
  pattern).
- `test_slack_adapter.py` / `test_whatsapp_adapter.py` / `test_messenger_adapter.py` /
  `test_instagram_adapter.py` / `test_telegram_adapter.py` / `test_teams_adapter.py`: per platform,
  with a mocked HTTP client for `deliver()` —
  - `parse()` maps a representative platform payload to the expected `BaseRunRequest` fields.
  - `native_request_id()` returns the platform-native id from the table above.
  - `reply_to()` returns the exact keys from the table above.
  - `verify()` rejects a bad signature/secret (WhatsApp/Messenger/Instagram/Telegram); is a no-op
    for Slack/Teams (asserting it does *not* raise, documenting the delegated-verification
    exception).
  - `OutboundAdapter.format()` chunks at the platform's existing size (WhatsApp 4096, Messenger
    2000, Instagram 1000, Telegram 4096 with `reply_markup` only on the last chunk, Slack 3000
    with the 5-chunk truncation notice).
  - Teams-specific: `reply_to()` returns a `conversation_reference` whose JSON round-trips through
    `BotFrameworkAdapter.continue_conversation`'s expected shape (mocked adapter).
- `test_akagentrunner_stream.py` (existing): unaffected — `ServerlessStreamAgentRunner` is
  untouched (Non-goal).
- **New**: `ServerlessAgentRunner._get_record_attributes`/`_send_to_output_queue` and
  `ECSAgentRunner`'s equivalents currently have **zero direct unit tests** (confirmed — no
  existing test file covers either). Add cases asserting: an attribute other than `endpoint_url`
  (e.g. `integration`, `channel`) forwards opaquely; `request_id`/`user_id` are never
  double-forwarded as generic attributes (they stay in their own dedicated fields).
- `test_akresponsehandler.py` (existing, patch targets unchanged —
  `patch.object(ResponseHandler, "_get_base_ws_handler", ...)`,
  `@patch("agentkernel.deployment.aws.serverless.akresponsehandler.AKConfig")`): add cases for the
  new `integration`-attribute branch — dispatches to `IntegrationAdapterFactory.get_outbound`
  (mocked) and always calls `_get_response_store().add_message(...)` after a successful `deliver()`;
  a `deliver()` exception propagates without a store write (message retries); `on_permanent_failure`'s
  integration branch attempts `deliver()` once, swallows its exception, and always writes the
  error to the store.
- New test for `ECSOutputConsumer.process_message`'s integration branch (the existing
  `test_ecs_sqs_consumer_parallel.py` uses local `_SyncConsumer`/`_AsyncConsumer` test doubles, not
  `ECSOutputConsumer` itself, so this is new coverage, not a patch-target update) — same
  assertions as the `ResponseHandler` case above.
- `test_chat_service*.py` (existing): add a case asserting `ChatService`'s sync path now rejects
  `session_id=""` with the same error the async/stream paths already produce (behavioral change 4).
- `test_config.py` (existing): add `_TeamsConfig` field defaults/env-var override assertions,
  matching the existing per-platform config test pattern.
- `deployment/common/queue_request_handler.py`'s existing route tests (if any — none of the
  research surfaced a dedicated `test_queue_request_handler.py`; if absent, add one): assert
  `enqueue_run_request` is what the route calls, and that a supplied `request_id` (simulating an
  adapter's platform-native id) is honored rather than overwritten by a fresh uuid4.

Run: `cd ak-py && uv run pytest`.
