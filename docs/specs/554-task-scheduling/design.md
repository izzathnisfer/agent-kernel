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
     following the session store type — see *The `TaskStore` abstraction*), EventBridge Scheduler
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
  → timer puts message on the input queue
  → agent runner consumes → runs the agent (normal path)
  → task table updated: last_run_at, last_run_status = COMPLETED / FAILED
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
- Manage scheduled tasks (create, view, update, delete) through both configuration files and an API.
- Enqueue a task exactly once per scheduled time, including when the deployment is running many
  replicas; execution then follows the existing at-least-once queue retry semantics.
- Record each run's outcome on the task row.

## Out of Scope

- Sending notifications or alerts when a task completes — output is limited to whatever the agent
  itself produces.
- Non-AWS clouds. No Azure or GCP scheduler implementation is written in this version; the ABC keeps
  the door open, nothing more.
- Non-queue AWS deployments (single-Lambda serverless, plain REST container, local development runs).
- An Agent Kernel-owned catch-up mechanism for missed runs. Recovery of delayed or missed fires is
  the timer's responsibility (see *Missed runs* below).
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
  `TaskStore` abstraction*). Queue mode has no config flag of its own — in code
  it is detected by `execution.queues.input.url` being set — so the startup check is:
  `scheduler.enabled` with `execution.queues.input.url` unset raises `AKConfigError`. A non-queue
  deployment fails loudly, not silently.
- **Precondition: the input queue is FIFO.** The duplication-prevention and serialization guarantees
  below depend on it. The containerized terraform hardcodes `fifo_queue = true` and serverless
  defaults it to `true`, but serverless exposes it as a toggle — disabling it silently breaks
  dedup, so FIFO is stated here as an explicit requirement of enabling scheduling.

### The `Scheduler` abstraction

- A `Scheduler` ABC defines the provider-agnostic contract, and is implemented in code as a real
  abstraction (not a thin wrapper) so a non-AWS provider can be added later without touching the API,
  config or reconciliation layers. Only the AWS implementation is built and shipped in this version:
  - `upsert(task)` — persist the task row and register (or re-register) its schedule with the timer,
    carrying the message body the timer should deliver.
  - `delete(task_id)` — remove the timer registration and the task row.
  - `get(task_id)` / `list(...)` — read task definitions and their last-run state.
  - `mark_run_completed(task_id, scheduled_time, status)` — record run outcome on the task row.
    There is deliberately no `mark_run_started`: only terminal outcomes are recorded, so the agent
    runner needs no task-store access or IAM grants, and a stuck run is visible from queue metrics
    instead.
- Beyond the method contract, any implementation must guarantee:
  - **At-most-once delivery per `(task_id, scheduled_time)`** — a duplicate timer-side fire is
    suppressed before it reaches the agent runner.
  - **Fires of the same task are serialized** — a fire does not overtake or interleave with an
    earlier fire of the same task.
  - **One-time registrations remove themselves after firing** — no separate cleanup process.
  - **Schedules finer than the provider's minimum granularity are rejected at registration**, not
    silently rounded.
- The ABC + factory follow the existing pluggable-backend pattern (`Trace`, `ResponseStore`,
  `QueueHandler`), so a non-AWS timer can be added later without touching the API or config layer.
  How AWS satisfies each obligation is in *AWS Feasibility* below; none of it leaks above this
  contract.

### The `TaskStore` abstraction

- The task table itself is a separate, pluggable concern from the timer, mirroring the existing
  `ResponseStore` ABC (`deployment/common/response_store.py`) and its backends
  (`deployment/aws/core/response_store/{dynamodb.py,redis.py,valkey.py}`): a `TaskStore` ABC with
  DynamoDB, Redis and Valkey implementations.
- The backend is not configured separately: the task store uses the **same backend type as the
  session store** (`session.type`). DynamoDB sessions → a dedicated DynamoDB task table; Redis or
  Valkey sessions → the **same cluster** the sessions use, with a separate table/keyspace — no new
  infrastructure is provisioned. Any other session type (e.g. in-memory) fails the startup check,
  since scheduling needs a durable store shared by all replicas.
- The task table is always a **new table/keyspace** dedicated to tasks — it is never a partition of
  an existing session or response-store table, even when the underlying cluster is shared.

### Schedule definition and timing

- A schedule is one of: a cron expression, a fixed rate, or a one-time instant.
- Minimum granularity is whatever the timer provider supports; schedules finer than that are
  rejected at create/update time (a `Scheduler` obligation — see above).
