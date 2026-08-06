# #554: Agent Kernel Task Scheduling

Add time-triggered agent invocation to Agent Kernel.

- Store a scheduled task in a task table and register it with an external **timer** service.
- When the timer fires, it enqueues a normal agent message onto the existing **input queue**.
- The existing agent runner consumes and executes it exactly as it would any other queued request.
- Availability (this version):
  - **AWS only**; Azure and GCP are not covered.
  - **Queue-mode scalable AWS deployments only** (scalable serverless Lambda and scalable containerized ECS).

## Problem Description

- Agent Kernel cannot invoke an agent on a schedule: every existing entry point — REST
  (`/api/v1/chat`), queue-based ingestion, A2A, MCP — requires an external caller to initiate the
  request. Nothing in the system can trigger agent work on its own.
- Some agent work is triggered by time, not by a user or an upstream system: a recurring report, a
  periodic cleanup, a scheduled check.
- That work needs to run on a cron-like schedule and deliver a prompt to the appropriate agent with
  no human or calling system in the loop.

## Design Choices

Four decisions shape the rest of this document:

1. **AWS only.**
   - The only implementation delivered: a pluggable task table (DynamoDB, Redis or Valkey,
     following the session store type — see *The `ScheduleTaskStore` abstraction*), EventBridge Scheduler
     for the timer, SQS for delivery.
   - Azure and GCP do not get task scheduling in this version; the `Scheduler` ABC exists so those
     can be added later, not because a second provider is being written now.
2. **Queue mode only.**
   - Within AWS, scheduling is delivered exclusively for scalable deployments running with
     `queue_mode = true`: scalable serverless (Lambda) and scalable containerized (ECS).
   - Non-queue deployments (single-Lambda serverless, plain REST container, local runs) do not get
     task scheduling in this version.
3. **Timing is delegated to an external timer, not to an in-process poller.**
   - A `Scheduler` ABC owns the task table and the registration of schedules with a platform timer.
   - On AWS the timer is **EventBridge Scheduler**.
4. **The timer's target is the input queue, not the agent.**
   - When a schedule fires, the timer delivers the pre-built message to the input queue (SQS).
   - From that point the request is indistinguishable from any other queued request and flows
     through the existing agent runner, guardrails, hooks, tracing, retries and DLQ.
   - After execution, the task table row is updated to record that the run completed.

### Flow

```
create/update task
  → Scheduler.upsert()
      → write task row to the task store
      → register schedule with the timer: recurring (cron/rate) or one-time (at),
        carrying the message payload and delivery attributes
timer fires
  → timer puts message on the input queue (grouped by task_id)
  → agent runner consumes → runs the agent (normal path)
  → runner publishes ScheduledTaskResponse to the output queue (same task_id group)
  → output consumer → Scheduler.mark_run_completed() → task store
      last_run_at, last_run_status = COMPLETED / FAILED
```

### Why this shape

- **No leader election, no distributed lock, no dedup window.** No replica polls for due work, so
  there is nothing for replicas to race over. The timer fires once per scheduled time, and delivery
  is deduplicated per `(task_id, scheduled_time)` as a cheap safety net against timer-side
  at-least-once delivery.
- **No new execution path.** The scheduled run reuses the input queue, so it inherits the retry
  handling, DLQ, concurrency controls, guardrails, hooks and observability that already apply to
  queued requests. Nothing is bypassed.
- **The scheduler process does not need to be running when a task is due.** The timer is
  infrastructure; a scaled-to-zero Lambda deployment still fires.

## Scope

- Define tasks that run on a schedule — recurring (cron or rate) or one-time (at a given instant).
- Have those tasks automatically place a prompt for an AI agent on the input queue when due.
- Manage scheduled tasks (create, view, update, delete) through an API and through agent-callable
  tools. Every task originates from an authenticated caller.
- Enqueue a task exactly once per scheduled time, including when the deployment is running many
  replicas; execution then follows the existing at-least-once queue retry semantics.
