# #554: Agent Kernel Task Scheduling

Add time-triggered agent invocation to Agent Kernel. A scheduled task is stored in a task table and
registered with an external **timer** service; when the timer fires it places a normal agent message
onto the existing **input queue**, and the existing agent runner consumes and executes it exactly as
it would any other queued request. Scheduling is therefore only available in **queue-mode scalable
AWS deployments** — scalable serverless (Lambda) and scalable containerized (ECS). **AWS is the only
cloud implemented in this version**; Azure and GCP are not covered.

## Problem Description

Agent Kernel cannot invoke an agent on a schedule. Every existing entry point — REST
(`/api/v1/chat`), queue-based ingestion, A2A, MCP — requires an external caller to initiate the
request. Nothing in the system can trigger agent work on its own.

Some agent work is triggered by time, not by a user or an upstream system: a recurring report, a
periodic cleanup, a scheduled check. That work needs to run on a cron-like schedule and deliver a
prompt to the appropriate agent with no human or calling system in the loop.

## Design Choices

Four decisions shape the rest of this document:

1. **AWS only.** The only implementation delivered is the AWS one: a pluggable task table
   (DynamoDB, Redis or Valkey — see *The `TaskStore` abstraction*), EventBridge Scheduler for the
   timer, SQS for delivery. Azure and GCP deployments do not get task scheduling in this version —
   the `Scheduler` ABC exists so those can be added later, not because a second provider is being
   written now.
2. **Queue mode only.** Within AWS, scheduling is delivered exclusively for scalable deployments that
   run with `queue_mode = true`: scalable serverless (Lambda) and scalable containerized (ECS).
   Non-queue deployments (single-Lambda serverless, plain REST container, local runs) do not get
   task scheduling in this version.
3. **Timing is delegated to an external timer, not to an in-process poller.** A `Scheduler` ABC owns
   the task table and the registration of schedules with a platform timer. On AWS the timer is
   **EventBridge Scheduler**.
4. **The timer's target is the input queue, not the agent.** When a schedule fires, the timer
   delivers the pre-built message to the input queue (SQS). From that point the request is
   indistinguishable from any other queued request and flows through the existing agent runner,
   guardrails, hooks, tracing, retries and DLQ. After execution, the task table row is updated to
   record that the run completed.

### Flow

```
create/update task
  → Scheduler.upsert()
      → write task row to task table (DynamoDB)
      → register schedule with timer (EventBridge Scheduler):
          recurring (cron/rate) or one-time (at), carrying the message payload
timer fires
  → timer puts message on input queue (SQS)
  → agent runner consumes → runs the agent (normal path)
  → task table updated: last_run_at, last_run_status = COMPLETED / FAILED
```

### Why this shape

- **No leader election, no distributed lock, no dedup window.** No replica polls for due work, so
  there is nothing for replicas to race over. The timer fires once per scheduled time. The FIFO
  message deduplication id (`<task_id>:<scheduled_time>`) remains as a cheap safety net against
  timer-side at-least-once delivery.
- **No new execution path.** The scheduled run reuses the input queue, so it inherits the retry
  handling, DLQ, concurrency controls, guardrails, hooks and observability that already apply to
  queued requests. Nothing is bypassed.
- **The scheduler process does not need to be running when a task is due.** The timer is
  infrastructure; a scaled-to-zero Lambda deployment still fires.

## Scope

- Define tasks that run on a schedule — recurring (cron or rate) or one-time (at a given instant).
- Have those tasks automatically place a prompt for an AI agent on the input queue when due.
- Manage scheduled tasks (create, view, update, delete) through both configuration files and an API.
- Run a task exactly once per scheduled time, including when the deployment is running many
  replicas.
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
- Enabling scheduling on a deployment that is not in queue mode must fail at startup with a clear
  error, not degrade silently.

### The `Scheduler` abstraction

