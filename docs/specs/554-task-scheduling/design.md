# #554: Agent Kernel Scheduled Tasks

Add time-triggered agent invocation to Agent Kernel.

- Store a **scheduled task** in a scheduled-task table and register it with an external **timer**
  service.
- When the timer fires, it enqueues an **ordinary agent message** onto the existing **input queue**.
- The existing agent runner consumes and executes it exactly as it would any other queued request.
  There is no scheduled-run code path in the runner.
- Availability (this version):
  - **AWS only**; Azure and GCP are not covered.
  - **Queue-mode scalable AWS deployments only** (scalable serverless Lambda and scalable containerized ECS).

## Terminology

Fixed vocabulary, used consistently throughout this document and in the implementation. The bare
word "task" is not used for this feature — it is too generic and collides with unrelated concepts
(ECS tasks, framework tasks).

| Term | Meaning |
| --- | --- |
| **Scheduled task** | The stored definition: an agent message plus the schedule that fires it. Identified by `scheduled_task_id`. |
| **Schedule** | The timing expression alone (cron / rate / one-time `at`) plus the conversation mode. A field of a scheduled task, and the name of the management endpoint. |
| **Fire** | One delivery of a scheduled task's message onto the input queue by the timer. |
| **Run** | The agent execution that results from a fire. |

## Problem Description

- Agent Kernel cannot invoke an agent on a schedule: every existing entry point — REST
  (`/api/v1/chat`), queue-based ingestion, A2A, MCP — requires an external caller to initiate the
  request. Nothing in the system can trigger agent work on its own.
- Some agent work is triggered by time, not by a user or an upstream system: a recurring report, a
  periodic cleanup, a scheduled check.
- That work needs to run on a cron-like schedule and deliver a prompt to the appropriate agent with
  no human or calling system in the loop.

## Design Choices

Six decisions shape the rest of this document:

1. **AWS only.**
   - The only implementation delivered: a pluggable scheduled-task table (DynamoDB, Redis or Valkey,
     following the session store type — see *The `ScheduledTaskStore` abstraction*), EventBridge
     Scheduler for the timer, SQS for delivery.
   - Azure and GCP do not get scheduling in this version; the `Scheduler` ABC exists so those can be
     added later, not because a second provider is being written now.
2. **Queue mode only.**
   - Within AWS, scheduling is delivered exclusively for scalable deployments running with
     `queue_mode = true`: scalable serverless (Lambda) and scalable containerized (ECS).
   - Non-queue deployments (single-Lambda serverless, plain REST container, local runs) do not get
     scheduling in this version.
3. **Timing is delegated to an external timer, not to an in-process poller.**
   - A `Scheduler` ABC owns the scheduled-task table and the registration of schedules with a
     platform timer.
   - On AWS the timer is **EventBridge Scheduler**.
4. **The timer's target is the input queue, not the agent.**
   - When a schedule fires, the timer delivers the pre-built message to the input queue (SQS).
   - From that point the request is indistinguishable from any other queued request and flows
     through the existing agent runner, guardrails, hooks, tracing, retries and DLQ.
   - After execution, the scheduled-task row is updated to record that the run completed.
5. **A fire is an ordinary agent message — no scheduled-specific request or response models.**
   - Agents are triggered by messages. A scheduled fire is the same message input as any other, with
     a few additional fields.
   - The existing agent request and response models gain **one optional `scheduled_run` block**
     carrying the run's correlation metadata. When it is absent, nothing changes anywhere.
   - The agent runner does nothing scheduling-specific: it validates the same request model, runs
     the agent on the normal path, and the response echoes `scheduled_run` back verbatim so output
     queue consumers can tell a scheduled run from an ordinary one.
6. **Creation reuses the existing chat endpoint; only management gets new routes.**
   - There is **no new creation endpoint**. `POST /api/v1/chat` accepts an optional `schedule` block
     in the body; when present, the message is registered to run later instead of being run now.
   - The new `/api/v1/schedule` routes exist only to **query and manage already-created** scheduled
     tasks.

### Flow

```
POST /api/v1/chat  { prompt, agent, schedule: {...} }
  → ScheduledTaskService.create()
      → Scheduler.upsert()
          → write the scheduled-task row to the ScheduledTaskStore
          → register the schedule with the timer: recurring (cron/rate) or one-time (at),
            carrying an ordinary agent message body + its `scheduled_run` block
  → acknowledgement returned on the caller's own channel (see *Creation acknowledgement*).
    Nothing is enqueued for execution at this point.

timer fires
  → timer puts an ordinary agent message on the input queue
    (MessageGroupId = scheduled_task_id)
  → agent runner consumes → runs the agent (the normal path, unchanged)
  → runner publishes the ordinary agent response; `scheduled_run` is echoed through verbatim
  → output consumer sees `scheduled_run` on the response
      → Scheduler.mark_run_completed() → ScheduledTaskStore
          last_run_at, last_run_status = COMPLETED / FAILED
```

### Why this shape

- **No leader election, no distributed lock, no dedup window.** No replica polls for due work, so
  there is nothing for replicas to race over. The timer fires once per scheduled time, and delivery
  is deduplicated per `(scheduled_task_id, scheduled_time)` as a cheap safety net against timer-side
  at-least-once delivery.
- **No new execution path and no new message models.** The scheduled run reuses the input queue and
  the ordinary request/response models, so it inherits the retry handling, DLQ, concurrency
  controls, guardrails, hooks and observability that already apply to queued requests. Nothing is
  bypassed and nothing is special-cased in the runner.
- **The scheduler process does not need to be running when a scheduled task is due.** The timer is
  infrastructure; a scaled-to-zero Lambda deployment still fires.

## Scope

- Define scheduled tasks — recurring (cron or rate) or one-time (at a given instant).
- Have those scheduled tasks automatically place a prompt for an AI agent on the input queue when
  due.
- Create scheduled tasks through the existing chat endpoint, and query/manage existing ones through
  the `/api/v1/schedule` routes and through agent-callable tools. Every scheduled task originates
  from an authenticated caller.
- Enqueue a fire exactly once per scheduled time, including when the deployment is running many
  replicas. What happens after enqueue is Agent Kernel's existing delivery guarantee, not the
  scheduler's concern (see *Reliability and duplication prevention*).
- Record each run's outcome on the scheduled-task row.

## Out of Scope

- Sending notifications or alerts when a run completes — output is limited to whatever the agent
  itself produces.
- Non-AWS clouds. No Azure or GCP scheduler implementation is written in this version; the ABC keeps
  the door open, nothing more.
- Non-queue AWS deployments (single-Lambda serverless, plain REST container, local development runs).
- **Retry policy, DLQ handling and reprocessing.** These are existing Agent Kernel mechanisms,
  unchanged by this feature and outside the scheduler's scope (see *Reliability and duplication
  prevention*).