- Record each run's outcome on the task row (with one documented exception: retry-exhausted runs —
  see *Reliability and duplication prevention*).

## Out of Scope

- Sending notifications or alerts when a task completes — output is limited to whatever the agent
  itself produces.
- Non-AWS clouds. No Azure or GCP scheduler implementation is written in this version; the ABC keeps
  the door open, nothing more.
- Non-queue AWS deployments (single-Lambda serverless, plain REST container, local development runs).
- An Agent Kernel-owned catch-up mechanism for missed runs. Recovery of delayed or missed fires is
  the timer's responsibility (see *Missed runs* below).
- DLQ processing to reconcile retry-exhausted executions. A run that exhausts input-queue retries
  is not reflected on the task row in this version (see the known limitation under *Reliability
  and duplication prevention*); a future iteration may add it.
- Long-term storage of scheduled run results.

## Key Requirements

### Deployment applicability

- Task scheduling is enabled only on AWS, and only when the deployment runs in queue mode. Supported
  targets — the complete list for this version:
  - AWS scalable serverless (`examples/aws-serverless/scalable-openai`) — request handler /
    agent runner / response handler Lambdas over SQS.
  - AWS scalable containerized (`examples/aws-containerized/openai-dynamodb-scalable`) — REST
    service + agent runner ECS tasks over SQS.
- Scheduling is switched on by a new `scheduler` config block (`scheduler.enabled: true`). The
  task-store backend is not part of this block — it follows the session store type (see *The
  `ScheduleTaskStore` abstraction*). Queue mode has no config flag of its own — in code it is detected by
  both `execution.queues.input.url` and `execution.queues.output.url` being set — so the enablement
  check is: `scheduler.enabled` with either queue URL unset raises `AKConfigError`. A non-queue
  deployment fails loudly, not silently.
- Configuration validation runs during **component initialization**: process startup on
  long-running services (ECS), cold start on serverless — in both cases before the first request
  or scheduled execution is processed. "At initialization" throughout this document means this.
- **Precondition: the input queue is FIFO — enforced at initialization.** The
  duplication-prevention and serialization guarantees below depend on it. The containerized
  terraform hardcodes `fifo_queue = true` and serverless defaults it to `true`, but serverless
  exposes it as a toggle — disabling it would silently break dedup. Since FIFO queue URLs always
  end in `.fifo`, the enablement check above is extended to enforce this: `scheduler.enabled` with
  an input queue URL not ending in `.fifo` also raises `AKConfigError`, keeping the "fails loudly,
  not silently" principle uniform.

### The `Scheduler` abstraction

- A `Scheduler` ABC defines the provider-agnostic contract, and is implemented in code as a real
  abstraction (not a thin wrapper) so a non-AWS provider can be added later without touching the API
  or config layers. Only the AWS implementation is built and shipped in this version:
  - `upsert(task)` — persist the task row and register (or re-register) its schedule with the timer,
    carrying the message body the timer should deliver.
  - `delete(task_id)` — remove the timer registration and soft-delete the task row (see *Deletion
    lifecycle*).
  - `get(task_id)` / `list(...)` — read task definitions and their last-run state.
  - `mark_run_completed(task_id, task_version, scheduled_time, status, last_error=None)` — record
    run outcome (status plus optional error detail) on the task row.
    There is deliberately no `mark_run_started`: only terminal outcomes are recorded, so a stuck
    run is visible from queue metrics instead of requiring a started-state write on every fire.
- **The `Scheduler` is the only component that touches the task store.** The `ScheduleTaskStore` is
  an implementation detail held by the `Scheduler`; no caller — not the `TaskService`, not the
  output consumer — resolves or calls the store directly. Every task-row read and write goes
  through a `Scheduler` method.