- A `Scheduler` ABC defines the provider-agnostic contract, and is implemented in code as a real
  abstraction (not a thin wrapper) so a non-AWS provider can be added later without touching the API,
  config or reconciliation layers. Only the AWS implementation is built and shipped in this version:
  - `upsert(task)` — persist the task row and register (or re-register) its schedule with the timer,
    carrying the message body the timer should deliver.
  - `delete(task_id)` — remove the timer registration and the task row.
  - `get(task_id)` / `list(...)` — read task definitions and their last-run state.
  - `mark_run_started(task_id, scheduled_time)` / `mark_run_completed(task_id, scheduled_time,
    status)` — record run outcome on the task row.
- The AWS `Scheduler` implementation registers schedules with **EventBridge Scheduler**, targeting
  the deployment's input SQS queue.
- The ABC + factory follow the existing pluggable-backend pattern (`Trace`, `ResponseStore`,
  `QueueHandler`), so a non-AWS timer can be added later without touching the API or config layer.

### The `TaskStore` abstraction

- The task table itself is a separate, pluggable concern from the timer, mirroring the existing
  `ResponseStore` ABC (`deployment/common/response_store.py`) and its backends
  (`deployment/aws/core/response_store/{dynamodb,redis,valkey}.py`): a `TaskStore` ABC with
  DynamoDB, Redis and Valkey implementations.
- Backend selection follows the same reuse-first rule as `ResponseStore`: if the deployment already
  runs a Redis/Valkey cluster (e.g. for session memory or the response store), the task store reuses
  it; otherwise a new DynamoDB table is created. No new infrastructure is provisioned when a
  compatible cluster already exists.
- The task table is always a **new table/keyspace** dedicated to tasks — it is never a partition of
  an existing session or response-store table, even when the underlying cluster is shared.

### Schedule definition and timing

- A schedule is one of: a cron expression, a fixed rate, or a one-time instant.
- Minimum granularity is whatever the timer provider supports (EventBridge Scheduler: 1 minute).
  Schedules finer than that must be rejected at create/update time, not silently rounded.
- One-time schedules are removed automatically once they have fired: after the output consumer
  records the run's outcome, both the EventBridge Scheduler registration and the task row are
  deleted. The row is not kept around afterward (no indefinite retention, no TTL) — for a one-time
  task, "did this run, when, and did it succeed" is answered from logs/traces, not the task row.

### Message payload

- The message registered with the timer is a standard input-queue body (`prompt`, `agent`,
  `session_id`), plus scheduling metadata: `task_id`, `scheduled_time`, and the owning identity.
- FIFO attributes: `message_group_id` = the task's `session_id`; `message_deduplication_id` =
  `<task_id>:<scheduled_time>`.

### Reliability and duplication prevention

- Exactly one execution per scheduled time, with any number of replicas running. Guaranteed by the
  timer firing once, backed by the FIFO deduplication id.
- No leader server and no distributed lock is required — by construction, not by configuration.
- Failures after the message reaches the queue are handled by the existing queue retry policy and
  DLQ. A run that exhausts retries is recorded as `FAILED` on the task row.

### Missed runs

- If the timer cannot deliver (queue throttling, transient failure) it retries per its own retry
  policy and, on exhaustion, delivers to the timer's DLQ.
- Agent Kernel does not reconstruct or replay missed fires from the task table. **This is a
  deliberate reduction from the original requirement** for configurable catch-up of missed runs: with
  timing delegated to infrastructure, there is no Agent Kernel process guaranteed to be awake to
  perform catch-up, and reconstructing fires would reintroduce the polling and locking this design
  removes.
- A fire that arrives outside an acceptable staleness window is logged as a warning and still
  executed; operators can detect gaps from the task row's `last_run_at` and from timer-side metrics.

### Conversation / session handling

- Each task chooses whether every run starts a fresh conversation or continues a long-running one:
  - **Per-run session** — a new `session_id` is derived per fire (`<task_id>:<scheduled_time>`).
  - **Continuous session** — a stable `session_id` is reused across runs.