- An Agent Kernel-owned catch-up mechanism for missed runs. Recovery of delayed or missed fires is
  the timer's responsibility (see *Missed fires* below).
- Rotating continuous sessions (a long-running conversation that rolls over on a fixed period).
  Dropped because it is the one session mode that cannot be resolved at registration time and would
  force scheduling-specific logic into the agent runner — see *Conversation / session handling* and
  *Open Questions*.
- Attachments in a scheduled message. A `schedule` block is accepted on `POST /api/v1/chat` only,
  not on `/api/v1/chat-multipart`.
- Long-term storage of scheduled run results.

## Key Requirements

### Deployment applicability

- Scheduling is enabled only on AWS, and only when the deployment runs in queue mode. Supported
  targets — the complete list for this version:
  - AWS scalable serverless — request handler / agent runner / response handler Lambdas over SQS.
    Shipped example: `examples/aws-serverless/scheduled-openai`.
  - AWS scalable containerized — REST service + agent runner ECS tasks over SQS. Shipped example:
    `examples/aws-containerized/openai-scheduled-task`.
- Scheduling is switched on by a new `scheduler` config block (`scheduler.enabled: true`). The block
  carries **no scheduled-task definitions** — tasks are only ever created at runtime by an
  authenticated caller — but it does carry the deployment values the capability cannot derive:
  - `agents` — which agents the agent-callable tool set attaches to (omitted = all, `[]` = none).
  - `group_name` / `target_role_arn` — the timer's schedule group and execution role, which are
    Terraform outputs, injected as `AK_SCHEDULER__GROUP_NAME` / `AK_SCHEDULER__TARGET_ROLE_ARN`.
  - `region`, and exactly one per-backend location block — `dynamodb.table_name`, `redis.prefix`
    or `valkey.prefix`. Which one is read is *not* configured: it follows the session store type
    (see *The `ScheduledTaskStore` abstraction*).
- Configuration validation runs during **component initialization**: process startup on
  long-running services (ECS), cold start on serverless — in both cases before the first request
  or scheduled execution is processed. "At initialization" throughout this document means this.
  - The two output consumers validate at the start of `handle()` / `run()` rather than at import,
    because `agentkernel.aws` re-exports every deployment class and importing it for an unrelated
    entry point must not assert on wiring only the scheduler-enabled components are given. This is
    still before any outcome can be recorded.
- The enablement check raises `AKConfigError` when, with `scheduler.enabled` true:
  - `session.type` is not one of `dynamodb`, `redis`, `valkey` — scheduling needs a durable store
    shared by all replicas.
  - `group_name` or `target_role_arn` is missing or blank. An empty string counts as unset: the
    examples declare them as `""` placeholders for Terraform to fill, so a deployment missing the
    wiring fails here rather than at the first registration.
  - The location block matching `session.type` is missing or blank, **or** a block for a different
    backend is populated — a configured-but-never-read table is worse ignored than rejected.
- **Queue mode and FIFO are preconditions, but they are enforced in Terraform, not in the
  application.** `scheduled_task = true` carries a `validation` block requiring `queue_mode = true`;
  the containerized queue module hardcodes `fifo_queue = true` and serverless defaults it to `true`.
  The application deliberately does **not** re-check the queue URLs: each serverless Lambda is given
  only the queue URL it publishes to, so an unset URL is not evidence of a misconfiguration and
  rejecting it would break the supported deployment. The gate therefore lives where it can be
  evaluated correctly — at deploy time.
- When `scheduler.enabled` is false, a `schedule` block on a chat request is rejected with 400 and
  the `/api/v1/schedule` routes are not mounted.

### The `Scheduler` abstraction

- A `Scheduler` ABC defines the provider-agnostic contract, and is implemented in code as a real
  abstraction (not a thin wrapper) so a non-AWS provider can be added later without touching the API
  or config layers. Only the AWS implementation is built and shipped in this version:
  - `minimum_granularity` — the finest interval this provider's timer supports, exposed as a
    property so callers above the ABC can reject a too-fine schedule without knowing which
    provider is in use.
  - `upsert(scheduled_task)` — persist the scheduled-task row and register (or re-register) its
    schedule with the timer, carrying the message body the timer should deliver. The row is written
    first and the registration follows; **a registration failure rolls the row back** to its prior
    state (or removes it outright when the id was new, so the create can be retried), because a row
    without a registration would silently never fire.
  - `delete(scheduled_task_id)` — remove the timer registration and soft-delete the row (see
    *Deletion lifecycle*). Idempotent.
  - `get(scheduled_task_id, include_deleted=False)` / `list(owner_id, limit, cursor)` — read
    scheduled-task definitions and their last-run state. Reads are filtered here, not in the store:
    `get` hides a soft-deleted row unless it is explicitly asked for, and `list` is **paginated**,
    returning a page plus an opaque continuation cursor that callers cannot construct or interpret.
  - `mark_run_completed(scheduled_task_id, scheduled_task_version, scheduled_time, status, last_error=None)`
    — record run outcome (status plus optional error detail) on the row. Returns whether the write
    was applied, so a guard rejection is an ordinary `False`, not an exception.
    There is deliberately no `mark_run_started`: only terminal outcomes are recorded, so a stuck
    run is visible from queue metrics instead of requiring a started-state write on every fire.
- **The `Scheduler` is the only component that touches the store.** The `ScheduledTaskStore` is an
  implementation detail held by the `Scheduler`; no caller — not the `ScheduledTaskService`, not the
  output consumer — resolves or calls the store directly. Every row read and write goes through a
  `Scheduler` method.
- Beyond the method contract, any implementation must guarantee:
  - **At-most-once delivery per `(scheduled_task_id, scheduled_time)`** — a duplicate timer-side
    fire is suppressed before it reaches the agent runner.
  - **Fires of the same scheduled task are serialized** — a fire does not overtake or interleave
    with an earlier fire of the same scheduled task.
  - **One-time registrations remove themselves after firing** — no separate cleanup process.
  - **Schedules finer than the provider's minimum granularity are rejected at registration**, not
    silently rounded.
  - **The delivered payload is a valid ordinary agent message** — a fully resolved `session_id` and
    a `scheduled_run` block, so the agent runner needs no scheduling awareness to consume it.
  - **Outcome writes are guarded against stale and mismatched runs** — `mark_run_completed` is a
    no-op when the row is absent, soft-deleted, belongs to a different incarnation of the id, or
    reports a `scheduled_time` older than the one already recorded (see *Outcome-write guards*).
- The ABC + factory follow the existing pluggable-backend pattern (`Trace`, `ResponseStore`,
  `QueueHandler`), so a non-AWS timer can be added later without touching the API or config layer.
  How AWS satisfies each obligation is in *AWS Feasibility* below; none of it leaks above this
  contract.