- Beyond the method contract, any implementation must guarantee:
  - **At-most-once delivery per `(task_id, scheduled_time)`** — a duplicate timer-side fire is
    suppressed before it reaches the agent runner.
  - **Fires of the same task are serialized** — a fire does not overtake or interleave with an
    earlier fire of the same task.
  - **One-time registrations remove themselves after firing** — no separate cleanup process.
  - **Schedules finer than the provider's minimum granularity are rejected at registration**, not
    silently rounded.
  - **Outcome writes are guarded against stale and mismatched runs** — `mark_run_completed` is a
    no-op when the task row is absent, soft-deleted, belongs to a different incarnation of the
    id, or reports a `scheduled_time` older than the one already recorded (see *Outcome-write
    guards*).
- The ABC + factory follow the existing pluggable-backend pattern (`Trace`, `ResponseStore`,
  `QueueHandler`), so a non-AWS timer can be added later without touching the API or config layer.
  How AWS satisfies each obligation is in *AWS Feasibility* below; none of it leaks above this
  contract.

### The `ScheduleTaskStore` abstraction

- The task table itself is a separate, pluggable concern from the timer, mirroring the existing
  `ResponseStore` ABC (`deployment/common/response_store.py`) and its backends
  (`deployment/aws/core/response_store/{dynamodb.py,redis.py,valkey.py}`): a `ScheduleTaskStore` ABC with
  DynamoDB, Redis and Valkey implementations.
- It is a **private collaborator of the `Scheduler`**, not a public seam: the `Scheduler` holds it
  and is the only caller. Pluggability here is about supporting three storage backends behind one
  timer implementation, not about giving other components a way in (see *The `Scheduler`
  abstraction*).
- The backend is not configured separately: the task store uses the **same backend type as the
  session store** (`session.type`). DynamoDB sessions → a dedicated DynamoDB task table; Redis or
  Valkey sessions → the **same cluster** the sessions use, with a separate table/keyspace — no new
  infrastructure is provisioned. Any other session type (e.g. in-memory) fails the enablement check
  at initialization, since scheduling needs a durable store shared by all replicas.
- The task table is always a **new table/keyspace** dedicated to tasks — it is never a partition of
  an existing session or response-store table, even when the underlying cluster is shared.
- The task row carries: the task definition (schedule plus the `ScheduledTaskRequest` template),
  the owner identity, `task_version` (the incarnation token — see *Outcome-write guards*),
  `status` (`ACTIVE` | `COMPLETED`), `last_run_at` / `last_run_status` / `last_error` /
  `last_run_scheduled_time`, `completed_at` (one-time tasks), and the soft-delete fields
  `deleted` / `deleted_at` / `ttl` (see *Deletion lifecycle*).

### Schedule definition and timing

- A schedule is one of: a cron expression, a fixed rate, or a one-time instant.
- Minimum granularity is whatever the timer provider supports; schedules finer than that are
  rejected at create/update time (a `Scheduler` obligation — see above).
- One-time schedules: the timer registration removes itself after firing (a `Scheduler`
  obligation), but the task row is **kept** — `mark_run_completed` records the run's outcome and
  sets `status = COMPLETED` / `completed_at`. `GET` keeps answering "did this run, when, and did
  it succeed" after the fire, and there is no orphan-row problem: the row is removed only by an
  explicit delete (see *Deletion lifecycle*). Recurring task rows are likewise never deleted
  except by an explicit delete.

### Deletion lifecycle

- Deleting a task — always via the API or an agent-callable tool — is a **soft delete**, not an
  immediate physical removal: the timer registration is removed first, then the row is marked
  `deleted = true` with `deleted_at` and a TTL; the store expires the row afterwards (DynamoDB TTL;
  key expiry on Redis/Valkey).