- Continuous sessions support a rotation period, after which a fresh `session_id` is derived, so a
  conversation does not grow without bound.

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
- The identity is written to the task row by the server and stamped into the timer's message payload;
  it is never read from client input, so it cannot be forged or overridden.
- By default, scheduled task activity is kept out of the user's regular activity/conversation history.

### How tasks get run

- One execution path only: the timer places the message on the input queue and the existing agent
  runner executes it. **This replaces the original two-path requirement** — the "run the agent
  directly inside the scheduling process" option is dropped, because in queue mode there is no
  long-lived scheduler process to run it in, and the direct path would bypass the retry and DLQ
  behaviour the queue path already provides.
- A scheduled run goes through the same guardrails, hooks, logging and monitoring as any other agent
  run. No shortcuts.

### Management and administration

- Tasks are defined two ways: through configuration files (administrators / deployment) or through
  an API (end users / applications).
- Config-defined tasks reconcile against the config file at startup: added entries are upserted,
  removed entries are deleted from both the task table and the timer. Reconciliation is idempotent
  (keyed by `task_id`), so it is safe for several replicas to perform it concurrently.
- Config-defined tasks are not editable or deletable through the API; such attempts are rejected with
  a clear error.
- All API-based task management goes through the existing authorization system.

### REST API surface

Task creation reuses the chat endpoint's request shape; reading, updating and deleting a task are a
separate small resource API, mirroring how `ThreadRESTRequestHandler` sits next to
`AgentRESTRequestHandler` (`api/thread.py`, `api/handler.py`):

- **Create — `POST /api/v1/chat/{task_id}`**, on `AgentRESTRequestHandler`. Body is the normal
  chat/run body (`prompt`, `agent`, `session_id`, ...) extended with a `schedule` object (cron / rate
  / one-time `at`, plus conversation mode and rotation period). The path param is the task's
  `task_id` (also its idempotency key for config-defined tasks). Instead of running the agent
  immediately, this calls `Scheduler.upsert(...)`, creating the task row and registering it with
  EventBridge Scheduler.
  The caller's identity (resolved the same way as for `/api/v1/chat`) is stamped as the task owner and
  cannot be supplied in the body.
- **Read — `GET /api/v1/task`** (list, scoped to the caller's own tasks) and
  **`GET /api/v1/task/{task_id}`** (single task definition + last-run status), on a new
  `TaskRESTRequestHandler`.
- **Update — `PUT /api/v1/task/{task_id}`**. Body is the same shape as create. Calls
  `Scheduler.upsert(...)` again: the task row is overwritten and the EventBridge Scheduler
  registration is replaced (old trigger removed, new one registered) so the next fire reflects the
  new schedule/prompt/agent. Rejected with 409 if the task is config-defined.
- **Delete — `DELETE /api/v1/task/{task_id}`**. Calls `Scheduler.delete(...)`: the EventBridge
  Scheduler registration is removed first, then the task row, so a fire can never race a deletion and
  land on a queue with no corresponding task. Rejected with 409 if the task is config-defined.
- All four routes go through the same authorization/Authoriser path as the thread endpoints
  (`ThreadRESTRequestHandler._resolve_user`), and update/delete additionally check task ownership.

### Agent-callable tool

- A `SystemTool` set (mirroring the sandbox capability's `get_sandbox_tools()` /
  `SystemToolFactory` pattern in `sandbox/tools.py`) exposes task management to the agent itself, so a
  conversation can create, update, delete or list its own scheduled tasks without a separate API call:
  `schedule_task`, `update_scheduled_task`, `delete_scheduled_task`, `list_scheduled_tasks`.
- These tools call the same `Scheduler` methods as the REST layer (`upsert` / `delete` / `get` /
  `list`) — no parallel code path. A task created via the tool is owned by the identity of the
  session that invoked it, exactly like an API-created task; the tool cannot set an arbitrary owner.
- Registration is gated behind the same applicability rule as the rest of scheduling (AWS, queue
  mode): the tool set is only added to an agent's toolset when scheduling is enabled for the
  deployment.