### The `ScheduledTaskStore` abstraction

- The table itself is a separate, pluggable concern from the timer, mirroring the existing
  `ResponseStore` ABC (`deployment/common/response_store.py`) and its backends
  (`deployment/aws/core/response_store/{dynamodb.py,redis.py,valkey.py}`): a `ScheduledTaskStore`
  ABC with DynamoDB, Redis and Valkey implementations.
- It is a **private collaborator of the `Scheduler`**, not a public seam: the `Scheduler` holds it
  and is the only caller. Pluggability here is about supporting three storage backends behind one
  timer implementation, not about giving other components a way in (see *The `Scheduler`
  abstraction*).
- The backend is not configured separately: it uses the **same backend type as the session store**
  (`session.type`). DynamoDB sessions → a dedicated DynamoDB table; Redis or Valkey sessions → the
  **same cluster** the sessions use, with a separate table/keyspace — no new infrastructure is
  provisioned. Any other session type (e.g. in-memory) fails the enablement check at initialization,
  since scheduling needs a durable store shared by all replicas.
- The table is always a **new table/keyspace** dedicated to scheduled tasks — it is never a
  partition of an existing session or response-store table, even when the underlying cluster is
  shared.
- The row carries: the schedule, the agent message template that the timer delivers, the owner
  identity, `scheduled_task_version` (the incarnation token — see *Outcome-write guards*), `status`
  (`ACTIVE` | `COMPLETED`), `created_at` / `updated_at`, `last_run_at` / `last_run_status` /
  `last_error` / `last_run_scheduled_time`, `completed_at` (one-time), and the soft-delete fields
  `deleted` / `deleted_at` / an expiry timestamp (see *Deletion lifecycle*).
- Beyond whole-row reads and writes the store must offer two operations the guards depend on:
  - **A partial-field update, conditional on `scheduled_task_version`.** An outcome write must not
    rewrite the definition from a possibly-stale row, and folding the incarnation check into the
    write closes the check-then-act window left by reading the row first (see *Outcome-write
    guards*). A management `PUT` and an outcome write can therefore interleave without clobbering
    each other's fields.
  - **A physical remove that leaves no tombstone**, used only to roll back a creation whose timer
    registration failed — a tombstone there would block retrying the create at the same id.
- **Listing an owner's tasks needs an owner index, and tombstones must fall out of it.** On DynamoDB
  this is a sparse GSI keyed on an index attribute that mirrors `owner_id` while the row is live and
  is removed on soft-delete, so a page is never short because tombstones were filtered out after the
  read — while the row itself stays readable by primary key throughout the grace window, which the
  outcome-write guards need.

### Schedule definition and timing

- A schedule is one of: a cron expression, a fixed rate, or a one-time instant — **exactly one**;
  naming none or more than one is rejected. It also carries a `timezone` (default `UTC`), which the
  timer evaluates the **wall-clock** expressions in — cron and rate. A one-time `at` names an
  absolute instant and is registered in UTC, so `timezone` does not shift it.
- Minimum granularity is whatever the timer provider supports; schedules finer than that are
  rejected at create/update time (a `Scheduler` obligation — see above). Concretely, validation is
  provider-agnostic and rejects: a rate that is not `<n> <second|minute|hour|day>` or is finer than
  the granularity, a cron without its six fields (a seventh leading field would be seconds), a
  one-time instant that is not in the future, and an expression still wrapped in its provider-native
  `cron(...)` / `rate(...)` / `at(...)` form — the spec carries the bare expression and the provider
  adds its own wrapper, so a pasted-in native form would otherwise be doubly wrapped and fail later
  at the timer as an opaque server error.
- One-time schedules: the timer registration removes itself after firing (a `Scheduler`
  obligation), but the row is **kept** — `mark_run_completed` records the run's outcome and sets
  `status = COMPLETED` / `completed_at`. `GET` keeps answering "did this run, when, and did it
  succeed" after the fire, and there is no orphan-row problem: the row is removed only by an
  explicit delete (see *Deletion lifecycle*). Recurring rows are likewise never deleted except by
  an explicit delete.

### Identity of a scheduled task

- `scheduled_task_id` is the identity and the idempotency key of a scheduled task.
- It may be **supplied by the caller** — a client through the chat endpoint, or an **agent** through
  the agent-callable tool — as `schedule.id` in the creation body. A caller-supplied id makes
  creation idempotent: re-creating with the same id replaces the definition rather than producing a
  duplicate.
- When it is **not** supplied, the server generates one as `schedule_<uuid4>` and returns it in the
  acknowledgement.
- Caller-supplied ids are reusable after a deleted row's TTL expires, which is why outcome writes
  carry an incarnation token (see *Outcome-write guards*).

### Deletion lifecycle

- Deleting a scheduled task — always via `DELETE /api/v1/schedule/{scheduled_task_id}` or an
  agent-callable tool — is a **soft delete**, not an immediate physical removal: the timer
  registration is removed first, then the row is marked `deleted = true` with `deleted_at` and a
  TTL; the store expires the row afterwards (DynamoDB TTL; key expiry on Redis/Valkey).
- **The TTL is derived, not a fixed constant.** It sizes the window during which the id stays
  reserved, and should outlive the longest execution an already-enqueued fire can have — so an
  in-flight run's id cannot be claimed by a new scheduled task while that run is still going. It is
  computed as `input queue visibility timeout × receive count` plus a safety margin, floored at a
  documented minimum. The receive count is the **larger** of the queue's own
  `RedrivePolicy.maxReceiveCount` and `execution.queues.input.max_receive_count`
  (`core/config.py:324`, default 3), which keeps the TTL an upper bound either way — the default
  deployment has no DLQ and therefore no redrive policy at all.
  - Correctness does not depend on getting this number right — the `scheduled_task_version` guard
    rejects a cross-incarnation outcome however short the TTL turns out to be (see *Outcome-write
    guards*). The derivation exists to make the common case unsurprising, not to carry the
    guarantee.
  - The visibility timeout is an SQS queue attribute set by Terraform
    (`queue_config.input_queue_visibility_timeout`), not an `AKConfig` field, so the AWS
    implementation reads it via `GetQueueAttributes`. If the call fails it raises rather than
    falling back to a guessed TTL.
  - **The derivation is deferred to first use, not done at initialization**, and cached for the
    life of the process. Only the delete path needs the TTL, so a component that merely records run
    outcomes constructs a `Scheduler` without ever reading the input queue — which is why the
    output consumers are given neither the input queue URL nor permission on it (see *AWS
    Feasibility*).
- **A deleted scheduled task is terminal.** It cannot be restored or transitioned back to an active
  state, and while the deleted row still exists, creating or updating at the same
  `scheduled_task_id` is rejected — after TTL expiry the id can be reused, which creates a **new
  incarnation** with a fresh `scheduled_task_version` (see *Outcome-write guards*).