- **The TTL is derived, not a fixed constant.** It sizes the window during which the id stays
  reserved, and should outlive the longest execution an already-enqueued fire can have — so an
  in-flight run's id cannot be claimed by a new task while that run is still going. It is computed
  at initialization as `input queue visibility timeout × execution.queues.input.max_receive_count`
  (`core/config.py:317-319`, default 3) plus a safety margin, floored at a documented minimum.
  - Correctness does not depend on getting this number right — the `task_version` guard rejects a
    cross-incarnation outcome however short the TTL turns out to be (see *Outcome-write guards*).
    The derivation exists to make the common case unsurprising, not to carry the guarantee.
  - The visibility timeout is an SQS queue attribute set by Terraform
    (`queue_config.input_queue_visibility_timeout`), not an `AKConfig` field, so the AWS
    implementation reads it once at initialization via `GetQueueAttributes` — an IAM grant the
    scheduler-enabled components need (see *AWS Feasibility*). If the call fails, initialization
    fails loudly rather than falling back to a guessed TTL.
- **A deleted task is terminal.** It cannot be restored or transitioned back to an active state,
  and while the deleted row still exists `PUT` on the same `task_id` is rejected — after TTL
  expiry the id can be reused, which creates a **new incarnation** with a fresh `task_version`
  (see *Outcome-write guards*).
- The soft delete closes the delete/fire race without locking or immediate physical deletion:
  removing the registration stops future fires, and a fire already on the queue when the delete
  lands still executes, but `mark_run_completed` sees `deleted = true` and discards the outcome —
  a queued execution can never recreate or mutate state for a deleted task.

### Outcome-write guards

`task_id` is client-chosen and reusable after TTL expiry, and a fire's outcome can arrive
arbitrarily late, so `mark_run_completed` cannot assume the row it finds belongs to the run
reporting. It applies four guards, in order, and is a **silent no-op** (logged at warning, message
still acknowledged — never retried, never dead-lettered) when any of them rejects the write:

- **Row absent** — the task was deleted and its TTL has since expired. Nothing to record.
- **Row soft-deleted** (`deleted = true`) — the task was deleted while this run was in flight.
- **Incarnation mismatch** — the row's `task_version` differs from the `task_version` carried in
  the message. `task_version` is a server-generated token (a UUID or creation timestamp) stamped
  onto the row at creation and into the `ScheduledTaskRequest` payload at registration, so an
  outcome from a deleted-and-recreated task can never be written onto its successor's row. This
  guard is what makes client-chosen, reusable `task_id`s safe; existence checking alone is not
  sufficient.
- **Stale scheduled time** — the reported `scheduled_time` is older than the row's
  `last_run_scheduled_time`. Defence in depth behind FIFO ordering (see *Reliability and
  duplication prevention*), so a redelivered older outcome cannot overwrite a newer one.

### Message payload

- Scheduling traffic travels in dedicated message models, not loose metadata on the chat body, so
  correlation is explicit end to end and the fields pass unchanged through the whole pipeline
  (scheduler → request → runner → response → output consumer):
  - **`ScheduledTaskRequest`** — the body registered with the timer: the standard input-queue
    fields (`prompt`, `agent`, ...) plus `task_id`, `task_version`, `scheduled_time` and `run_id`
    (the latter two stamped by the timer at fire time), the conversation mode and rotation period,
    and the owning identity.
  - **`ScheduledTaskResponse`** — placed on the output queue by the agent runner: `task_id`,
    `task_version`, `scheduled_time`, `run_id`, `status`, `error`. The runner copies `task_id` and
    `task_version` through from the request unchanged; both are needed by the outcome-write guards.
- The request carries the session *policy*, not a final `session_id` — the schedule payload is a
  static template, so the agent runner derives the session id at consume time (see *Conversation /
  session handling*).
- Delivery attributes on the **input** queue: fires are grouped and serialized by `task_id` —
  stable across fires regardless of conversation mode — and deduplicated by
  `<task_id>:<scheduled_time>`.