- One-time schedules are removed automatically once they have fired: the timer registration removes
  itself (a `Scheduler` obligation), and the task row is deleted by the output consumer after it
  records the run's outcome. The row is not kept around afterward (no indefinite retention, no
  TTL) — for a one-time task, "did this run, when, and did it succeed" is answered from
  logs/traces, not the task row.

### Message payload

- The message registered with the timer is a standard input-queue body (`prompt`, `agent`), plus
  scheduling metadata: `task_id`, `scheduled_time` (stamped by the timer at fire time), the
  conversation mode and rotation period, and the owning identity. The payload carries the session
  *policy*, not a final `session_id` — the schedule payload is a static template, so the agent
  runner derives the session id at consume time (see *Conversation / session handling*).
- Delivery attributes: fires are grouped and serialized by `task_id` — stable across fires
  regardless of conversation mode — and deduplicated by `<task_id>:<scheduled_time>`.

### Reliability and duplication prevention

- Exactly one **enqueue** per scheduled time, with any number of replicas running. Guaranteed by the
  timer firing once, backed by the `Scheduler`'s at-most-once delivery obligation.
- After enqueue, execution follows the existing at-least-once queue semantics: a consumer crash
  after the agent's side effects but before message deletion redelivers and re-executes the run.
  Agents whose actions must not repeat need to be idempotent, as with any queued request today.
- No leader server and no distributed lock is required — by construction, not by configuration.
- Failures after the message reaches the queue are handled by the existing queue retry policy and
  DLQ. A run that exhausts retries is recorded as `FAILED` on the task row.

### Missed runs

- If the timer cannot deliver (queue throttling, transient failure) it retries per its own retry
  policy and, on exhaustion, delivers to the timer's DLQ.
- Agent Kernel does not reconstruct or replay missed fires from the task table. **This is a
  deliberate reduction from the original requirement** (listed for sign-off in *Open Questions*) for configurable catch-up of missed runs: with
  timing delegated to infrastructure, there is no Agent Kernel process guaranteed to be awake to
  perform catch-up, and reconstructing fires would reintroduce the polling and locking this design
  removes.
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
  outcome of a run (success or failure) on the output queue, so it is the natural place to update
  `last_run_at` / `last_run_status` / `last_error` on the task row.

### Ownership and identity

- Tasks defined in configuration files run under a distinct system identity.
- Tasks created through the API are bound to the identity of their creator.
- Unforgeability is a precondition, not an aspiration: exposing the task API surface (REST routes
  and agent-callable tools) requires a configured identity resolver — the `Authoriser` on the
  FastAPI surface, the API Gateway authorizer on serverless. Enabling scheduling without one fails
  at startup, like the queue-mode check. Config-defined tasks run under the system identity and
  need no resolver.
- The identity is written to the task row by the server and stamped into the timer's message payload;
  it is never read from client input, so it cannot be forged or overridden.
- Scheduled task activity is kept out of the user's regular conversation history: sessions created
  for scheduled runs skip `ConversationThreadManager` thread auto-creation, so scheduled runs never
  appear in the owner's thread listings. This is fixed behaviour, not configurable.

### How tasks get run

- One execution path only: the timer places the message on the input queue and the existing agent
  runner executes it. **This replaces the original two-path requirement** (listed for sign-off in
  *Open Questions*) — the "run the agent
  directly inside the scheduling process" option is dropped, because in queue mode there is no
  long-lived scheduler process to run it in, and the direct path would bypass the retry and DLQ
  behaviour the queue path already provides.
- A scheduled run goes through the same guardrails, hooks, logging and monitoring as any other agent
  run. No shortcuts.

### Management and administration

- Tasks are defined two ways: through configuration files (administrators / deployment) or through
  an API (end users / applications).
- Config-defined tasks reconcile against the config file as an explicit **deploy-time step**, not
  an "at startup" hook — a scaled-to-zero Lambda deployment has no startup moment. On ECS it runs
  once during container startup; on serverless it is a post-deploy invocation (Terraform-triggered
  or a deploy-script step). Added entries are upserted, removed entries are deleted from both the
  task table and the timer.
- Reconciliation is idempotent (keyed by `task_id`) and cheap to repeat: a hash of the config task
  set is stored on a sentinel row in the task table, so a run no-ops when nothing changed and
  concurrent ECS replicas need no coordination.
- Config-defined tasks are not editable or deletable through the API; such attempts are rejected with
  a clear error.
- All API-based task management goes through the existing authorization system.

### REST API surface

All task-management logic lives in a shared **`TaskService`** (analogous to `ChatService`):
request validation, identity stamping, and the `Scheduler.upsert / delete / get / list` calls. The
route layers are thin, and there is one per deployment style — the supported queue-mode targets do
not serve FastAPI's `AgentRESTRequestHandler`:

- **ECS containerized** — a new `TaskRESTRequestHandler` (a `RESTRequestHandler`, mirroring how
  `ThreadRESTRequestHandler` sits beside the chat handler in `api/thread.py`), mounted by
  `ECSIOHandler` alongside `ECSQueueRequestHandler` when scheduling is enabled.
- **Scalable serverless** — the same routes registered on the Lambda router via the existing
  `Lambda.register(route, method)` custom-route mechanism (`aklambda.py`), delegating to the same
  `TaskService`.

The routes — create and update are a single operation, because `Scheduler.upsert` is
create-or-replace:

- **Create / update — `PUT /api/v1/task/{task_id}`**. Body is the normal chat/run body (`prompt`,
  `agent`, ...) extended with a `schedule` object (cron / rate / one-time `at`, plus conversation
  mode and rotation period). The path param is the task's `task_id` (also its idempotency key for
  config-defined tasks). Calls `Scheduler.upsert(...)`: the task row is written and the timer
  registration is created or replaced, so the next fire reflects the new schedule/prompt/agent. The caller's resolved identity is stamped as the task owner and cannot be
  supplied in the body. Rejected with 409 if the task is config-defined.
- **Read — `GET /api/v1/task`** (list, scoped to the caller's own tasks) and
  **`GET /api/v1/task/{task_id}`** (single task definition + last-run status).
- **Delete — `DELETE /api/v1/task/{task_id}`**. Calls `Scheduler.delete(...)`: the timer
  registration is removed first, then the task row, so a fire can never race a deletion and land
  on a queue with no corresponding task. Rejected with 409 if the task is config-defined.
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

## AWS Feasibility

How the AWS provider satisfies the `Scheduler` contract — only the claims a design reviewer needs
to judge the reliability guarantees. Field-level configuration (exact target ARNs, IAM policies,
Terraform changes) is deferred to `spec.md`. None of this is visible to the API, config,
`TaskService` or reconciliation layers.

- Schedules are registered with **EventBridge Scheduler**, delivering directly to the input SQS
  queue via its universal target. The universal target is required, not a preference: it is the
  only SQS target that can set both `MessageGroupId` and `MessageDeduplicationId`, and it stamps
  the scheduled time into the payload at fire time.
- The delivery attributes map onto SQS FIFO: `MessageGroupId` = `task_id` (serialization);
  `MessageDeduplicationId` = `<task_id>:<scheduled_time>` (at-most-once per scheduled time). No
  queue changes: the dedup id is set explicitly, so content-based deduplication stays off and
  existing chat traffic is untouched.
- Minimum granularity: 1 minute, for both cron and rate expressions.
- One-time schedules delete their own registration after firing (`ActionAfterCompletion=DELETE`) —
  atomically, with no race against a concurrent delete and no scheduler permissions needed
  downstream.
- Accepted edge case: SQS's FIFO deduplication window is fixed at 5 minutes, so a timer-side retry
  delivered later than that would not be deduplicated. Schedules cap event age at ~300 s so a
  retried delivery cannot outlive the dedup window.
- Per supported target, the `ak-deployment` modules gain: a task table following the session store
  type (dedicated DynamoDB table, or a separate table/keyspace on the existing Redis/Valkey
  cluster — no new infrastructure); an EventBridge Scheduler schedule group per deployment (for
  namespacing and destroy-time cleanup); an execution role allowing the timer to send to the input
  queue; and the IAM grants for task-table and scheduler access (enumerated in `spec.md`).

## Open Questions

Two deliberate reductions from the original requirements in issue #554 need explicit sign-off
rather than implicit approval with the rest of the design:

1. **Configurable catch-up of missed runs is dropped** (see *Missed runs*). The original
   requirement asked for configurable catch-up; this design delegates missed-fire recovery entirely
   to the timer's retry policy and DLQ. Proposed resolution: accept the reduction — with timing
   delegated to infrastructure there is no Agent Kernel process guaranteed to be awake to perform
   catch-up, recurring tasks self-heal at the next fire, and reconstructing fires would reintroduce
   the polling and locking this design removes.
2. **The direct in-process execution path is dropped** (see *How tasks get run*). The original
   requirement described two execution paths — via the queue, or directly inside the scheduling
   process; this design ships only the queue path. Proposed resolution: accept the reduction — in
   queue mode there is no long-lived scheduler process to host a direct run, and the direct path
   would bypass the retry/DLQ behaviour the queue path provides.