- The soft delete closes the delete/fire race without locking or immediate physical deletion:
  removing the registration stops future fires, and a fire already on the queue when the delete
  lands still executes, but `mark_run_completed` sees `deleted = true` and discards the outcome —
  a queued execution can never recreate or mutate state for a deleted scheduled task.

### Outcome-write guards

`scheduled_task_id` is caller-choosable and reusable after TTL expiry, and a fire's outcome can
arrive arbitrarily late, so `mark_run_completed` cannot assume the row it finds belongs to the run
reporting. It applies four guards, in order, and is a **silent no-op** (logged at warning, message
still acknowledged — never retried, never dead-lettered) when any of them rejects the write:

- **Row absent** — the scheduled task was deleted and its TTL has since expired. Nothing to record.
- **Row soft-deleted** (`deleted = true`) — it was deleted while this run was in flight.
- **Incarnation mismatch** — the row's `scheduled_task_version` differs from the one carried in the
  message. `scheduled_task_version` is a server-generated token (a UUID or creation timestamp)
  stamped onto the row at creation and into the `scheduled_run` block at registration, so an outcome
  from a deleted-and-recreated scheduled task can never be written onto its successor's row. This
  guard is what makes caller-chosen, reusable ids safe; existence checking alone is not sufficient.
- **Stale scheduled time** — the reported `scheduled_time` is older than the row's
  `last_run_scheduled_time`. Defence in depth behind FIFO ordering (see *Reliability and
  duplication prevention*), so a redelivered older outcome cannot overwrite a newer one.

The guards are evaluated against a row that was read a moment earlier, so the write that follows
carries the incarnation check with it: the outcome is applied as a **partial-field update
conditional on `scheduled_task_version`**, not a whole-row write. This closes the check-then-act
window between the read and the write, and keeps an outcome write from rewriting a definition a
concurrent management `PUT` has since changed. A failed condition is the same silent no-op as any
other guard rejection.

### Message model

There is **no `ScheduledTaskRequest` and no `ScheduledTaskResponse`**. A fire travels as the ordinary
agent message, with one optional block added to the existing models.

- **`ScheduledRunMetadata`** — a small model with `scheduled_task_id`, `scheduled_task_version`,
  `scheduled_time` and `run_id` (the latter two stamped by the timer at fire time).
- **On the request model** (`BaseRunRequest`, `core/model.py:309`) — two new optional fields, both
  defaulting to `None`, so every existing caller is unaffected: `schedule: ScheduleSpec | None`
  (create-time only — registers the message to run later) and
  `scheduled_run: ScheduledRunMetadata | None` (fire-time only — correlates one run). They never
  appear on the same request.
- **On the response** — the shared response builder (`ResponseBuilder.build_response`,
  `core/chat_service.py:311`) echoes the request's `scheduled_run` block into the response body
  verbatim when it is present. This is a generic pass-through of an optional block, not scheduling
  logic, and it is the only change on the response side.
- **The agent runner is untouched.** It validates the same `BaseRunRequest`, calls
  `ChatService.process_chat_request` on the normal path, and publishes the resulting response body.
  It does not read, branch on, or construct `scheduled_run`.
  - The same applies to the permanent-failure path: both runners already detect an exhausted
    message **at consume time** — `ApproximateReceiveCount > max_receive_count` — and publish an
    error body to the output queue instead of processing it
    (`deployment/aws/serverless/core/sqs_consumer.py:45-51`,
    `deployment/aws/containerized/core/sqs_consumer.py:110-115`;
    handlers at `deployment/aws/containerized/akagentrunner.py:122-133`,
    `deployment/aws/serverless/akagentrunner.py:153-181,305-324`). That body echoes `scheduled_run`
    from the failed request for the same reason — pass-through, not special-casing — which is what
    makes a retry-exhausted run recordable as `FAILED` without any DLQ involvement. Extracting the
    block there is best-effort by construction: this path has no error channel left, so an
    unparseable or malformed body reads as "not a scheduled run" rather than raising.
  - One consequence the runners do have to account for: a fire's `MessageGroupId` is the
    `scheduled_task_id`, not a session id, so the error body's `session_id` cannot be taken from
    the group id as it is for ordinary traffic — it is read from the failed message's body instead.
- **The output consumer is the only component that reads `scheduled_run`.** Its presence is exactly
  how a consumer tells a scheduled run from an ordinary one. The outcome status is derived from the
  ordinary response shape — an `error` key means `FAILED`, otherwise `COMPLETED` — so no
  scheduling-specific status or error fields are introduced.
- The registered payload carries a **fully resolved `session_id`**, not a session policy, so the
  runner has nothing to derive (see *Conversation / session handling*).