- Delivery attributes on the **output** queue: the same `MessageGroupId = task_id`, so a task's
  outcomes are processed in order and a stale outcome cannot overwrite a newer one. This requires
  no new mechanism — the agent runner already propagates the incoming message's group and dedup ids
  verbatim when publishing to the output queue
  (`deployment/aws/containerized/akagentrunner.py:73-82`,
  `deployment/aws/serverless/akagentrunner.py:93`), so grouping the input fire by `task_id`
  automatically groups its outcome by `task_id` too. The output queue is FIFO on both supported
  targets (containerized `modules/queues/main.tf:53`; serverless shares the `fifo_queue` variable
  across both queues, `modules/queues/main.tf:15,60`).
  - Consequence of the same propagation: the outcome inherits dedup id
    `<task_id>:<scheduled_time>`, so if an at-least-once redelivery re-executes a run within the
    5-minute FIFO dedup window, only the first outcome is published. This is harmless — the guards
    make repeat outcome writes idempotent anyway — but it is deliberate, not accidental.

### Reliability and duplication prevention

- Exactly one **enqueue** per scheduled time, with any number of replicas running. Guaranteed by the
  timer firing once, backed by the `Scheduler`'s at-most-once delivery obligation.
- After enqueue, execution follows the existing at-least-once queue semantics: a consumer crash
  after the agent's side effects but before message deletion redelivers and re-executes the run.
  Agents whose actions must not repeat need to be idempotent, as with any queued request today.
- No leader server and no distributed lock is required — by construction, not by configuration.
- **Outcome ordering per task is guaranteed, not assumed.** Outcomes travel the output queue under
  `MessageGroupId = task_id` on a FIFO queue (see *Message payload*), so a task's outcomes are
  consumed in publish order and a stale outcome cannot overwrite a newer one. The stale-time guard
  in `mark_run_completed` (see *Outcome-write guards*) is defence in depth behind that ordering,
  not the primary mechanism.
- Failures after the message reaches the queue are handled by the existing queue retry policy and
  DLQ. A failure the runner observes and reports on the output queue (`ScheduledTaskResponse` with
  `status = FAILED`) is recorded on the task row.
- **Known limitation — retry-exhausted runs are not recorded on the task row.** A message that
  exhausts input-queue retries moves to the DLQ and never reaches the output queue where outcomes
  are recorded, so the task row keeps its previous last-run state. This is an intentional
  limitation of the initial design, accepted to keep the architecture simple; such runs are
  detected from DLQ metrics and alarms, not the task row. A future iteration may introduce DLQ
  processing to reconcile retry-exhausted executions (see *Out of Scope*).

### Missed runs

- If the timer cannot deliver (queue throttling, transient failure) it retries per its own retry
  policy and, on exhaustion, delivers to the timer's DLQ.
- Agent Kernel does not reconstruct or replay missed fires from the task table. **This is a
  deliberate scope reduction** (listed for sign-off in *Open Questions*): configurable catch-up of
  missed runs is not provided — with timing delegated to infrastructure, there is no Agent Kernel
  process guaranteed to be awake to perform catch-up, and reconstructing fires would reintroduce
  the polling and locking this design removes.
- A fire that arrives outside an acceptable staleness window is logged as a warning and still
  executed; operators can detect gaps from the task row's `last_run_at` and from timer-side metrics.

### Conversation / session handling

- Each task chooses whether every run starts a fresh conversation or continues a long-running one.
  The schedule payload carries the mode and rotation period only; the **agent runner derives the
  actual `session_id` at consume time** with a small pure helper:
  - **Per-run session** — `task:<task_id>:<scheduled_time>`.
  - **Continuous session** — `task:<task_id>`.
  - **Continuous with rotation** — `task:<task_id>:<floor(scheduled_time / rotation_period)>`; the
    bucket index changes only when the rotation window rolls over, so a conversation does not grow
    without bound.
- Derived session ids carry the reserved `task:` prefix because `task_id` is client-chosen
  (`PUT /api/v1/task/{task_id}`) and lives in the same session-id namespace as user-supplied
  session ids — without the prefix, a user session whose id equals a task's id would share
  conversation state with the scheduled runs.

### Output and logging

- Scheduled run results are not stored long-term; output lives wherever the agent's own actions
  leave it.