- Delivery attributes on the **input** queue: fires are grouped and serialized by
  `scheduled_task_id` — stable across fires regardless of conversation mode — and deduplicated by
  `<scheduled_task_id>:<scheduled_time>`. The fire also carries the two message attributes any
  queued request carries: `request_id` (the timer's execution id, unique per fire — both runners
  raise without one) and `user_id` (the scheduled task's owner).
- Delivery attributes on the **output** queue: the same `MessageGroupId = scheduled_task_id`, so a
  scheduled task's outcomes are processed in order and a stale outcome cannot overwrite a newer one.
  This requires no new mechanism — the agent runner already propagates the incoming message's group
  and dedup ids verbatim when publishing to the output queue
  (`deployment/aws/containerized/akagentrunner.py:85-90`,
  `deployment/aws/serverless/akagentrunner.py:101-106`), so grouping the input fire by
  `scheduled_task_id` automatically groups its outcome by `scheduled_task_id` too. The output queue
  is FIFO on both supported targets (`containerized/modules/queues/main.tf:53`; serverless shares
  the `fifo_queue` variable across both queues, `serverless/modules/queues/main.tf:15,60`).
  - Consequence of the same propagation: the outcome inherits dedup id
    `<scheduled_task_id>:<scheduled_time>`, so if an at-least-once redelivery re-executes a run
    within the 5-minute FIFO dedup window, only the first outcome is published. This is harmless —
    the guards make repeat outcome writes idempotent anyway — but it is deliberate, not accidental.

### Reliability and duplication prevention

- **The scheduler's responsibility ends at enqueue.** Exactly one **enqueue** per scheduled time,
  with any number of replicas running, guaranteed by the timer firing once and backed by the
  `Scheduler`'s at-most-once delivery obligation.
- **Once a message is on the queue, Agent Kernel's existing fault-tolerance guarantee applies: it
  will be delivered.** Retry policy, visibility-timeout redelivery, DLQ and reprocessing are
  existing Agent Kernel mechanisms. This feature neither changes nor extends them, and they are not
  part of the scheduler's scope.
  - Practical consequence unchanged from any queued request today: delivery is at-least-once, so a
    consumer crash after the agent's side effects but before message deletion re-executes the run.
    Agents whose actions must not repeat need to be idempotent.
- No leader server and no distributed lock is required — by construction, not by configuration.
- **Outcome ordering per scheduled task is guaranteed, not assumed.** Outcomes travel the output
  queue under `MessageGroupId = scheduled_task_id` on a FIFO queue (see *Message model*), so
  outcomes are consumed in publish order and a stale outcome cannot overwrite a newer one. The
  stale-time guard in `mark_run_completed` (see *Outcome-write guards*) is defence in depth behind
  that ordering, not the primary mechanism.
- **A retry-exhausted run is recorded as `FAILED` — no DLQ reconciliation is needed.** The DLQ is
  not how Agent Kernel notices an exhausted message: both runners check
  `ApproximateReceiveCount > max_receive_count` **before** processing a record and, when it is
  exceeded, publish an error body to the output queue instead of running the agent
  (`deployment/aws/serverless/core/sqs_consumer.py:45-51`,
  `deployment/aws/containerized/core/sqs_consumer.py:110-115`). That error body echoes
  `scheduled_run`, so it reaches the output consumer like any other outcome and is written to the
  row as `FAILED` with the retry message as `last_error`.
  - The DLQ therefore stays what it already is — a backstop for messages that fail outside this
    check — and this feature adds no DLQ processing (see *Out of Scope*).
- **The output consumer's own permanent-failure path makes one last attempt to record the
  outcome.** The consumers' retry limit sits one below the output queue's, so when they give up the
  message is deleted rather than dead-lettered — an outcome not written there is lost and the row
  keeps a stale `last_run_*` forever. The status still comes from the body, never from the fact
  that this is the failure path: a body reporting a result describes a run the agent completed
  where only the recording failed, so inventing `FAILED` would report a broken task to a caller
  whose agent ran fine. If even that write fails, the log line is the run's only surviving trace.

### Missed fires

- Before enqueue, delivery is the timer's problem: if the timer cannot deliver (queue throttling,
  transient failure) it retries per its own retry policy and, on exhaustion, delivers to the timer's
  DLQ. This is infrastructure behaviour, not Agent Kernel behaviour.
- Agent Kernel does not reconstruct or replay missed fires from the table. **This is a deliberate
  scope reduction** (listed for sign-off in *Open Questions*): configurable catch-up of missed runs
  is not provided — with timing delegated to infrastructure, there is no Agent Kernel process
  guaranteed to be awake to perform catch-up, and reconstructing fires would reintroduce the polling
  and locking this design removes.
- A fire that arrives outside an acceptable staleness window is logged as a warning and still
  executed; operators can detect gaps from `last_run_at` and from timer-side metrics.

### Conversation / session handling

- Each scheduled task chooses whether every run starts a fresh conversation or continues a
  long-running one. **The `session_id` is fully resolved before the message reaches the runner**, so
  the runner receives an ordinary message with a `session_id` already set and derives nothing:
  - **Per-run session** — `schedule:<scheduled_task_id>:<scheduled_time>`. The scheduled time is not
    known at registration, so the timer substitutes it into the payload at fire time (on AWS, the
    EventBridge Scheduler `<aws.scheduler.scheduled-time>` context variable — see *AWS
    Feasibility*).
  - **Continuous session** — the static value `schedule:<scheduled_task_id>`, baked into the payload
    at registration.
- Derived session ids carry the reserved `schedule:` prefix because `scheduled_task_id` is
  caller-choosable and lives in the same session-id namespace as user-supplied session ids — without
  the prefix, a user session whose id equals a scheduled task's id would share conversation state
  with the scheduled runs.
- **Resolving the session id is the provider's job, not the `ScheduledTaskService`'s.** Only the
  provider knows how its timer expresses the fire time, so the service hands over a message template
  without a `session_id` and the provider fills it in at registration — a static value in continuous
  mode, a substitution template in per-run mode.
- **Rotation is out of scope in this version.** A rotating continuous session
  (`floor(scheduled_time / rotation_period)`) needs arithmetic on the fire time, which neither the
  timer's template substitution nor a static payload can express — it would require the agent runner
  to compute a session id, which decision 5 rules out. Listed in *Open Questions* for sign-off.

### Output and logging

- Scheduled run results are not stored long-term; output lives wherever the agent's own actions
  leave it.
- The row records `last_run_at`, `last_run_status` and `last_error` — enough to answer "did this
  run, when, and did it succeed" — and each run emits the same logs and traces as any queued
  request, correlated by `scheduled_task_id` and `scheduled_time`.
- The run-completion write happens in the **output consumer** (response handler on serverless,
  output consumer on ECS), not the agent runner: it is the component that observes the terminal
  outcome of a run on the output queue. For each response carrying a `scheduled_run` block it makes
  exactly one call — `Scheduler.mark_run_completed(...)` — passing the ids and derived status.
- **A scheduled run has no live client channel, and the output consumer must account for that.** In
  the WebSocket execution modes the response handler broadcasts the reply to the originating
  connection (`deployment/aws/serverless/akresponsehandler.py:80-107`, dispatched at `:127-130`),
  using an `endpoint_url` attribute that a timer-originated message does not carry. A response with
  a `scheduled_run` block is therefore recorded on the row instead of broadcast, and is not written
  to the response store either — nobody is polling for it. The check runs **before** the
  execution-mode fan-out, since the WebSocket branches would otherwise raise. This is the one branch
  the feature adds to an existing component, and it lives in the consumer, not the runner.
  - The recognition step itself is written once and shared by both output consumers, so the
    serverless response handler and the ECS output consumer behave identically.
- **The output consumer depends only on the `Scheduler` interface.** It never resolves, imports or
  calls the `ScheduledTaskStore`, and it holds none of the outcome-write policy: loading the row,
  applying the four guards (*Outcome-write guards*), updating `last_run_at` / `last_run_status` /
  `last_error` / `last_run_scheduled_time`, and setting `status = COMPLETED` / `completed_at` for a
  one-time scheduled task all happen inside the `Scheduler` implementation. The consumer's only job
  is to recognise a `scheduled_run` block and forward it.
  - Consequence: the guard rules live in one place and are shared by both deployment targets, and a
    non-AWS provider can change how outcomes are persisted without touching either consumer.
- This still gives the output consumer new infrastructure on both targets: `Scheduler` wiring plus
  IAM grants to **read and update the table** (see *AWS Feasibility*).

### Ownership and identity

- **Every scheduled task is owned by an authenticated human identity.** With config-defined
  scheduled tasks out of scope, there is no system-identity source and no unowned scheduled task.
- **An agent can create a scheduled task, but an agent is never an owner.** A scheduled task created
  through an agent-callable tool is bound to the identity that owns the invoking session — the
  original human — exactly as if that person had called the chat endpoint themselves. There is no
  synthetic agent identity and no ownership handover; the agent is the mechanism, the human remains
  the principal.
- Unforgeability is a precondition, not an aspiration: **no scheduled task is created without a
  resolved identity, on any surface.** The rule is uniform; the check that enforces it is not, and
  cannot be — each surface authenticates differently and can prove the requirement at a different
  moment:
  - **FastAPI surfaces** (the queue-mode chat route and the `/api/v1/schedule` routes) require an
    `Authoriser`. Constructing them with `scheduler.enabled` and no `Authoriser` raises
    `AKConfigError` at initialization.
  - **WebSocket surfaces** (ECS and Lambda) have no `Authoriser` object at all: the connection is
    already authenticated at `$connect` by the deployment's `AuthValidator`, and the frame's
    resolved `user_id` is the owner. Requiring an `Authoriser` there would demand a second,
    redundant identity mechanism.
  - **The serverless REST surface** takes the owner from the API Gateway authorizer's
    `principalId`. Python cannot see whether Terraform attached the authorizer to the route, so
    this one is enforced **per request**: a schedule request arriving with no authorizer context is
    rejected with 401 rather than caught at initialization.
- The identity resolver is a shared concern, not a scheduling one: the `Authoriser` contract lives
  in the `auth` package (re-exported from the thread integration, where it originally sat) and the
  bearer-token resolution that uses it is shared by every route layer that needs a subject, so the
  thread and schedule routes read and reject tokens identically.
- The identity is written to the row by the server and stamped into the timer's message payload;
  it is never read from client input, so it cannot be forged or overridden.
- Scheduled activity is kept out of the user's regular conversation history: sessions created for
  scheduled runs skip `ConversationThreadManager` thread auto-creation, so scheduled runs never
  appear in the owner's thread listings. This is fixed behaviour, not configurable.

### How scheduled tasks get run

- One execution path only: the timer places the message on the input queue and the existing agent
  runner executes it. **This is a deliberate scope reduction** (listed for sign-off in *Open
  Questions*) — the "run the agent directly inside the scheduling process" option is dropped,
  because in queue mode there is no long-lived scheduler process to run it in, and the direct path
  would bypass the retry and DLQ behaviour the queue path already provides.
- A scheduled run goes through the same guardrails, hooks, logging and monitoring as any other agent
  run. No shortcuts.

### Management and administration

- **Scheduled tasks are defined one way: at runtime, by an authenticated caller** — a client through
  the chat endpoint, or an agent through the agent-callable tools. The `scheduler` config block
  carries deployment wiring only, never a task definition (see *Deployment applicability*).
- All scheduling logic lives in a shared **`ScheduledTaskService`**: request validation, identity
  stamping, `scheduled_task_id` generation when the caller supplies none, ownership and
  lifecycle-state checks, and the `Scheduler.upsert / delete / get / list` calls. Session-id
  resolution is **not** among them — that belongs to the provider (see *Conversation / session
  handling*).
  - **`ScheduledTaskService` is not a peer of `ChatService` and is not analogous to it.**
    `ChatService` runs agents; `ScheduledTaskService` never runs an agent — it only registers and
    manages schedules. In queue mode the chat route does not go through `ChatService` at all
    (`RestHandler` bypasses it deliberately — validation and execution happen in the agent runner),
    so the create path is: chat route → `ScheduledTaskService` → `Scheduler`.
    - The create branch itself lives in the provider-agnostic `RestHandler`
      (`deployment/common/rest_handler.py`), not in an AWS subclass: it contains nothing
      AWS-specific, so any queue-mode target inheriting it gets the create path.
  - It has three callers: the chat route (create), the `/api/v1/schedule` routes (read, update,
    delete) and the agent-callable tools (all four). No parallel code paths.
- Visibility and mutation follow ownership: the `GET` routes return only scheduled tasks owned by
  the authenticated caller, and update/delete additionally check ownership. Since every scheduled
  task has an authenticated owner, there is no category that is visible-but-not-editable.
- The service reports failures as a small typed hierarchy — invalid schedule, not found, not owned,
  soft-deleted — which each surface maps onto its own protocol (400 / 404 / 403 / 409 over HTTP,
  `{"error": ...}` for the agent tools). The mapping is declared once per surface rather than at
  each route.
- All management — REST and tool-invoked alike — goes through the existing authorization system.
- Operational consequence to accept: a fresh environment starts with an empty table. Seeding is an
  API call, not a deploy artefact.

### Creation — through the existing chat endpoint

There is **no new creation endpoint**. `POST /api/v1/chat` gains an optional `schedule` block in the
body:

- Body is the normal chat body (`prompt`, `agent`, `user_id`, ...) plus
  `schedule: { id?, cron | rate | at, mode, timezone }` — `mode` being per-run or continuous (see
  *Conversation / session handling*), `timezone` defaulting to `UTC`.
- The `schedule` block is checked **before** `session_id`, which a scheduled create legitimately
  omits: the session id is derived, not supplied.
- When `schedule` is present the request is **not enqueued for execution**. The request handler
  calls `ScheduledTaskService.create(...)` inline, which writes the row and registers the timer, and
  returns an acknowledgement. The first message on the input queue appears when the timer fires.
- `schedule.id` is the optional caller-supplied `scheduled_task_id`; when omitted the server
  generates `schedule_<uuid4>` (see *Identity of a scheduled task*).
- The caller's resolved identity is stamped as the owner and cannot be supplied in the body.
- Creating at an id with no live row assigns a fresh `scheduled_task_version`. Creating at an id
  that already has a live row is an upsert that **keeps** its `scheduled_task_version`, so in-flight
  runs still record their outcomes.
- Rejected with 400 for an invalid or too-fine schedule expression, 400 when `scheduler.enabled` is
  false, 403 when a live row at that id is owned by a different identity, and 409 when the id is
  soft-deleted (see *Deletion lifecycle*).

#### Creation acknowledgement

The acknowledgement is delivered on **the same channel the caller would have received a chat reply
on**, so a client does not need a second transport to learn the outcome. The payload is the same in
every mode:

```json
{
  "status": "SCHEDULED",
  "scheduled_task_id": "schedule_5f1c...",
  "scheduled_task_version": "…",
  "session_id": "schedule:schedule_5f1c...",
  "next_run_at": "2026-08-07T09:00:00Z",
  "request_id": "…"
}
```

Two of those fields are conditional, and absent fields are omitted rather than sent as `null`:

- **`session_id` appears in continuous mode only.** A per-run session id exists only at fire time,
  so returning its template would look like a usable session id without being one.