- The task row records `last_run_at`, `last_run_status` and `last_error` — enough to answer "did this
  run, when, and did it succeed" — and each run emits the same logs and traces as any queued request,
  correlated by `task_id` and `scheduled_time`.
- The run-completion write happens in the **output consumer** (response handler on serverless,
  output consumer on ECS), not the agent runner: it is the component that observes the terminal
  outcome of a run on the output queue. For each `ScheduledTaskResponse` it makes exactly one call
  — `Scheduler.mark_run_completed(...)` — passing the ids and outcome from the message.
- **The output consumer depends only on the `Scheduler` interface.** It never resolves, imports or
  calls the `ScheduleTaskStore`, and it holds none of the outcome-write policy: loading the row,
  applying the four guards (*Outcome-write guards*), updating `last_run_at` / `last_run_status` /
  `last_error` / `last_run_scheduled_time`, and setting `status = COMPLETED` / `completed_at` for a
  one-time task all happen inside the `Scheduler` implementation. The consumer's only job is to
  recognise a `ScheduledTaskResponse` and forward it.
  - Consequence: the guard rules live in one place and are shared by both deployment targets, and a
    non-AWS provider can change how outcomes are persisted without touching either consumer.
- This still gives the output consumer new infrastructure on both targets: `Scheduler` wiring plus
  IAM grants to **read and update the task table** (see *AWS Feasibility*).

### Ownership and identity

- **Every task is owned by an authenticated identity.** With config-defined tasks out of scope,
  there is no system-identity task source and no unowned task — a task created through the REST
  routes is bound to the caller's identity, and one created through an agent-callable tool is bound
  to the identity of the invoking session.
- Unforgeability is a precondition, not an aspiration: **an identity resolver is unconditionally
  required whenever `scheduler.enabled` is true** — the `Authoriser` on the FastAPI surface, the
  API Gateway authorizer on serverless. Enabling scheduling without one fails at initialization,
  with the same timing as the queue-mode check. There is no task source exempt from this rule, so
  the requirement is a single unambiguous check rather than a per-source condition.
- The identity is written to the task row by the server and stamped into the timer's message payload;
  it is never read from client input, so it cannot be forged or overridden.
- Scheduled task activity is kept out of the user's regular conversation history: sessions created
  for scheduled runs skip `ConversationThreadManager` thread auto-creation, so scheduled runs never
  appear in the owner's thread listings. This is fixed behaviour, not configurable.

### How tasks get run

- One execution path only: the timer places the message on the input queue and the existing agent
  runner executes it. **This is a deliberate scope reduction** (listed for sign-off in *Open
  Questions*) — the "run the agent directly inside the scheduling process" option is dropped,
  because in queue mode there is no long-lived scheduler process to run it in, and the direct path
  would bypass the retry and DLQ behaviour the queue path already provides.
- A scheduled run goes through the same guardrails, hooks, logging and monitoring as any other agent
  run. No shortcuts.

### Management and administration

- **Tasks are defined one way: at runtime, by an authenticated caller** — through the REST routes
  or the agent-callable tools, both of which go through the same `TaskService`. The `scheduler`
  config block carries only `enabled`.
  - Because every task has an authenticated owner, the identity resolver is unconditionally
    required whenever scheduling is enabled.
- API visibility and mutation follow ownership: the `GET` routes return only tasks owned by the
  authenticated caller, and update/delete additionally check ownership. Since every task has an
  authenticated owner, there is no category of task that is visible-but-not-editable.
- All task management — REST and tool-invoked alike — goes through the existing authorization
  system.
- Operational consequence to accept: a fresh environment starts with an empty task table. Seeding is
  an API call, not a deploy artefact.

### REST API surface

All task-management logic lives in a shared **`TaskService`** (analogous to `ChatService`):
request validation, identity stamping, and the `Scheduler.upsert / delete / get / list` calls.
Each supported deployment style mounts a thin route layer over the same `TaskService` — the
handler classes and mounting mechanics per target are implementation detail deferred to `spec.md`.