- **`next_run_at` is best-effort.** It is derivable from a one-time instant and from a rate — counted
  from when the definition was registered, not from when the id was first created, since a create
  over a live id re-bases the rate — but not from a cron expression — no timer API supplies a next-invocation time and a cron evaluator is not
  worth the dependency for a convenience field. Absent means "not computed", never "not scheduled";
  `last_run_at` on the row is the authoritative history.

| Execution mode | Delivery |
| --- | --- |
| `rest_sync` | Returned directly as the `201` response body. The handler does **not** wait on the response store — there is no run to wait for, so the sync wait is skipped entirely. |
| `rest_async` | The same `201` body. `GET /api/v1/chat/{session_id}` is not used for scheduling; run outcomes are read from `GET /api/v1/schedule/{scheduled_task_id}`. |
| `async` (WebSocket) | One message on the caller's live connection, carrying the same payload, sent by the request handler rather than the response handler — the acknowledgement never travels the queues. |
| `stream` (WebSocket / SSE) | A single terminal frame carrying the same payload with `done: true`. No token deltas: nothing is generated at creation time. |

- Errors are surfaced the same way an ordinary chat error is in that mode: an HTTP error response
  in the REST modes, a system/error message on the connection in the WebSocket modes.
- The acknowledgement confirms **registration**, not execution. Run outcomes are observed through
  `GET /api/v1/schedule/{scheduled_task_id}`.
- **The table above governs the acknowledgement only. A fire always executes as a non-stream
  execution, in every mode — including `stream`.** The two are produced at different moments by
  different actors: the ack by the request handler at create time, the fire by the timer later. A
  fire has no client connection to stream to — there was none when the schedule was registered — so
  streaming it has no meaning and no destination. It therefore runs the whole-response path, which is
  also the only one that carries the correlation block run outcomes are recorded from. A `stream`
  deployment is fully supported for scheduling; it simply does not stream the runs.

### Management API — `/api/v1/schedule`

These routes exist only to query and manage **already-created** scheduled tasks. Each supported
deployment style mounts a thin route layer over the same `ScheduledTaskService` — the handler
classes and mounting mechanics per target are implementation detail deferred to `spec.md`.

- On the FastAPI surfaces the schedule router is **mounted automatically** when scheduling is
  enabled, unless the application already supplies its own handler — which is how the `Authoriser`
  is provided. An app that enables scheduling and supplies none fails before the server binds,
  rather than serving routes that cannot establish an owner. On serverless the routes are
  registered under their API Gateway resource templates,
  because the hand-rolled router matches literal paths and has no path-parameter support of its own;
  a template lookup is used only where dispatch previously raised, so existing routing is unchanged.
- **List — `GET /api/v1/schedule`**. Scoped to the caller's own scheduled tasks, and **paginated** —
  `limit` plus an opaque cursor carried between pages. Soft-deleted rows are not returned — they are
  an internal grace-period artefact, not a user-visible state.
- **Read — `GET /api/v1/schedule/{scheduled_task_id}`**. Definition plus last-run status. `404` on
  an unknown or soft-deleted id.
- **Update — `PUT /api/v1/schedule/{scheduled_task_id}`**. Body may change the `schedule` block and
  the message fields (`prompt`, `agent`). Calls `Scheduler.upsert(...)`: the row is written and the
  timer registration replaced, so the next fire reflects the new definition.
  - Update does **not** create: `404` when there is no live row at that id. Creation is the chat
    endpoint's job.
  - `scheduled_task_version` is **retained**, so in-flight runs still record their outcomes after an
    update.
  - Updates affect future executions only: an execution already enqueued continues to use the
    definition that existed when it was enqueued (accepted behaviour).
  - A `PUT` on a one-time scheduled task that has already run re-arms it, but only when the body
    supplies a **new future `at`**: the elapsed instant cannot be re-registered, so a `PUT` without
    one is rejected with `400` naming the field to change rather than failing on a schedule the
    caller never sent. Given the new instant, the schedule is re-registered and `status` returns to
    `ACTIVE` with `completed_at` cleared. The version is retained — the same scheduled task,
    rescheduled.
  - A replacement `schedule` block that omits `mode` keeps the task's current mode. Retiming a
    `continuous` task therefore never silently moves it to a `per_run` session id; changing the mode
    takes naming it explicitly.
  - `403` if the caller does not own it, `409` if it is soft-deleted.
- **Delete — `DELETE /api/v1/schedule/{scheduled_task_id}`**. Calls `Scheduler.delete(...)`: the
  timer registration is removed, then the row is soft-deleted (see *Deletion lifecycle*). `403` if
  the caller does not own it.
- All routes require the configured identity resolver (see *Ownership and identity*); update and
  delete additionally check ownership.

### Agent-callable tools

- A `SystemTool` set (mirroring the sandbox capability's `get_sandbox_tools()` pattern in
  `sandbox/tools.py`, built on `SystemToolFactory` in `core/tool.py`) exposes scheduling to the
  agent itself, so a conversation can create and manage its own scheduled tasks without a separate
  API call: `create_scheduled_task`, `update_scheduled_task`, `delete_scheduled_task`,
  `list_scheduled_tasks`.
- These tools go through the same `ScheduledTaskService` as the REST surfaces — no parallel code
  path — and are the agent's equivalent of the chat endpoint's `schedule` block, since an agent has
  no HTTP client into its own deployment. They return JSON strings and report failures as
  `{"error": ...}` rather than raising into the framework.
- A scheduled task created via a tool is owned by the human identity that owns the invoking session
  (see *Ownership and identity*); the tool cannot set an arbitrary owner. The agent may choose the
  `scheduled_task_id`, exactly as an API client may.
  - **How that identity reaches tool code:** `user_id` is deliberately kept out of the agent's
    request context, so `ChatService` binds the request's authenticated user id onto the session's
    volatile cache and the tools read it from there. A tool that finds no bound user refuses rather
    than guessing an owner. On a scheduled fire the bound user is the task's own owner, so a task
    created from a scheduled run stays with the same person.
- Registration is gated behind the same applicability rule as the rest of the feature (AWS, queue
  mode): the tool set is only added to an agent's toolset when scheduling is enabled for the
  deployment — and then only to the agents in scope, since `scheduler.agents` narrows it (omitted =
  all agents, `[]` = none).
- **Consequence for the agent runner — it is not free of scheduling infrastructure.** These tools
  execute in-process inside the agent runner, so when the tool set is registered the runner resolves
  a `Scheduler` and needs the same access the REST surface has: **read and write on the table, and
  permission to create, update and delete timer registrations** (EventBridge Scheduler on AWS).
  - This is the only place scheduling reaches into the runner, and it is a *tool* concern, not an
    execution concern — the runner still has no scheduled-run branch on its message path (decision
    5). It is opt-in on both sides and the two gates line up: `scheduler.agents: []` scopes the
    tools away in the application, and a separate Terraform flag
    (`scheduled_task_config.enable_agent_tools`, default off) is what grants the runner the table
    and scheduler permissions at all. A deployment that enables scheduling without enabling the
    tools leaves the runner with no scheduler grants whatsoever.
- `create_scheduled_task` and `update_scheduled_task` are both `Scheduler.upsert` underneath,
  exposed as two tools because the distinct names and descriptions steer the model better than one
  overloaded tool; they are not two code paths.

## AWS Feasibility

How the AWS provider satisfies the `Scheduler` contract — only the claims a design reviewer needs
to judge the reliability guarantees. Field-level configuration (exact target ARNs, IAM policies,
Terraform changes) is deferred to `spec.md`. None of this is visible to the API, config or
`ScheduledTaskService` layers.

- Schedules are registered with **EventBridge Scheduler**, delivering directly to the input SQS
  queue via its universal target. The universal target is required, not a preference: it is the
  only SQS target that can set both `MessageGroupId` and `MessageDeduplicationId`, and it substitutes
  context variables into the payload at fire time.
- **Context-variable substitution is what keeps the runner clean.** The registered payload is a
  template; at fire time the timer substitutes `<aws.scheduler.scheduled-time>` and
  `<aws.scheduler.execution-id>` into the `scheduled_run` block, and — for per-run sessions —
  `<aws.scheduler.scheduled-time>` into the `session_id` field too. The message that lands on the
  queue is therefore a complete, ordinary agent message with nothing left to derive.
- The delivery attributes map onto SQS FIFO: `MessageGroupId` = `scheduled_task_id`
  (serialization); `MessageDeduplicationId` = `<scheduled_task_id>:<scheduled_time>` (at-most-once
  per scheduled time). No queue changes: the dedup id is set explicitly, so content-based
  deduplication stays off (`content_based_deduplication = false` on both queues, containerized
  `containerized/modules/queues/main.tf:18,54`) and existing chat traffic is untouched.
  - Note the departure from existing convention: ordinary chat traffic groups by `session_id`
    (`deployment/aws/core/sqs_handler.py:346,388` default `message_group_id` to the body's
    `session_id`). Scheduled fires deliberately group by `scheduled_task_id` instead, because the
    session id changes between fires in per-run mode and would not serialize a scheduled task's
    runs. Both kinds of traffic coexist on the same queue — group ids need only be distinct, and
    the reserved `schedule:` prefix on scheduled session ids (see *Conversation / session
    handling*) keeps the two id spaces from colliding.
- Minimum granularity: 1 minute, for both cron and rate expressions.
- One-time schedules delete their own registration after firing (`ActionAfterCompletion=DELETE`) —
  atomically, with no race against a concurrent delete and no scheduler permissions needed
  downstream.
- Accepted edge case: SQS's FIFO deduplication window is fixed at 5 minutes, so a timer-side retry
  delivered later than that would not be deduplicated. Schedules cap event age at ~300 s so a
  retried delivery cannot outlive the dedup window.
- Per supported target, the `ak-deployment` modules gain: a scheduled-task table following the
  session store type (dedicated DynamoDB table with a sparse owner index and TTL enabled for
  soft-delete expiry, or a separate table/keyspace on the existing Redis/Valkey cluster — no new
  infrastructure); an EventBridge Scheduler schedule group per deployment (for namespacing and
  destroy-time cleanup — deleting the group removes every registration the deployment made, so
  `terraform destroy` leaves no orphans); an execution role allowing the timer to send to the input
  queue; and the IAM grants below, enumerated in `spec.md`.
- The whole thing sits behind **one Terraform gate**, `scheduled_task`, mirroring the existing
  `queue_mode` gate: `true` creates every scheduler resource, IAM permission and route; `false` (the
  default) creates none of them and leaves the deployment byte-identical to today. A `validation`
  block rejects `scheduled_task = true` with `queue_mode = false`, which is where the queue-mode
  precondition is enforced (see *Deployment applicability*).
- IAM grants by component — deliberately unequal, so no component gets more than its role needs:
  - **REST service / request handler** (hosts the chat create path and the `/api/v1/schedule`
    routes): full table read/write, plus EventBridge Scheduler create/update/delete within the
    deployment's schedule group, plus `iam:PassRole` on the timer execution role — registering a
    schedule hands EventBridge Scheduler the role it assumes — plus `sqs:GetQueueAttributes` on the
    input queue for the soft-delete TTL derivation (see *Deletion lifecycle*).
    - On the containerized target the REST task also hosts the output-consumer thread, so it
      carries the full grant rather than a split one.
  - **Response handler / output consumer** (records run outcomes): table read and update only — no
    scheduler permissions, since it never registers or removes a schedule, and **no
    `sqs:GetQueueAttributes` either**: the TTL is derived lazily and only the delete path needs it,
    so a consumer that merely records outcomes never reads the input queue.
  - **Agent runner**: nothing by default. **Only when the agent-callable tool set is registered**
    does it need the same grants as the REST service (table read/write, scheduler
    create/update/delete, `iam:PassRole`, `sqs:GetQueueAttributes`) — see *Agent-callable tools*.
  - Scheduler grants are scoped to the **schedule** ARN namespace (`…:schedule/<group>/*`), not to
    the schedule-group ARN: a schedule is not addressed as a child of its group's ARN, so a grant
    scoped to `<group-arn>/*` would match no schedule at all and deny every scheduler call.

## Open Questions

Open design questions are tracked directly in this document. Three deliberate scope reductions need
explicit sign-off rather than implicit approval with the rest of the design:

1. **Configurable catch-up of missed fires is dropped** (see *Missed fires*). This design delegates
   missed-fire recovery entirely to the timer's retry policy and DLQ. Proposed resolution: accept
   the reduction — with timing delegated to infrastructure there is no Agent Kernel process
   guaranteed to be awake to perform catch-up, recurring schedules self-heal at the next fire, and
   reconstructing fires would reintroduce the polling and locking this design removes.
2. **The direct in-process execution path is dropped** (see *How scheduled tasks get run*). Two
   execution paths were considered — via the queue, or directly inside the scheduling process; this
   design ships only the queue path. Proposed resolution: accept the reduction — in queue mode there
   is no long-lived scheduler process to host a direct run, and the direct path would bypass the
   retry/DLQ behaviour the queue path provides.
3. **Rotating continuous sessions are dropped** (see *Conversation / session handling*). Only two
   session modes ship: per-run and continuous. Proposed resolution: accept the reduction — rotation
   needs arithmetic on the fire time, which the timer's template substitution cannot express, so
   supporting it would put a scheduling-specific session-derivation step in the agent runner and
   break the "a fire is an ordinary message" property. If unbounded continuous conversations turn
   out to be a real problem, the cheaper answer is a second scheduled task, not runner logic.