The routes — create and update are a single operation, because `Scheduler.upsert` is
create-or-replace:

- **Create / update — `PUT /api/v1/task/{task_id}`**. Body is the normal chat/run body (`prompt`,
  `agent`, ...) extended with a `schedule` object (cron / rate / one-time `at`, plus conversation
  mode and rotation period). The path param is the task's `task_id`, which is also the
  idempotency key. Calls `Scheduler.upsert(...)`: the task row is written and the timer
  registration is created or replaced, so the next fire reflects the new schedule/prompt/agent.
  Updates affect future scheduled executions only: an execution already enqueued continues to use
  the task definition that existed when it was enqueued (accepted behaviour). The caller's
  resolved identity is stamped as the task owner and cannot be supplied in the body.
  - Creating a task at an id with no live row assigns a fresh `task_version`; updating an existing
    live row **keeps** its `task_version`, so in-flight runs of that task still record their
    outcomes after an update.
  - A `PUT` on an existing one-time task whose `status` is `COMPLETED` re-arms it: the schedule is
    re-registered and `status` returns to `ACTIVE` with `completed_at` cleared. The
    `task_version` is retained (the same task, rescheduled).
  - Rejected with 409 if the task is soft-deleted (see *Deletion lifecycle*), and with 403 if the
    live row is owned by a different identity.
- **Read — `GET /api/v1/task`** (list, scoped to the caller's own tasks) and
  **`GET /api/v1/task/{task_id}`** (single task definition + last-run status). Soft-deleted rows
  are not returned by either route — they are an internal grace-period artefact, not a user-visible
  state; a `GET` on a soft-deleted id returns 404.
- **Delete — `DELETE /api/v1/task/{task_id}`**. Calls `Scheduler.delete(...)`: the timer
  registration is removed, then the row is soft-deleted (see *Deletion lifecycle*). Rejected with
  403 if the caller does not own the task.
- All routes require the configured identity resolver (see *Ownership and identity*); update and
  delete additionally check task ownership.

### Agent-callable tool

- A `SystemTool` set (mirroring the sandbox capability's `get_sandbox_tools()` pattern in
  `sandbox/tools.py`, built on `SystemToolFactory` in `core/tool.py`) exposes task management to the agent itself, so a
  conversation can create, update, delete or list its own scheduled tasks without a separate API call:
  `schedule_task`, `update_scheduled_task`, `delete_scheduled_task`, `list_scheduled_tasks`.
- These tools go through the same `TaskService` as the REST routes — no parallel code path. A task
  created via the tool is owned by the identity of the session that invoked it, exactly like an
  API-created task; the tool cannot set an arbitrary owner.
- Registration is gated behind the same applicability rule as the rest of scheduling (AWS, queue
  mode): the tool set is only added to an agent's toolset when scheduling is enabled for the
  deployment.
- **Consequence for the agent runner — it is no longer free of scheduling infrastructure.** These
  tools execute in-process inside the agent runner, so when the tool set is registered the runner
  resolves a `Scheduler` and needs the same access the REST surface has: **read and write on the
  task table, and permission to create, update and delete timer registrations** (EventBridge
  Scheduler on AWS). This is the one place scheduling reaches into the runner, and it is opt-in —
  a deployment that enables scheduling but excludes the tool set from its agents leaves the runner
  with no task-store or scheduler grants at all. The grants are therefore scoped per target in
  `spec.md`, not applied unconditionally to every runner.
- `schedule_task` and `update_scheduled_task` are both `Scheduler.upsert` underneath, exposed as
  two tools because the distinct names and descriptions steer the model better than one overloaded
  tool; they are not two code paths.

## AWS Feasibility

How the AWS provider satisfies the `Scheduler` contract — only the claims a design reviewer needs
to judge the reliability guarantees. Field-level configuration (exact target ARNs, IAM policies,
Terraform changes) is deferred to `spec.md`. None of this is visible to the API, config or
`TaskService` layers.

- Schedules are registered with **EventBridge Scheduler**, delivering directly to the input SQS
  queue via its universal target. The universal target is required, not a preference: it is the
  only SQS target that can set both `MessageGroupId` and `MessageDeduplicationId`, and it stamps
  the scheduled time and a unique execution id (the `run_id`) into the payload at fire time.
- The delivery attributes map onto SQS FIFO: `MessageGroupId` = `task_id` (serialization);
  `MessageDeduplicationId` = `<task_id>:<scheduled_time>` (at-most-once per scheduled time). No
  queue changes: the dedup id is set explicitly, so content-based deduplication stays off
  (`content_based_deduplication = false` on both queues, containerized
  `modules/queues/main.tf:18,54`) and existing chat traffic is untouched.
  - Note the departure from existing convention: ordinary chat traffic groups by `session_id`
    (`deployment/aws/core/sqs_handler.py:346,388` default `message_group_id` to the body's
    `session_id`). Scheduled fires deliberately group by `task_id` instead, because the derived
    session id changes between fires in per-run and rotating modes and would not serialize a task's
    runs. Both kinds of traffic coexist on the same queue — group ids need only be distinct, and
    the reserved `task:` prefix on derived session ids (see *Conversation / session handling*)
    keeps the two id spaces from colliding.
- Minimum granularity: 1 minute, for both cron and rate expressions.
- One-time schedules delete their own registration after firing (`ActionAfterCompletion=DELETE`) —
  atomically, with no race against a concurrent delete and no scheduler permissions needed
  downstream.
- Accepted edge case: SQS's FIFO deduplication window is fixed at 5 minutes, so a timer-side retry
  delivered later than that would not be deduplicated. Schedules cap event age at ~300 s so a
  retried delivery cannot outlive the dedup window.
- Per supported target, the `ak-deployment` modules gain: a task table following the session store
  type (dedicated DynamoDB table with TTL enabled for soft-delete expiry, or a separate
  table/keyspace on the existing Redis/Valkey cluster — no new infrastructure); an EventBridge
  Scheduler schedule group per deployment (for namespacing and destroy-time cleanup); an execution
  role allowing the timer to send to the input queue; and the IAM grants below, enumerated in
  `spec.md`.
- IAM grants by component — deliberately unequal, so no component gets more than its role needs:
  - **REST service / request handler** (hosts the task routes): full task-table read/write, plus
    EventBridge Scheduler create/update/delete within the deployment's schedule group.
  - **Response handler / output consumer** (records run outcomes): task-table read and update only
    — no scheduler permissions, since it never registers or removes a schedule.
  - **Agent runner**: nothing by default. **Only when the agent-callable tool set is registered**
    does it need the same grants as the REST service (task-table read/write plus scheduler
    create/update/delete) — see *Agent-callable tool*.
  - Every component that constructs a `Scheduler` also needs `sqs:GetQueueAttributes` on the input
    queue, to read the visibility timeout the soft-delete TTL is derived from (see *Deletion
    lifecycle*).

## Open Questions

Open design questions are tracked directly in this document. Two deliberate scope reductions need
explicit sign-off rather than implicit approval with the rest of the design:

1. **Configurable catch-up of missed runs is dropped** (see *Missed runs*). This design delegates
   missed-fire recovery entirely to the timer's retry policy and DLQ. Proposed resolution: accept
   the reduction — with timing delegated to infrastructure there is no Agent Kernel process
   guaranteed to be awake to perform catch-up, recurring tasks self-heal at the next fire, and
   reconstructing fires would reintroduce the polling and locking this design removes.
2. **The direct in-process execution path is dropped** (see *How tasks get run*). Two execution
   paths were considered — via the queue, or directly inside the scheduling process; this design
   ships only the queue path. Proposed resolution: accept the reduction — in queue mode there is
   no long-lived scheduler process to host a direct run, and the direct path would bypass the
   retry/DLQ behaviour the queue path provides.
