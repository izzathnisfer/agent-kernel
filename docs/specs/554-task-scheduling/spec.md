# #554: Agent Kernel Scheduled Tasks — Implementation Spec

Detailed design for the scheduled-task capability described in [design.md](design.md), which remains
the requirements source: every must/should statement there is traced into a section here. The
change adds one new capability package (`agentkernel/scheduler/`), one optional field pair on the
existing chat request model, one optional echo on the shared response builder, a schedule route
layer per supported deployment target, and outcome recording in the two output consumers. The
one-sentence design idea: **a scheduled task is a stored row plus an EventBridge Scheduler
registration whose target is the existing input queue, so the fire is an ordinary agent message and
the agent runner needs no scheduling awareness.**

Applicability is unchanged from the design: AWS, queue mode, the two scalable examples
(`examples/aws-serverless/scalable-openai`, `examples/aws-containerized/openai-dynamodb-scalable`).

---

## Design

### Package placement

The capability lives in a new top-level package, `ak-py/src/agentkernel/scheduler/`, mirroring
`agentkernel/sandbox/` rather than `deployment/`. This is forced by two existing constraints:

1. `core/tool.py`'s `SystemToolFactory.get_all()` (`core/tool.py:178-200`) must import the
   agent-callable tools, and **core never imports from `deployment/`**. `SystemToolFactory` already
   reaches sideways into `agentkernel.sandbox.tools` (`core/tool.py:196`) — the same shape applies
   here.
2. The chat create path lives in `deployment/` on both targets, so the service must be importable
   from `deployment/` too. A top-level capability package is importable from both.

The AWS provider therefore sits inside the capability package (the `sandbox/providers/ec2_ssm.py`
precedent — AWS-specific providers live with their capability, not under `deployment/`), and talks
to boto3 directly rather than importing `deployment/aws/core/sqs_handler.py`.

```
ak-py/src/agentkernel/scheduler/
├── __init__.py          # public exports: Scheduler, ScheduledTask, ScheduleSpec, ScheduledTaskService, SchedulerFactory
├── base.py              # Scheduler ABC
├── model.py             # ScheduledTask, ScheduledTaskPage, RunStatus, TaskStatus; re-exports ScheduleSpec/ScheduleMode
├── service.py           # ScheduledTaskService
├── factory.py           # SchedulerFactory: validate_config() + build()
├── errors.py            # SchedulerError hierarchy
├── tools.py             # get_scheduler_tools() → list[SystemTool]
├── testing.py           # SchedulerContract — reusable provider contract suite
├── providers/
│   ├── __init__.py
│   └── aws.py           # AWSScheduler (EventBridge Scheduler + SQS target)
└── store/
    ├── __init__.py
    ├── base.py          # ScheduledTaskStore ABC + ScheduledTaskStoreBuilder
    ├── dynamodb.py      # DynamoDBScheduledTaskStore
    ├── redis.py         # RedisScheduledTaskStore
    └── valkey.py        # ValkeyScheduledTaskStore
```

`agentkernel/api/schedule.py` holds the FastAPI route layer (mirroring `api/thread.py`), keeping the
API surface with the other REST handlers.

Governing rules for the package, in the spirit of the shared-driver rules
(`core/util/driver/`):

1. **Only the `Scheduler` touches the `ScheduledTaskStore`.** The service, the route layers, the
   tools and the output consumers hold a `Scheduler` and nothing else. No caller imports a store
   class or `ScheduledTaskStoreBuilder`.
2. **Stores never read `AKConfig`.** All connection and layout parameters are explicit constructor
   arguments; config reading lives in `ScheduledTaskStoreBuilder` (same rule the shared drivers
   already follow).
3. **The `Scheduler` owns all outcome-write policy.** The four guards, the field updates and the
   one-time `COMPLETED` transition happen inside `mark_run_completed`, never in a consumer.
4. **Nothing above the `Scheduler` ABC is AWS-aware.** `ScheduledTaskService`, the route layers and
   the tools never mention EventBridge, SQS, or boto3.

### Models — `scheduler/model.py`

```python
from ..core.model import ScheduleMode, ScheduleSpec   # re-exported; defined in core — see below

class TaskStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"    # a one-time task that has fired

class RunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class ScheduledTask(BaseModel):
    """The stored row. Written only by the Scheduler."""
    scheduled_task_id: str
    scheduled_task_version: str        # incarnation token (uuid4 hex), server-generated
    owner_id: str                      # authenticated identity; never read from client input
    schedule: ScheduleSpec
    message: dict                      # the agent message template the timer delivers
    status: TaskStatus = TaskStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[RunStatus] = None
    last_run_scheduled_time: Optional[datetime] = None
    last_error: Optional[str] = None
    deleted: bool = False
    deleted_at: Optional[datetime] = None
```

`message` holds `prompt`, `agent`, `user_id` (the owner) and the resolved `session_id` — everything
the runner needs. It is stored as the template (with the timer's substitution placeholders in place
for `per_run`), so `GET` shows exactly what will be delivered.

**`ScheduleSpec`, `ScheduleMode` and `ScheduledRunMetadata` deliberately live in `core/model.py`, not
here**, and are re-exported from `scheduler/model.py` for callers who import the capability package.
All three are fields (or field types) on `BaseRunRequest`, and a Pydantic field annotated
`Optional[ScheduleSpec]` needs the real class at class-construction time — no lazy or `TYPE_CHECKING`
import can satisfy that. Defining them in `core/` keeps the dependency pointing one way: `core/` has
**zero** imports from `scheduler/`, and `scheduler/model.py` imports from `core/model.py`.

This is exactly the split the sandbox capability already uses — its *configuration shape*
(`_SandboxConfig`) lives in `core/config.py:526` while its *behaviour* is imported lazily inside a
function (`core/tool.py:194-196`). Same rule here: the capability's data shapes live in core, its
behaviour does not. See *Core model changes*.

### `Scheduler` ABC — `scheduler/base.py`

```python
class Scheduler(ABC):
    """Provider-agnostic contract: owns the scheduled-task table and the timer registrations.

    The store is a private collaborator held by the implementation; no caller resolves it.
    """

    @abstractmethod
    def upsert(self, task: ScheduledTask) -> ScheduledTask:
        """Persist the row and register (or replace) its timer registration.
        Writes the row first, then registers; a registration failure rolls the row back
        to its prior state (see Error handling).
        :raises ScheduleValidationError: expression invalid or finer than provider granularity
        """

    @abstractmethod
    def delete(self, scheduled_task_id: str) -> None:
        """Remove the timer registration, then soft-delete the row. Idempotent."""

    @abstractmethod
    def get(self, scheduled_task_id: str, *, include_deleted: bool = False) -> ScheduledTask | None:
        """Read one row. Soft-deleted rows are hidden unless include_deleted."""

    @abstractmethod
    def list(self, owner_id: str, *, limit: int | None = None, cursor: str | None = None) -> ScheduledTaskPage:
        """List an owner's live rows. Soft-deleted rows are never returned."""

    @abstractmethod
    def mark_run_completed(
        self,
        scheduled_task_id: str,
        scheduled_task_version: str,
        scheduled_time: datetime,
        status: RunStatus,
        last_error: str | None = None,
    ) -> bool:
        """Record a terminal run outcome. Returns False (a logged no-op) when any of the
        four guards rejects the write; raises only on store/infrastructure failure.
        There is no mark_run_started: only terminal outcomes are recorded."""
```

There is no `Scheduler` method a consumer can use to reach the store; `ScheduledTaskPage` is a small
`{items: list[ScheduledTask], next_cursor: str | None}` model in `scheduler/model.py`.

Every implementation must satisfy the design's five obligations. How the AWS provider does so is in
*AWS provider* below; the obligations are restated as a reusable contract test suite
(`SchedulerContract`, `scheduler/testing.py`) so a future provider inherits the checks — the
`SandboxProviderContract` pattern (`sandbox/testing.py`).

### `ScheduledTaskStore` ABC and backends — `scheduler/store/`

```python
class ScheduledTaskStore(ABC):
    """Private collaborator of the Scheduler. Never reads AKConfig."""

    @abstractmethod
    def put(self, task: ScheduledTask) -> None:
        """Whole-row write. Used for creation only."""
    @abstractmethod
    def update_fields(self, scheduled_task_id: str, fields: dict[str, Any], *,
                      expected_version: str | None = None) -> bool:
        """Write a subset of attributes, leaving every other attribute untouched.
        Returns False when expected_version does not match the stored
        scheduled_task_version. See Concurrency contract."""
    @abstractmethod
    def get(self, scheduled_task_id: str) -> ScheduledTask | None:
        """Returns the row including soft-deleted ones; filtering is the Scheduler's job."""
    @abstractmethod
    def list_by_owner(self, owner_id: str, *, limit: int | None, cursor: str | None) -> ScheduledTaskPage:
        """Live rows only. Each backend is responsible for excluding soft-deleted rows,
        and the returned page size reflects live rows — a page is never short because
        tombstones were filtered out of it."""
    @abstractmethod
    def soft_delete(self, scheduled_task_id: str, deleted_at: datetime, ttl_seconds: int) -> None: ...
```

One concept, one name across all three backends — no per-backend method renames.

`put` and `update_fields` are both required, and the split is not cosmetic. `put` is a whole-row
write, which cannot express the disjoint-attribute-set property the *Concurrency contract* depends
on: a `PUT` from the management API and an outcome write from a consumer must be able to interleave
without either clobbering the other's fields. `update_fields` is what makes that expressible, and its
`expected_version` parameter folds guard 3 into the write itself rather than leaving a
check-then-act window between the `get` and the write.

**`ScheduledTaskStoreBuilder.build()`** resolves the backend from `session.type`, not from a
scheduler-specific type field, exactly as the design requires. It follows the house factory shape
(`core/builder.py:87-131`, `core/util/factory.py`): `if/elif` over built-in short names with
`require_extra` around each lazy import, and `AKConfigError` on anything else.

| `session.type` | Store | Backing infrastructure |
|---|---|---|
| `dynamodb` | `DynamoDBScheduledTaskStore` | A **dedicated** DynamoDB table, `scheduler.dynamodb.table_name` |
| `redis` | `RedisScheduledTaskStore` | The **session cluster** (`session.redis.url`), separate keyspace `scheduler.redis.prefix` |
| `valkey` | `ValkeyScheduledTaskStore` | The **session cluster** (`session.valkey.url`), separate keyspace `scheduler.valkey.prefix` |
| anything else (incl. `in_memory`, `cosmosdb`, `firestore`, a dotted path) | — | `AKConfigError` at initialization |

The table is never a partition of an existing session or response-store table: on DynamoDB it is a
distinct table name; on Redis/Valkey a distinct key prefix on the shared cluster. No new
infrastructure is provisioned for the Redis/Valkey backends.

Note the deliberate asymmetry with `SessionStoreBuilder`: that builder has a dotted-path
bring-your-own branch, this one does not, because it resolves off `session.type` rather than its own
`type` field.

**DynamoDB layout.** Partition key `scheduled_task_id` (S), no sort key. A global secondary index
`owner_id-index` on `owner_id` (S) with sort key `created_at` (S) serves `list_by_owner` and its
cursor. TTL attribute is `expiry_time` — the name `DynamoDBDriver.put` already uses
(`core/util/driver/dynamodb.py:95`).

**The GSI is sparse: `owner_id` is written only while the row is live, and `soft_delete` `REMOVE`s
it.** This is how `list_by_owner`'s live-rows-only contract is met on DynamoDB. The alternative —
keeping `owner_id` on tombstones and adding `FilterExpression=Attr("deleted").ne(True)` — filters
*after* the read, so a page of `limit` items can come back with fewer (or zero) live rows while
`LastEvaluatedKey` is still set, forcing the caller into a paging loop over invisible rows. Removing
the attribute drops tombstones out of the index entirely: no filter expression, no short pages, and
no read capacity spent on rows nobody can see. The row itself is untouched and stays `get`-able by
primary key throughout the grace window, which is what the guards need.

The driver is constructed with **`ttl=0`**, deliberately: `DynamoDBDriver.put` stamps `expiry_time`
on *every* put when `ttl > 0` (`dynamodb.py:93-95`), which would expire live rows. `expiry_time` is
written explicitly, and only by `soft_delete`. Pagination and the GSI query use the driver's public
`table` handle, which the driver contract exposes for exactly this case (`dynamodb.py:50-61`).

**Redis / Valkey layout.**

| Key | Type | Contents |
|---|---|---|
| `<prefix><scheduled_task_id>` | string | the JSON-serialized `ScheduledTask` |
| `<prefix>owner:<owner_id>` | set | the owner's `scheduled_task_id`s |

The driver is constructed with **`ttl=0`** for the same reason: `_RedisLikeDriver.set()` applies
`ex=ttl` on every write when configured (`redis_like.py:123-124`), and `expire(key)` can only apply
the *configured* TTL (`redis_like.py:282-291`). The derived soft-delete TTL is per-call, so
`soft_delete` uses the driver's public native handle: `driver.client.expire(name=key,
time=ttl_seconds)`.

`list_by_owner` reads the owner set with `smembers`, loads each row, **skips rows whose `deleted` is
true** (the live-rows-only contract), and **prunes** ids whose row key no longer exists (`srem` via
the native handle) — a set member does not disappear when the row key expires, so without pruning the
index grows without bound after TTL expiry. Skipping and pruning are deliberately different actions:
`soft_delete` leaves the id in the owner set, because the row is still readable during the grace
window and must stay `get`-able, so a tombstone is filtered from the listing but kept in the index;
pruning removes the id only once the row is actually gone. Redis cannot use the DynamoDB sparse-index
trick — the owner set is the only index there is — so the filter is unavoidable here; the cost is one
skipped row per tombstone within a single owner's grace window.

### AWS provider — `scheduler/providers/aws.py`

`AWSScheduler(Scheduler)` holds three collaborators, all constructed eagerly in `__init__` (no lazy
init — see *Concurrency contract*):

- `boto3.client("scheduler")` — EventBridge Scheduler
- `boto3.client("sqs")` — used once, at construction, for `GetQueueAttributes`
- the `ScheduledTaskStore` from `ScheduledTaskStoreBuilder.build()`

**Registration.** `upsert` calls `create_schedule` / `update_schedule` in the deployment's schedule
group (`scheduler.group_name`) with a **universal target** (`Arn` = `arn:aws:scheduler:::aws-sdk:sqs:sendMessage`,
`RoleArn` = `scheduler.target_role_arn`). The universal target is required, not preferred: it is
the only SQS target that can set both `MessageGroupId` and `MessageDeduplicationId`, and it performs
context-variable substitution into the payload.

`Target.Input` is the JSON template:

```json
{
  "QueueUrl": "<input queue url>",
  "MessageBody": "{\"prompt\": \"...\", \"agent\": \"...\", \"user_id\": \"<owner_id>\",
                   \"session_id\": \"schedule:<id>:<aws.scheduler.scheduled-time>\",
                   \"scheduled_run\": {\"scheduled_task_id\": \"<id>\",
                                       \"scheduled_task_version\": \"<version>\",
                                       \"scheduled_time\": \"<aws.scheduler.scheduled-time>\",
                                       \"run_id\": \"<aws.scheduler.execution-id>\"}}",
  "MessageGroupId": "<id>",
  "MessageDeduplicationId": "<id>:<aws.scheduler.scheduled-time>",
  "MessageAttributes": {"request_id": {"DataType": "String", "StringValue": "<aws.scheduler.execution-id>"}}
}
```

Angle-bracket tokens prefixed `aws.scheduler.` are EventBridge context variables substituted at fire
time; the rest are baked in at registration. `request_id` is set because **both** runners raise
`ValueError` without it (`containerized/akagentrunner.py:63-64`,
`serverless/akagentrunner.py:53-54`) and both output consumers require it
(`akoutputconsumer.py:158-160`, `akresponsehandler.py:54-56`). Using the execution id makes it unique
per fire.

For `ScheduleMode.CONTINUOUS` the `session_id` is the static `schedule:<id>` — no substitution.

**Delivery attributes.** `MessageGroupId = scheduled_task_id` serializes a scheduled task's fires;
`MessageDeduplicationId = <scheduled_task_id>:<scheduled_time>` gives at-most-once per scheduled
time. Both queues have `content_based_deduplication = false` (containerized
`containerized/modules/queues/main.tf:18,54`; serverless default in
`serverless/modules/queues/variables.tf:56`), so setting
the dedup id explicitly is required and existing chat traffic is untouched.

This departs from the existing convention that chat traffic groups by `session_id`
(`deployment/aws/core/sqs_handler.py:346,388` default the group id to the body's `session_id`).
Scheduled fires group by `scheduled_task_id` because in `per_run` mode the session id changes
between fires and would not serialize them. The two id spaces cannot collide: scheduled session ids
carry the reserved `schedule:` prefix.

**Granularity.** `ScheduleSpec` is validated before any AWS call: EventBridge Scheduler's minimum is
1 minute for both cron and rate. A `rate` finer than `1 minute`, or a cron with a seconds field,
raises `ScheduleValidationError` → 400. Schedules are never silently rounded.

**One-time schedules** are registered with `ActionAfterCompletion="DELETE"`, so the registration
removes itself atomically after firing — no cleanup process and no race with a concurrent delete.
The **row is kept**: `mark_run_completed` sets `status = COMPLETED` and `completed_at`, so `GET`
keeps answering "did this run, when, and did it succeed". Rows are removed only by an explicit
delete.

**Event age.** Every schedule sets `FlexibleTimeWindow={"Mode": "OFF"}` and a retry policy with
`MaximumEventAgeInSeconds = 300`, so a timer-side retry can never be delivered after SQS's fixed
5-minute FIFO deduplication window has closed.

**Soft-delete TTL derivation.** Computed once in `__init__`:

```python
attrs      = sqs.get_queue_attributes(QueueUrl=input_url,
                                      AttributeNames=["VisibilityTimeout", "RedrivePolicy"])["Attributes"]
visibility = int(attrs["VisibilityTimeout"])
redrive    = json.loads(attrs.get("RedrivePolicy", "{}"))
receives   = max(int(redrive.get("maxReceiveCount", 0)),
                 AKConfig.get().execution.queues.input.max_receive_count)
ttl        = max(visibility * receives + SAFETY_MARGIN, TTL_FLOOR)
```

`visibility_timeout` comes from the queue rather than `AKConfig` because it is a queue attribute set
by Terraform (`queue_config.input_queue_visibility_timeout`, default 60 in both targets —
`containerized/modules/queues/variables.tf:29`, `serverless/modules/queues/variables.tf:30`) and has
no `AKConfig` field at all. `SAFETY_MARGIN = 300` seconds and `TTL_FLOOR = 900` seconds
(`scheduler/factory.py` module constants, both documented in the config field descriptions).

`receives` is the **max of two sources, and both are needed.** The queue's own
`RedrivePolicy.maxReceiveCount` is the hard ceiling on redeliveries, but
`input_queue_create_dlq` defaults to `false` on both targets
(`containerized/modules/queues/variables.tf:32`), so on a default deployment there is **no redrive
policy and no `maxReceiveCount` attribute** — reading only the queue would silently collapse the TTL
to `SAFETY_MARGIN`. The `AKConfig` value covers that case.

The reverse case is why the queue value is read at all. The two are **not** independently configured:
both targets inject `AK_EXECUTION__QUEUES__INPUT__MAX_RECEIVE_COUNT = max(1, input_queue_max_receive_count - 1)`
into the agent runner (`containerized/modules/agent-runner/main.tf:9`,
`serverless/modules/agent-runner/main.tf:18,257`), so the runner stops one receive before the queue's
redrive fires. **That injection reaches the agent runner only** — `containerized/modules/rest-service/main.tf`
and the response handler get no such variable, so the components that actually derive this TTL fall
back to the `AKConfig` default of 3 (`core/config.py:323-325`) while the runner retries 4 (the
Terraform default is 5, `containerized/modules/queues/variables.tf:31`). Taking the max over both
sources makes the TTL an upper bound regardless of which components received the injection, and
removes the AKConfig-only dependency the previous derivation had.

(`examples/aws-containerized/openai-dynamodb-scalable/config.yaml:15` hardcodes
`max_receive_count: 4`, which happens to equal that example's injected `5 - 1`. That agreement is
coincidental and does not hold for a deployment that leaves the field at its default.)

If `GetQueueAttributes` fails, `__init__` raises — initialization fails loudly rather than falling
back to a guessed TTL. Correctness does not depend on the number: the `scheduled_task_version` guard
rejects a cross-incarnation outcome however short the TTL is. The derivation exists to make the
common case unsurprising.

**Outcome-write guards.** `mark_run_completed` loads the row and applies the four guards in order,
returning `False` and logging at WARNING when any rejects:

| # | Guard | Condition | Why |
|---|---|---|---|
| 1 | Row absent | `store.get(...) is None` | Deleted and TTL-expired; nothing to record |
| 2 | Soft-deleted | `task.deleted` | Deleted while this run was in flight |
| 3 | Incarnation mismatch | `task.scheduled_task_version != scheduled_task_version` | Outcome from a deleted-and-recreated task must not land on its successor's row |
| 4 | Stale scheduled time | `task.last_run_scheduled_time is not None and scheduled_time < task.last_run_scheduled_time` | A redelivered older outcome must not overwrite a newer one |

Guard 3 is what makes caller-chosen, reusable ids safe; existence checking alone is not sufficient.
Guard 4 is defence in depth behind FIFO ordering, not the primary mechanism (see *Reliability*).

On acceptance the write is a single
`store.update_fields(id, {...}, expected_version=scheduled_task_version)` setting `last_run_at`,
`last_run_status`, `last_error`, `last_run_scheduled_time`, and — when the schedule is one-time —
`status = COMPLETED` and `completed_at`. It is never a `put`: the definition fields must not be
rewritten from a row that was read before a concurrent `PUT` landed. `expected_version` re-checks
guard 3 at write time, closing the check-then-act window between the `get` above and the write.

### `ScheduledTaskService` — `scheduler/service.py`

The single place all scheduling logic lives. It is **not** a peer of `ChatService`: it never runs an
agent. Three callers — the chat create path, the `/api/v1/schedule` routes, and the agent-callable
tools — and no parallel code paths.

```python
class ScheduledTaskService:
    def __init__(self, scheduler: Scheduler): ...

    def create(self, *, spec: ScheduleSpec, prompt: str, agent: str | None, owner_id: str) -> CreateAck
    def update(self, scheduled_task_id: str, *, owner_id: str, spec: ScheduleSpec | None,
               prompt: str | None, agent: str | None) -> ScheduledTask
    def delete(self, scheduled_task_id: str, *, owner_id: str) -> None
    def get(self, scheduled_task_id: str, *, owner_id: str) -> ScheduledTask
    def list(self, *, owner_id: str, limit: int | None, cursor: str | None) -> ScheduledTaskPage
```

Responsibilities, in `create` order:

1. **Validate** the `ScheduleSpec` (exactly one expression; `at` in the future; granularity).
2. **Generate the id** when `spec.id` is absent: `f"schedule_{uuid4().hex}"`.
3. **Resolve the incarnation.** `scheduler.get(id, include_deleted=True)`:
   - no row → fresh `scheduled_task_version = uuid4().hex`
   - live row owned by `owner_id` → upsert, **retaining** the existing version so in-flight runs
     still record their outcomes
   - live row owned by someone else → `SchedulerPermissionError` → 403
   - soft-deleted row → `SchedulerConflictError` → 409 (deletion is terminal; the id frees up when
     the TTL expires)
4. **Resolve the session id** — `schedule:<id>` for `continuous`, the substitution template
   `schedule:<id>:<aws.scheduler.scheduled-time>` for `per_run`. The reserved `schedule:` prefix is
   applied here, once, because `scheduled_task_id` is caller-choosable and shares a namespace with
   user-supplied session ids.
5. **Stamp the owner** into the row and into `message["user_id"]`. `owner_id` is a parameter
   resolved by the caller from the authenticated identity; the service never reads it from a request
   body, so it cannot be forged or overridden.
6. **Call `scheduler.upsert(task)`** and return the acknowledgement.

`update` does not create: a missing live row raises `SchedulerNotFoundError` → 404. It retains
`scheduled_task_version`. A `PUT` on a one-time task whose `status` is `COMPLETED` re-arms it —
`status` back to `ACTIVE`, `completed_at` cleared, version retained. Updates affect future
executions only; an already-enqueued fire continues with the definition it was enqueued with.

`get`/`list`/`update`/`delete` all check ownership: `list` is scoped to `owner_id` in the store
query, and the others raise `SchedulerPermissionError` on a mismatch. Because every scheduled task
has an authenticated owner, there is no visible-but-not-editable category.

`CreateAck` is the payload from design.md's *Creation acknowledgement*:

```python
class CreateAck(BaseModel):
    status: Literal["SCHEDULED"] = "SCHEDULED"
    scheduled_task_id: str
    scheduled_task_version: str
    session_id: Optional[str] = None      # continuous mode only — see below
    next_run_at: Optional[datetime] = None  # only when derivable without evaluation — see below
    request_id: str                        # correlation id for the create call — see below
```

`session_id` is populated for `ScheduleMode.CONTINUOUS`, where the value is stable and meaningful,
and **omitted for `per_run`**, where no stable session exists (the id is only resolved at fire
time). Returning the unsubstituted template would be misleading. This is the third deviation from
design.md (see *Deviations from design.md* #3).

**`next_run_at` is documented best-effort, and populated only where it is knowable without a cron
evaluator.** No AWS API supplies it: neither `create_schedule` nor `get_schedule` returns a next
invocation time, and no cron library is present in any `pyproject.toml` extra. Rather than add a
dependency for a convenience field, the service derives it from the expression it already validated:

| Expression | `next_run_at` |
|---|---|
| `at` | the `at` value, normalized to UTC |
| `rate` | `created_at` + the interval (the `"<n> <unit>"` grammar is parsed by the same validator that enforces the 1-minute granularity) |
| `cron` | `None` |

The field's description states this explicitly: *"the next fire time when derivable from the
expression without evaluating it; `None` for `cron` expressions. Authoritative run history is
`last_run_at` on `GET /api/v1/schedule/{scheduled_task_id}`."* A `None` here means "not computed",
never "not scheduled".

**`request_id` is a correlation id for the create call, not a run id.** Creation enqueues nothing, so
there is no queue message and no runner-assigned id to return. `ScheduledTaskService.create` echoes
the caller's id when the surface supplies one and generates `uuid4()` otherwise, so the field is
always present:

| Surface | Source |
|---|---|
| ECS REST, serverless REST | generated — the same `str(uuid.uuid4())` shape `enqueue_and_wait` uses (`rest_handler.py:49`) |
| ECS WebSocket, serverless WebSocket | `BaseRequest.request_id` from the inbound frame (`core/model.py:226`) when present, generated otherwise |

Run-level correlation is a different id: `scheduled_run.run_id`, the EventBridge execution id, unique
per fire. The two never collide and are never interchangeable — `request_id` identifies the act of
creating the schedule, `run_id` identifies one firing of it.

### `SchedulerFactory` — `scheduler/factory.py`

Two entry points, deliberately split so config errors surface without touching AWS:

```python
class SchedulerFactory:
    @staticmethod
    def enabled() -> bool:
        """True when a scheduler block is present and enabled."""

    @staticmethod
    def validate_config() -> None:
        """Pure config validation, no network. Raises AKConfigError.
        Called by every scheduler-enabled component at initialization."""

    @staticmethod
    def build() -> Scheduler:
        """validate_config(), then construct the provider (which reads the queue
        visibility timeout and derives the soft-delete TTL). Raises on failure."""
```

`validate_config()` enforces, in order:

1. `scheduler.enabled` — when false, return immediately (nothing else is checked).
2. `execution.queues.input.url` **and** `execution.queues.output.url` are both set. Queue mode has no
   config flag of its own; both URLs being present is how it is detected. Either unset →
   `AKConfigError`. A non-queue deployment fails loudly, not silently.
3. `execution.queues.input.url` ends in `.fifo`. The duplication-prevention and serialization
   guarantees depend on a FIFO input queue; the containerized Terraform hardcodes
   `fifo_queue = true` (`containerized/modules/queues/main.tf:17`) but serverless exposes it as a
   toggle defaulting to true (`serverless/modules/queues/variables.tf:50`), and disabling it would
   silently break dedup. Since FIFO queue URLs always end in `.fifo`, the suffix check enforces it.
4. `session.type` is one of `dynamodb`, `redis`, `valkey` — scheduling needs a durable store shared
   by all replicas.
5. `scheduler.group_name` and `scheduler.target_role_arn` are non-empty. An **empty string counts as
   unset**, because the examples declare these as `""` placeholders that Terraform fills via
   `AK_SCHEDULER__*` (see *Terraform*); a deployment that enables scheduling in YAML without the
   Terraform wiring must fail here rather than at the first `create_schedule` call.
6. **The backend block matching `session.type` is present and non-empty**, and no *other* backend
   block is populated:

   | `session.type` | Required | Rejected |
   |---|---|---|
   | `dynamodb` | `scheduler.dynamodb.table_name` non-empty | `scheduler.redis` or `scheduler.valkey` populated |
   | `redis` | `scheduler.redis.prefix` non-empty | `scheduler.dynamodb` or `scheduler.valkey` populated |
   | `valkey` | `scheduler.valkey.prefix` non-empty | `scheduler.dynamodb` or `scheduler.redis` populated |

   Both halves matter, for different failure modes:

   - **Missing required block.** Without this check, `session.type = dynamodb` with an unset
     `table_name` would construct a `DynamoDBScheduledTaskStore` on an empty table name and fail
     later inside `DynamoDBDriver._connect`'s `table.load()` — a connection error at first use,
     three layers from the actual mistake. The check turns it into an `AKConfigError` at startup
     naming the missing field.
   - **Populated wrong block.** The store is resolved from `session.type`, so a
     `scheduler.dynamodb.table_name` set on a Redis deployment is simply never read. Silently
     ignoring it is the worse outcome: the operator believes they have configured a table that does
     not exist and will never be used. Rejecting it makes the contradiction visible at startup.

   This check exists because the store type is *derived* rather than declared. With a
   `scheduler.type` field a mismatch would be impossible to express; deriving from `session.type`
   buys the design's "no separate backend config" property at the cost of needing this validation.

Timing: "at initialization" means process startup on ECS and cold start on serverless — in both
cases before the first request or scheduled execution is processed. Concretely:

| Component | Call site |
|---|---|
| ECS REST service | `RestHandler.__init__` (`deployment/common/rest_handler.py:22-26`) and `ScheduleRESTRequestHandler.__init__`, both constructed before uvicorn starts by `AWSRestAPI.run()`, which `ECSIOHandler.run()` invokes (`ecs_io_handler.py:42-46`) |
| ECS WebSocket service | `ECSWebSocketRequestHandler.__init__`, constructed before uvicorn starts by `AWSWebsocketAPI.run()` (`ecs_io_handler.py:34-40`). `validate_config()` and `SchedulerFactory.build()` only — **not** the `Authoriser` check, which is REST-only |
| ECS output consumer | `ECSOutputConsumer` class body, alongside the existing `_config` class attribute (`akoutputconsumer.py:24-27`) |
| ECS agent runner | `scheduler/tools.py:get_scheduler_tools()`, reached from `SystemToolFactory.get_all()` at agent wrap time — only when the tool set is registered |
| Serverless request handler | `DefaultEndpointsHandler.__init__` (`serverless/core/router/rest_lambda.py:17-24`) and `SystemRoutesHandler.__init__` (`ws_lambda.py:246-251`) |
| Serverless response handler | `ResponseHandler` class body, alongside `_response_store` (`akresponsehandler.py:19-20`) |

`build()` is memoized per process (a module-level singleton behind a `threading.RLock`, the
`ConversationThreadManager.get()` pattern) so the `GetQueueAttributes` call and the boto3 clients are
created once per process, not per request.

### Core model changes — `core/model.py`

```python
SCHEDULED_SESSION_PREFIX = "schedule:"

class ScheduleMode(str, Enum):
    PER_RUN = "per_run"        # session_id = schedule:<id>:<scheduled_time>
    CONTINUOUS = "continuous"  # session_id = schedule:<id>

class ScheduleSpec(BaseModel):
    """The timing expression plus conversation mode — the `schedule` block on a chat body."""
    id: Optional[str] = None                     # caller-supplied scheduled_task_id
    cron: Optional[str] = None                   # exactly one of cron / rate / at
    rate: Optional[str] = None
    at: Optional[datetime] = None
    mode: ScheduleMode = ScheduleMode.PER_RUN
    timezone: str = "UTC"

    @model_validator(mode="after")
    def _exactly_one_expression(self) -> "ScheduleSpec": ...   # else ValueError → 400

class ScheduledRunMetadata(BaseModel):
    """Correlation metadata for one fire of a scheduled task. Set by the timer,
    echoed through the response verbatim, read only by the output consumer."""
    scheduled_task_id: str
    scheduled_task_version: str
    scheduled_time: datetime
    run_id: str

    @classmethod
    def from_body(cls, body: dict) -> "ScheduledRunMetadata | None":
        """Parsed-dict path — the output consumers. One dict.get; validation only on a hit."""

    @classmethod
    def from_raw_body(cls, raw: str | bytes | dict | None) -> "ScheduledRunMetadata | None":
        """Parse-tolerant path — the runners' on_permanent_failure, which must never raise.
        Returns None on any parse or validation failure."""
```

**Two extraction methods, not one.** They serve different callers with different inputs and
different costs, and collapsing them made the *Per-operation cost* claim untrue. `from_body` takes an
already-parsed dict and is what both output consumers call on every output-queue message — its cost
is one `dict.get("scheduled_run")` and nothing else on the overwhelmingly common miss.
`from_raw_body` accepts a raw, possibly-unparseable queue body and is called only from
`on_permanent_failure`, off the hot path. `from_body` may propagate a `ValidationError` on a
malformed block (a real bug worth surfacing on the ordinary path); `from_raw_body` swallows
everything, because the permanent-failure path has no error channel left.

`BaseRunRequest` (`core/model.py:217-222`) gains two optional fields, both defaulting to `None`, so
every existing caller is unaffected:

```python
class BaseRunRequest(BaseChatRequest):
    files: Optional[List[FileData]] = None
    images: Optional[List[ImageData]] = None
    schedule: Optional[ScheduleSpec] = None                  # create-time only; never on a fire
    scheduled_run: Optional[ScheduledRunMetadata] = None     # fire-time only; never on a create
    model_config = ConfigDict(extra="allow")
```

**`core/` imports nothing from `scheduler/`.** `ScheduleSpec`, `ScheduleMode` and
`ScheduledRunMetadata` are *defined* here and re-exported by `scheduler/model.py`, so the dependency
points one way only. A lazy or `TYPE_CHECKING` import could not work: `schedule` is a Pydantic field
annotated `Optional[ScheduleSpec]`, and Pydantic resolves field annotations to real classes when the
model class is constructed at import time — a deferred import would leave `BaseRunRequest`
unbuildable. This is the sandbox split (`_SandboxConfig` in `core/config.py:526`, behaviour imported
lazily inside a function at `core/tool.py:194-196`) applied to models rather than config, and it
keeps the import-order test trivial: importing `agentkernel.core.model` must pull in no module under
`agentkernel.scheduler`.

**`known_fields` must be extended.** `RequestBuilder._attach_additional_context`
(`core/chat_service.py:118-130`) turns every request field not in `known_fields` into an
`AgentRequestAny` handed to the agent. Its current set (line 125) enumerates exactly
`BaseRunRequest`'s declared fields. Without adding the two new names, a scheduled fire would push its
own `scheduled_run` block into the agent's request list as opaque context:

```python
known_fields = {"request_id", "user_id", "group_id", "thread_name", "prompt", "agent",
                "session_id", "images", "files", "schedule", "scheduled_run"}
```

This is a required change, not an optimization, and it is covered by a dedicated test.

**`ResponseBuilder.build_response`** (`core/chat_service.py:277-303`) gains one optional parameter
and one three-line echo:

```python
@staticmethod
def build_response(status_code, session_id, rest_api_mode, result=None, error=None,
                   scheduled_run: Optional["ScheduledRunMetadata"] = None):
    ...
    if scheduled_run is not None:
        response_dict["scheduled_run"] = scheduled_run.model_dump(mode="json")
```

`ChatService.process_chat_request` / `process_async_chat_request` pass `scheduled_run=req.scheduled_run`
at all four call sites (success and both error paths in each). This is a generic pass-through of an
optional block, not scheduling logic, and it is the only change on the response side. It runs before
the `HTTPException` raise, so an errored scheduled run still carries its correlation metadata.

**Thread auto-creation is skipped for scheduled sessions.** `ChatService._thread_pre_run`
(`core/chat_service.py:507-541`) and `_thread_post_run` (`:543-555`) return early when
`req.session_id` starts with the reserved `schedule:` prefix, so scheduled runs never appear in the
owner's thread listings. `_validate_thread` (`:493-505`) is unchanged: a fire carries `user_id` (the
owner), so the existing requirement is already satisfied. The prefix constant
(`SCHEDULED_SESSION_PREFIX = "schedule:"`) lives in `core/model.py` next to `ScheduledRunMetadata`,
so `core/` needs no import from `scheduler/` for this check. This is fixed behaviour, not
configurable.

### Consumer changes

#### Agent runners — happy path unchanged, failure path echoes

`ECSAgentRunner.process_message` (`containerized/akagentrunner.py:97-119`) and
`ServerlessAgentRunner.process_message` (`serverless/akagentrunner.py:121-134`) are **verified
unchanged**. Both validate the same `BaseRunRequest`, call `ChatService.process_chat_request`, and
publish the returned body. The `scheduled_run` echo happens inside `ResponseBuilder`, so neither
runner reads, branches on, or constructs it.

The FIFO attributes propagate with no new mechanism: both runners forward the incoming message's
group and dedup ids verbatim when publishing
(`containerized/akagentrunner.py:85-91`, `serverless/akagentrunner.py:100-106`), so grouping the input
fire by `scheduled_task_id` automatically groups its outcome by `scheduled_task_id` too. The output
queue is FIFO on both targets (`containerized/modules/queues/main.tf:53`; serverless shares the
`fifo_queue` variable across both queues, `serverless/modules/queues/main.tf:15,60`).

The **permanent-failure path does change**, because both runners construct their error body from
scratch without touching the record body (`containerized/akagentrunner.py:122-130`;
`serverless/akagentrunner.py:137-156`). Each now extracts the block best-effort and echoes it:

```python
# after the error body is built, before it is sent to the output queue
scheduled_run = ScheduledRunMetadata.from_raw_body(record.get("Body") or record.get("body"))
if scheduled_run is not None:
    error_body["scheduled_run"] = scheduled_run.model_dump(mode="json")
```

The two runners build `error_body` differently and the echo attaches to each in place: ECS
constructs the dict inline (`containerized/akagentrunner.py:122-130`), while the serverless runner
calls `cls._construct_error_message_body(error_msg=...)` (`serverless/akagentrunner.py:147-149`) and
mutates the returned dict. The echo is the same three lines on both; only the line it is inserted
after differs.

`from_raw_body` never raises, and both call sites are already inside the existing `try/except` that
guarantees `on_permanent_failure` catches its own exceptions (the `QueueConsumer` contract). This is
what makes a retry-exhausted run recordable as `FAILED` without any DLQ involvement.

One asymmetry must be resolved. `ServerlessAgentRunner.on_permanent_failure` sets
`error_message_body["session_id"] = record_attributes["message_group_id"]`
(`serverless/akagentrunner.py:150`); its ECS twin does not. For a scheduled fire the group id is the
`scheduled_task_id`, not a session id, so that line would write a wrong `session_id` into the error
body. **Resolution:** when `scheduled_run` is present, the serverless runner takes `session_id` from
the parsed body instead, and omits it when the body cannot be parsed. Non-scheduled behaviour is
unchanged.

`ServerlessStreamAgentRunner` (`serverless/akagentrunner.py:159-304`) is **verified unchanged**: a
scheduled fire is never produced in `stream` mode — the acknowledgement is delivered at creation
time and no fire is enqueued for a stream (see *Creation acknowledgement*).

#### Output consumers — the only readers of `scheduled_run`

Both consumers gain the same branch, expressed as one shared shape and applied per target. The
presence of a `scheduled_run` block in the response body is exactly how a consumer tells a scheduled
run from an ordinary one; the outcome status is derived from the ordinary response shape — an
`error` key means `FAILED`, otherwise `COMPLETED` — so no scheduling-specific status or error field
is introduced.

```python
# in both consumers, before the existing broadcast/store logic
scheduled_run = ScheduledRunMetadata.from_body(body)
if scheduled_run is not None:
    cls._get_scheduler().mark_run_completed(
        scheduled_task_id=scheduled_run.scheduled_task_id,
        scheduled_task_version=scheduled_run.scheduled_task_version,
        scheduled_time=scheduled_run.scheduled_time,
        status=RunStatus.FAILED if "error" in body else RunStatus.COMPLETED,
        last_error=body.get("error"),
    )
    return  # not broadcast, not written to the response store
```

**`ResponseHandler.process_message`** (`serverless/akresponsehandler.py:90-109`). The branch goes
first, before the execution-mode fan-out. This is necessary, not cosmetic: in `async`/`stream` mode
the handler broadcasts to the originating connection using an `endpoint_url` message attribute
(`:69-76`, dispatched at `:102-105`) that a timer-originated message does not carry, so without the branch a
scheduled response raises `ValueError("endpoint_url is required in SQS message attributes")`. In the
REST modes it would write to the response store, where nobody is polling for it.

**`ECSOutputConsumer.process_message`** (`containerized/akoutputconsumer.py:56-83`) gets the same
branch, and for the same reason rather than merely for symmetry: in WebSocket modes it too broadcasts
using an `endpoint_url` message attribute and raises without one. The existing test
`test_broadcast_via_websocket_raises_when_endpoint_url_missing`
(`tests/test_ecs_akoutputconsumer.py:51`) is that failure, already pinned. So the branch must go
**first** on this consumer as well, before the mode fan-out. The design describes this as the one
branch the feature adds to an existing component; it is one branch per target, symmetric in both.

`on_permanent_failure` on both consumers is **verified unchanged**. An output-queue message that
exhausts its own retries has already failed to be recorded; adding a second store write on that path
would double the failure modes for no benefit, and the run is still visible as "no `last_run_at`
update" plus queue metrics.

Both consumers depend only on the `Scheduler` interface. Neither resolves, imports or calls the
`ScheduledTaskStore`, and neither holds any outcome-write policy — loading the row, applying the four
guards, updating the fields and setting `status = COMPLETED` all happen inside the `Scheduler`. The
consumer's only job is to recognise the block and forward it. Consequence: the guard rules live in
one place, are shared by both targets, and a non-AWS provider can change how outcomes are persisted
without touching either consumer.

#### Chat create path — ECS

`RestHandler` (`deployment/common/rest_handler.py:16`) — the shared queue-aware REST base that
`ECSQueueRequestHandler` extends — gains an optional `authoriser` constructor parameter and a
schedule branch:

```python
def __init__(self, logger_name: str = "ak.deployment.queue_handler", authoriser: Optional[Authoriser] = None):
    super().__init__()
    self._log = logging.getLogger(logger_name)
    self._config = AKConfig.get()
    self._authoriser = authoriser
    SchedulerFactory.validate_config()
    if SchedulerFactory.enabled() and authoriser is None:
        raise AKConfigError("scheduler.enabled requires an Authoriser on the chat route — "
                            "every scheduled task must have an authenticated owner")
    self._schedule_service = ScheduledTaskService(SchedulerFactory.build()) if SchedulerFactory.enabled() else None
```

**Why the shared base rather than `ECSQueueRequestHandler`.** `RestHandler` lives under
`deployment/common/`, which is provider-agnostic, and `ECSQueueRequestHandler`
(`containerized/core/api/rest_api.py:9`) is its only subclass today — so this is a real placement
decision, not a default. It goes in the base because the branch contains nothing AWS-specific: it
calls `ScheduledTaskService`, which rule #4 keeps provider-agnostic, and never names EventBridge,
SQS or boto3. A future queue-mode target inheriting `RestHandler` gets the create path for free. On
any deployment with no `scheduler` block `SchedulerFactory.enabled()` is `False`,
`_schedule_service` is `None`, and `enqueue_and_wait` behaves exactly as it does today.

In `POST /api/v1/chat` — the route declared by `AgentRESTRequestHandler.get_router()`
(`api/handler.py:107`) and served by `RestHandler.enqueue_and_wait` — the branch is the first
statement inside the existing `try`, **before** the `session_id` check (`rest_handler.py:45-46`) and
before `request_id = str(uuid.uuid4())` (`:49`). It must precede the `session_id` check because a
scheduled create legitimately has no session id — the service derives one:

```python
if body.schedule is not None:
    if self._schedule_service is None:
        raise HTTPException(status_code=400, detail="Scheduling is not enabled for this deployment")
    owner_id = self._resolve_user(request)          # 401 when the token is missing or rejected
    ack = self._schedule_service.create(spec=body.schedule, prompt=body.prompt,
                                        agent=body.agent, owner_id=owner_id)
    return JSONResponse(status_code=201, content=ack.model_dump(mode="json", exclude_none=True))
```

Nothing is enqueued: the first message on the input queue appears when the timer fires. In
`rest_sync` the handler does **not** wait on the response store — there is no run to wait for, so
the sync wait is skipped entirely. In `rest_async` the same 201 body is returned;
`GET /api/v1/chat/{session_id}` is not used for scheduling, and run outcomes are read from
`GET /api/v1/schedule/{scheduled_task_id}`. `_resolve_user` is the same 401-on-missing/invalid-Bearer
helper as `ThreadRESTRequestHandler._resolve_user` (`api/thread.py:36-55`), lifted into a shared
mixin in `api/handler.py` so the two implementations do not drift.

`ECSQueueRequestHandler.__init__` (`containerized/core/api/rest_api.py:12-15`) forwards the
`authoriser`. No change is needed to `ECSIOHandler.run()` (`ecs_io_handler.py:24-25`, whose only
parameter is `auth_validator`): the injection point already exists, because `RESTAPI.run()` accepts
a `handlers` list (`api/http.py:90`) and `AWSRestAPI.get_default_handlers()`
(`containerized/core/api/rest_api.py:33-35`) is the seam a deployment overrides to supply handlers
built with its own `Authoriser`.

#### Chat create path — ECS WebSocket

Containerized supports all four execution modes with `queue_mode = true`
(`containerized/variables.tf:241`, and the `execution_mode` validation at `:247-258` admits `async`
and `stream` under queue mode), so an ECS WebSocket deployment has a queue-mode create path of its
own. `ECSWebSocketRequestHandler._handle_chat` (`containerized/core/api/websocket_api.py:404-425`)
routes straight to `_enqueue_chat` (`:335`), which calls `SQSHandler.send_message_to_input_queue`
(`:340`). Without a branch here a `schedule` block sent over an ECS WebSocket connection is
enqueued and **executed immediately instead of scheduled** — the one outcome this feature must never
produce. It gets the branch:

```python
# in _handle_chat, after the `ctx.message.body is None` check (:409-410),
# before the `session_id is required` check (:413-414)
if ctx.message.body.schedule is not None:
    if self._schedule_service is None:
        return self.build_error_http_response(400, "Scheduling is not enabled for this deployment")
    ack = self._schedule_service.create(
        spec=ctx.message.body.schedule, prompt=ctx.message.body.prompt,
        agent=ctx.message.body.agent, owner_id=ctx.user_id,
    )
    return await self._broadcast_ack(ack, ctx)
```

**Identity needs no new mechanism, and no `Authoriser`.** `build_route_context` already resolves
`ctx.user_id` via `get_websocket_handler().get_user_id(connection_id)` and raises
`WSRouteError(401, ...)` when the connection has no user (`websocket_api.py:320-322`) — the same
guarantee the serverless WebSocket section relies on, reached a different way. `ECSIOHandler.run()`
additionally refuses to start a WebSocket deployment without an `AuthValidator`
(`ecs_io_handler.py:36-39`), so the connection is authenticated before any frame arrives.

Consequently **the `AKConfigError`-on-missing-`Authoriser` check does not apply to this handler.**
`ECSWebSocketRequestHandler` extends `ECSWebSocketHandlerBase`, not `RestHandler`, so it does not
inherit the check — and it must not be added, because a WebSocket deployment has no `Authoriser`
object at all and would fail to boot. Behavioural change #8 is scoped to the REST chat route for
exactly this reason.

The acknowledgement travels the caller's live connection, using the same envelope table as the
serverless WebSocket surface — `_broadcast_ack` is the two-line helper that selects it:

| Execution mode | Envelope |
|---|---|
| `async` | `broadcast_message(..., message_type=CHAT_RESPONSE, message=ack)` |
| `stream` | `broadcast_message(..., message_type=STREAM_CHUNK, message={**ack, "done": True})` |

Errors surface as they already do on this route: `WSRouteError` → `build_error_http_response`
(`:421-422`), so a disabled-scheduling create returns 400 and an unauthenticated connection 401,
with no new error channel.

#### Chat create path — serverless REST

`DefaultEndpointsHandler` gains the same branch. `_handle_request`
(`serverless/core/router/rest_lambda.py:103-121`) widens its operation callback from
`(BaseRequest) -> dict` to `(BaseRequest, dict) -> dict` so the operation can read the event's
authorizer context; the signature is internal and has no public surface.

`sync_operation` and `submit_operation` both start with:

```python
ack = self._maybe_schedule(payload, event)
if ack is not None:
    return ack
```

`_maybe_schedule` returns `None` when `payload.body.schedule` is absent, raises a 400-mapped error
when scheduling is disabled, and otherwise resolves the owner from
`event["requestContext"]["authorizer"]["principalId"]` — the value `APIGatewayAuthorizer._build_policy`
sets from `ValidationResult.subject` (`serverless/akauthorizer.py:75-92`).

Identity enforcement differs from ECS here, and this is a **deviation from design.md flagged for
re-review** (see *Deviations from design.md*). Python cannot observe whether Terraform attached the
API Gateway authorizer to the route, so the check cannot be an initialization check on serverless.
It is enforced per request: a `schedule` block with no authorizer context on the event is rejected
with **401**, and the Terraform module attaches the authorizer to the schedule routes.

#### Chat create path — serverless WebSocket

`SystemRoutesHandler._handle_queue_mode` (`ws_lambda.py:367-418`) gains the branch before
`send_message_to_input_queue`. Identity is already authenticated and available:
`ws_handler.get_user_id(connection_id)` (call site `ws_lambda.py:77`; defined at
`deployment/aws/core/websocket_service.py:63`), populated at `$connect` from the JWT's `userId`
claim (`ws_lambda.py:204-208`) — WebSocket connections are unconditionally authenticated, so the
design's identity requirement holds here without a new mechanism.

The acknowledgement travels the caller's live connection, sent by the request handler rather than
the response handler, so it never travels the queues:

| Execution mode | Envelope |
|---|---|
| `async` | `broadcast_message(..., message_type=CHAT_RESPONSE, message=ack)` |
| `stream` | `broadcast_message(..., message_type=STREAM_CHUNK, message={**ack, "done": True})` — a single terminal frame, no token deltas, since nothing is generated at creation time |

Errors surface exactly as an ordinary chat error does in that mode: an HTTP error response in the
REST modes, a `SYSTEM_RESPONSE`/error frame on the connection in the WebSocket modes
(`ws_lambda.py:507-537`).

### Management API — `/api/v1/schedule`

These routes query and manage **already-created** scheduled tasks. Creation is the chat endpoint's
job; there is no new creation endpoint.

| Route | Behaviour | Errors |
|---|---|---|
| `GET /api/v1/schedule` | List, scoped to the caller's own tasks. Soft-deleted rows are never returned — they are an internal grace-period artefact, not a user-visible state. Cursor-paginated. | 401 |
| `GET /api/v1/schedule/{scheduled_task_id}` | Definition plus last-run status. | 401, 403, 404 on unknown **or soft-deleted** |
| `PUT /api/v1/schedule/{scheduled_task_id}` | Body may change the `schedule` block and the message fields (`prompt`, `agent`). Calls `Scheduler.upsert`: the row is written and the registration replaced, so the next fire reflects the new definition. Version retained; a `COMPLETED` one-time task is re-armed. | 400 invalid/too-fine, 401, 403 not owner, 404 no live row, 409 soft-deleted |
| `DELETE /api/v1/schedule/{scheduled_task_id}` | Removes the registration, then soft-deletes the row. Idempotent. | 401, 403 not owner |

All routes require the configured identity resolver; update and delete additionally check ownership.

**ECS mounting.** `agentkernel/api/schedule.py` defines
`ScheduleRESTRequestHandler(RESTRequestHandler)`, taking a required `Authoriser` and a
`ScheduledTaskService`. `RESTAPI.run()` auto-mounts it when `scheduler.enabled`, unless the caller
supplied one — the exact shape of the existing thread auto-mount (`api/http.py:105-112`):

```python
if SchedulerFactory.enabled():
    from .schedule import ScheduleRESTRequestHandler
    if not any(isinstance(h, ScheduleRESTRequestHandler) for h in handlers):
        routers.append(ScheduleRESTRequestHandler().get_router())
```

`ScheduleRESTRequestHandler()` with no `Authoriser` raises `AKConfigError` at construction — the
loud initialization failure the design requires, before uvicorn binds.

**Serverless mounting.** The serverless REST surface is not FastAPI: `RESTLambdaRouter`
(`serverless/core/router/rest_lambda.py:292-388`) keeps a hand-rolled `{path: {method: handler}}`
table and dispatches on an exact string lookup of the resolved path (`:381-385`), with no
path-parameter support. `DELETE /api/v1/schedule/{id}` therefore cannot match today.

`dispatch` gains a **resource-template fallback**, applied only where it currently raises:

```python
handler = methods.get(method)
if not handler:
    # API Gateway supplies the matched resource template and its extracted parameters.
    resource = event.get("resource")
    if resource and env_base_path:
        template = resource.removeprefix(env_base_path)
        handler = self._routes.get(template, {}).get(method)
        if handler:
            return handler(event, context)
    raise ValueError(...)   # unchanged
```

Schedule routes register under their resource templates (`/schedule`, `/schedule/{scheduled_task_id}`)
and read `event["pathParameters"]`. Existing dispatch behaviour is unchanged: the fallback runs only
on inputs that previously produced a `ValueError`.

### Agent-callable tools — `scheduler/tools.py`

`get_scheduler_tools() -> list[SystemTool]` mirrors `get_sandbox_tools()` (`sandbox/tools.py:304-352`)
and is wired into `SystemToolFactory.get_all()` (`core/tool.py:178-200`) with the same
`_agent_allowed` gating the sandbox uses:

```python
scheduler_config = getattr(AKConfig.get(), "scheduler", None)
if scheduler_config and scheduler_config.enabled and SystemToolFactory._agent_allowed(scheduler_config, agent_name):
    from ..scheduler.tools import get_scheduler_tools
    tools.extend(get_scheduler_tools())
```

Four tools: `create_scheduled_task`, `update_scheduled_task`, `delete_scheduled_task`,
`list_scheduled_tasks`. `create` and `update` are both `Scheduler.upsert` underneath, exposed as two
tools because the distinct names and descriptions steer the model better than one overloaded tool;
they are not two code paths. All four go through the same `ScheduledTaskService` as the REST
surfaces.

Following the sandbox conventions exactly: the whole capability's system-prompt section rides on the
first tool's `description` and the rest carry empty descriptions; every tool returns a JSON string;
machinery errors are caught and returned as `{"error": ...}` — tools never raise into the framework.

**Owner binding.** A tool cannot set an arbitrary owner. `ToolContext.get().session.id`
(`core/tool.py:73-79`) identifies the invoking session; the owner is the `user_id` carried on that
session's request, read from the `AgentRequestAny` context entry that `_attach_additional_context`
already injects, or — on a scheduled fire — from the fire's own `user_id`. There is no synthetic
agent identity and no ownership handover: the agent is the mechanism, the human remains the
principal. When no authenticated `user_id` is resolvable the tool returns
`{"error": "no authenticated owner available for this session"}` rather than creating an unowned
task.

**Runner consequence.** These tools execute in-process inside the agent runner, so when the tool set
is registered the runner resolves a `Scheduler` and needs the same access the REST surface has:
table read/write plus EventBridge Scheduler create/update/delete. This is a *tool* concern, not an
execution concern — the runner still has no scheduled-run branch on its message path. It is opt-in:
a deployment that enables scheduling but scopes the tool set away from its agents (via
`scheduler.agents: []`) leaves the runner with no scheduler grants at all, which is why the IAM
grants below are scoped per target rather than applied unconditionally.

### Config changes — `core/config.py`

New optional block, following the `thread` precedent (`core/config.py:615-618`) — the feature is
inert when the block is absent:

```python
class _SchedulerDynamoDBConfig(BaseModel):
    table_name: str = Field(default="ak-scheduled-tasks",
        description="Dedicated DynamoDB table for scheduled tasks. Partition key 'scheduled_task_id' (S), "
                    "GSI 'owner_id-index' on 'owner_id' (S) / 'created_at' (S), TTL attribute 'expiry_time'. "
                    "Never a partition of the session or response-store table.")

class _SchedulerRedisConfig(BaseModel):
    prefix: str = Field(default="ak:scheduled_tasks:",
        description="Key prefix for scheduled-task storage. Uses the session cluster's URL "
                    "(session.redis.url) with a separate keyspace — no new infrastructure.")

class _SchedulerValkeyConfig(BaseModel):
    prefix: str = Field(default="ak:scheduled_tasks:", description="... (session.valkey.url)")

class _SchedulerConfig(BaseModel):
    """Scheduled task support. Requires AWS, queue mode, and a durable session store."""
    enabled: bool = Field(default=False, description="Enable scheduled tasks")
    agents: Optional[list[str]] = Field(default=None,
        description="Agent names the scheduling tools attach to; omitted = all agents, [] = none")
    group_name: Optional[str] = Field(default=None,
        description="EventBridge Scheduler schedule group for this deployment; injected by Terraform "
                    "via AK_SCHEDULER__GROUP_NAME")
    target_role_arn: Optional[str] = Field(default=None,
        description="IAM role EventBridge Scheduler assumes to send to the input queue; injected by "
                    "Terraform via AK_SCHEDULER__TARGET_ROLE_ARN")
    dynamodb: Optional[_SchedulerDynamoDBConfig] = None
    redis: Optional[_SchedulerRedisConfig] = None
    valkey: Optional[_SchedulerValkeyConfig] = None

# on AKConfig, after `sandbox`:
scheduler: Optional[_SchedulerConfig] = Field(default=None,
    description="Scheduled task configurations. Feature is enabled only when this block is present and enabled.")
```

The block carries no scheduled-task *definitions* — scheduled tasks are defined one way, at runtime
by an authenticated caller. `group_name` and `target_role_arn` are unavoidable deployment outputs
(EventBridge Scheduler requires a role ARN to write to SQS) and are Terraform-injected via the
standard `AK_`-prefixed env vars, like the queue URLs already are.

**Compatibility.** Purely additive. No existing field's name, type, default, or description changes.
YAML files and `AK_*` env vars written before this change parse identically; a config with no
`scheduler` block leaves the feature inert, and `getattr(AKConfig.get(), "scheduler", None)` is used
at the `SystemToolFactory` call site so an older config object cannot raise `AttributeError` (the
same defensive shape used for `sandbox` at `core/tool.py:194`).

**Data compatibility.** No existing data layout changes. Response-store entries, session rows and
thread rows written before this change read back identically after it. `scheduled_run` is an
additive optional key on request and response bodies; a consumer that ignores it behaves as before.

### Terraform — `ak-deployment/ak-aws/`

**One gate: `scheduled_task`.** A single root-level boolean turns the entire capability on. Setting
`scheduled_task = true` creates every scheduler resource, every IAM permission, and every route;
`false` (the default) creates none of them and leaves the deployment byte-identical to today. There
is no second switch and no per-resource toggle — a deployer flips one variable.

This mirrors the existing `queue_mode` gate exactly (`containerized/variables.tf:241`,
`containerized/queue_mode.tf:6,22`), which is the established pattern for a whole-feature toggle in
these modules: a root bool, `count = var.<gate> ? 1 : 0` on the module, and outputs guarded with
`var.<gate> ? ... : null` (`containerized/outputs.tf:44-57`).

```hcl
# containerized/variables.tf and serverless/variables.tf
variable "scheduled_task" {
  description = "Enable scheduled tasks: creates the scheduled-task table, EventBridge Scheduler schedule group, timer execution role, component IAM grants, and (serverless) the /schedule API Gateway routes. Requires queue_mode = true."
  type        = bool
  default     = false
}

variable "scheduled_task_config" {
  description = "Scheduled task configuration. Ignored when scheduled_task = false."
  type = object({
    table_name          = optional(string, null)  # null → "<prefix>-scheduled-tasks"
    schedule_group_name = optional(string, null)  # null → "<prefix>-schedules"
    enable_agent_tools  = optional(bool, false)   # grant the agent runner scheduler access
  })
  default = {}
}
```

`scheduled_task = true` with `queue_mode = false` is rejected by a `validation` block on
`scheduled_task_config`, matching the existing `scaling_config.enabled requires queue_mode = true`
precondition (`containerized/variables.tf:396-401`). This is the Terraform-side twin of
`SchedulerFactory.validate_config()`'s queue-mode check — the deploy fails before the app ever gets a
chance to fail at startup.

#### Resources created when `scheduled_task = true`

A new `modules/scheduler/` per target, gated `count = var.scheduled_task ? 1 : 0`. Both directories
(`containerized/modules/scheduler/` and `serverless/modules/scheduler/`) **already exist in the tree
and are empty** — scaffolding committed ahead of this spec. They are the directories this section
fills; nothing else in either target references them today, so they are inert until then.

1. **Scheduled-task table.** `aws_dynamodb_table` — partition key `scheduled_task_id` (S), GSI
   `owner_id-index` on `owner_id` (S) / `created_at` (S), TTL on `expiry_time`. Created only when
   the deployment's session store is DynamoDB; when sessions are Redis/Valkey the module provisions
   **nothing** here, because the existing cluster is reused with a separate keyspace. The module
   derives which case applies from the same `session_store_type` variable the deployment already
   uses to pick its session backend, so the two can never disagree.
2. **`aws_scheduler_schedule_group`** — one per deployment, named from
   `scheduled_task_config.schedule_group_name`. It exists for namespacing and destroy-time cleanup:
   deleting the group removes every registration the deployment created, so `terraform destroy`
   leaves no orphaned schedules behind.
3. **Timer execution role** — `aws_iam_role` assumable by `scheduler.amazonaws.com`, with a single
   inline policy allowing `sqs:SendMessage` on the input queue **only**.
4. **Component IAM grants** — attached to the existing task/execution roles, deliberately unequal so
   no component gets more than its role needs:

| Component | Table | EventBridge Scheduler | SQS |
|---|---|---|---|
| REST service / request handler | read + write | create/update/delete, `Resource` scoped to the deployment's schedule group | `GetQueueAttributes` on the input queue |
| Response handler / output consumer | read + update | **none** — it never registers or removes a schedule | `GetQueueAttributes` on the input queue |
| Agent runner | none, unless `enable_agent_tools = true` → read + write | none, unless `enable_agent_tools = true` → create/update/delete in the group | only when `enable_agent_tools = true` |

The agent runner's grants sit behind `scheduled_task_config.enable_agent_tools` because the tools are
opt-in in the application too (`scheduler.agents: []` scopes them away). A deployment that enables
scheduling but not the agent tools leaves the runner with no scheduler permissions at all — the
Terraform gate and the app-level gate line up, so neither grants access the other doesn't.

Every component that constructs a `Scheduler` needs `sqs:GetQueueAttributes` on the input queue, to
read the visibility timeout the soft-delete TTL derives from.

5. **API Gateway routes** — `GET /schedule` plus `GET`/`PUT`/`DELETE` on
   `/schedule/{scheduled_task_id}`. Both targets need them: each gateway is an explicit route
   allow-list with no catch-all, so a route that is not declared 404s before the request reaches the
   application.
   - *Serverless* — REST API resources and methods, each with the **existing request authorizer
     attached**. This is the deploy-time half of the identity requirement (see *Deviations from
     design.md* #1): the app rejects a schedule request with no authorizer context, and this wiring is
     what guarantees the context is there.
   - *Containerized* — HTTP API route keys proxied to the ALB, each with an `overwrite:path` back to
     the application's fixed `/api/v1/schedule…` path, the way the chat route already does (the
     gateway prefix is configurable via `api_base_path`/`api_version`, the app's path is not). The
     item routes carry the path variable through as `$request.path.scheduled_task_id`. Identity is
     resolved in-process by the `Authoriser` here, not by a gateway authorizer. WebSocket modes are
     unaffected — schedule actions ride the existing `$default` route.

#### Outputs

The module's outputs are the contract between Terraform and the application config — they are what
gets injected as `AK_SCHEDULER__*` env vars, the same way the queue URLs already are
(`containerized/modules/rest-service/main.tf:16-21`, `modules/agent-runner/main.tf:7`). All are `null`
when `scheduled_task = false`, matching the guarded-output convention at
`containerized/outputs.tf:44-57`.

| Root output | Value | Injected as |
|---|---|---|
| `scheduled_task_enabled` | `var.scheduled_task` | `AK_SCHEDULER__ENABLED` |
| `scheduled_task_table_name` | table name; `null` on Redis/Valkey sessions | `AK_SCHEDULER__DYNAMODB__TABLE_NAME` |
| `scheduled_task_table_arn` | table ARN; `null` on Redis/Valkey sessions | — (consumed by IAM) |
| `scheduled_task_schedule_group_name` | schedule group name | `AK_SCHEDULER__GROUP_NAME` |
| `scheduled_task_target_role_arn` | timer execution role ARN | `AK_SCHEDULER__TARGET_ROLE_ARN` |

**Backend-specific env vars are injected conditionally, never as a null.** Only the block matching
the deployment's session backend is injected — the DynamoDB table name is *not* set on a
Redis/Valkey deployment, and the keyspace prefix is not set on a DynamoDB one. This follows the
conditional-merge shape the session store env vars already use
(`containerized/modules/rest-service/main.tf:1-25`, e.g.
`var.dynamodb_memory_table_arn != null ? { AK_SESSION__DYNAMODB__TABLE_NAME = ... } : {}`):

```hcl
var.scheduled_task ? merge(
  {
    AK_SCHEDULER__ENABLED         = "true"
    AK_SCHEDULER__GROUP_NAME      = var.scheduled_task_schedule_group_name
    AK_SCHEDULER__TARGET_ROLE_ARN = var.scheduled_task_target_role_arn
  },
  var.scheduled_task_table_name != null
    ? { AK_SCHEDULER__DYNAMODB__TABLE_NAME = var.scheduled_task_table_name }
    : {}
) : {}
```

Injecting a `null` is not merely untidy: the ECS `environment` map and Lambda `environment.variables`
both reject null values, so the unconditional form would fail `terraform apply` on every
Redis/Valkey deployment. The conditional form also keeps the running container's environment honest
— an operator reading it sees exactly the one backend that is in use.

Both example deployments (`examples/aws-serverless/scalable-openai`,
`examples/aws-containerized/openai-dynamodb-scalable`) set `scheduled_task = true` in their
`terraform.tfvars` and declare a **placeholder `scheduler` block** in `config.yaml`, with the
Terraform-supplied values left empty:

```yaml
scheduler:
  enabled: true
  group_name: ""        # injected by Terraform via AK_SCHEDULER__GROUP_NAME
  target_role_arn: ""   # injected by Terraform via AK_SCHEDULER__TARGET_ROLE_ARN
  dynamodb:
    table_name: ""      # injected by Terraform via AK_SCHEDULER__DYNAMODB__TABLE_NAME
```

The placeholder block is deliberate, not redundant. `scheduler` is `Optional[_SchedulerConfig] = None`
(the `thread` precedent), and **no existing config block in this repo is populated by `AK_*` env vars
alone while defaulting to `None`** — `execution` and `sandbox` are both non-`Optional` with a
`default_factory`, so neither demonstrates that path. Declaring the block keeps the env injection on
the proven footing the queue URLs already use (`examples/aws-containerized/openai-dynamodb-scalable/config.yaml:14`,
`url: ""  # injected by Terraform via AK_EXECUTION__QUEUES__INPUT__URL`).

If `test_scheduler_config.py` confirms that `EnvSettingsSource` populates an absent `Optional` block
from `AK_SCHEDULER__*` alone, the placeholder can be dropped in a follow-up — but the spec does not
assume it. Empty strings do not weaken the enablement check: `validate_config()` treats an empty
`group_name` or `target_role_arn` as unset and raises `AKConfigError`, so a deployment that enables
scheduling in YAML without the Terraform wiring still fails loudly at startup.

Operational consequence to accept: a fresh environment starts with an empty table — seeding is an API
call, not a deploy artefact.

### Behavioural changes

Exhaustive; each is intentional with its justification.

1. **`known_fields` grows by `schedule` and `scheduled_run`** (`core/chat_service.py:125`). A caller
   who previously sent an unknown field named `schedule` or `scheduled_run` had it forwarded to the
   agent as an `AgentRequestAny`; it is now consumed by the scheduler or rejected as an invalid
   `ScheduleSpec`. *Intentional:* leaking scheduling metadata into the agent's context would be
   worse, and both names are now declared model fields.
2. **Response bodies gain a `scheduled_run` key** when — and only when — the request carried one.
   *Intentional:* it is how an output consumer tells a scheduled run from an ordinary one. Ordinary
   traffic is byte-identical to before.
3. **Serverless `ResponseHandler.process_message` no longer broadcasts or stores a response carrying
   `scheduled_run`.** *Intentional:* a scheduled run has no live client channel and no `endpoint_url`
   attribute, so the pre-change code would raise; and nobody polls the response store for it.
4. **`ECSOutputConsumer.process_message` no longer writes a `scheduled_run` response to the response
   store**, for the same reason.
5. **Both runners' `on_permanent_failure` now parse the record body** (best-effort, never raising) to
   echo `scheduled_run`. *Intentional:* it is what makes a retry-exhausted run recordable as `FAILED`
   with no DLQ processing.
6. **`ServerlessAgentRunner.on_permanent_failure` no longer sets `session_id` from
   `message_group_id`** when the failed message is a scheduled fire (`serverless/akagentrunner.py:150`). For a
   fire the group id is the `scheduled_task_id`, so the pre-change line would write a wrong
   `session_id`. Non-scheduled behaviour is unchanged. This resolves the divergence with the ECS
   twin, which never set the field.
7. **`POST /api/v1/chat` accepts a body with no `session_id`** when a `schedule` block is present
   (`rest_handler.py:45-46` is now reached only on the non-schedule path). *Intentional:* the
   service derives the session id; requiring the caller to invent one would be meaningless.
8. **`RestHandler.__init__` raises `AKConfigError`** when `scheduler.enabled` and no
   `Authoriser` was supplied. *Intentional:* every scheduled task must have an unforgeable owner, and
   failing at startup beats failing at the first create. **Scoped to the REST chat route only** —
   `ECSWebSocketRequestHandler` does not extend `RestHandler` and must not inherit this check, since
   a WebSocket deployment authenticates at `$connect` via an `AuthValidator` and has no `Authoriser`
   object to supply.
9. **`RESTLambdaRouter.dispatch` gains a resource-template fallback**, reached only where it
   previously raised `ValueError`. *Intentional:* the router has no path-parameter support and
   `/schedule/{id}` needs one.
10. **`RESTAPI.run()` auto-mounts the schedule router** when scheduling is enabled, mirroring the
    thread auto-mount.
11. **Sessions whose id starts with `schedule:` skip thread creation and message appending**
    (`core/chat_service.py:507-555`). *Intentional:* scheduled activity is kept out of the owner's
    regular conversation history. Fixed behaviour, not configurable.
12. **`ECSWebSocketRequestHandler._handle_chat` no longer enqueues a frame carrying a `schedule`
    block** (`containerized/core/api/websocket_api.py:404-425`); it creates the schedule and
    broadcasts the acknowledgement instead. *Intentional:* containerized supports `async`/`stream`
    under `queue_mode = true`, so without this branch a schedule sent over an ECS WebSocket would be
    executed immediately rather than scheduled. Frames with no `schedule` block are unaffected.

**Non-changes** — fixed by this spec and verified against the base branch:

- `BaseRequest` envelope shape and `BaseRequest.from_payload` semantics (`core/model.py:225-264`).
- `SQSHandler`'s public surface and its default `message_group_id = session_id` for ordinary traffic
  (`sqs_handler.py:346,388`); `QueueHandler.QueueMessageBody` and `SendMessageAttributes`.
- `ResponseStore` ABC and all three backends; the response-store data layout.
- Session store, thread store and attachment store layouts, config and behaviour.
- Agent-runner happy path on both targets; `ServerlessStreamAgentRunner` entirely.
- `on_permanent_failure` on both output consumers.
- DLQ configuration and semantics; retry policy; visibility-timeout redelivery. The DLQ stays what it
  already is — a backstop for messages that fail outside the `ApproximateReceiveCount` check.
- `ECSIOHandler.run()`'s signature (`ecs_io_handler.py:24-25`) and `AWSRestAPI.get_default_handlers()`
  (`containerized/core/api/rest_api.py:33-35`). An `Authoriser`-carrying handler is injected through
  the existing seam, not a new parameter.
- All existing public exports; the new ones are additive.

### Reliability properties this design relies on

- **Exactly one enqueue per scheduled time**, with any number of replicas, from the timer firing once
  plus `MessageDeduplicationId = <id>:<scheduled_time>`. No leader election, no distributed lock, no
  dedup window of our own — by construction, not configuration, because no replica polls for due
  work.
- **After enqueue, Agent Kernel's existing guarantee applies unchanged**: delivery is at-least-once,
  so a consumer crash after the agent's side effects but before message deletion re-executes the run.
  Agents whose actions must not repeat need to be idempotent. This feature neither changes nor
  extends retry, DLQ or reprocessing.
- **Outcome ordering per scheduled task is guaranteed, not assumed.** Outcomes travel the output queue
  under `MessageGroupId = scheduled_task_id` on a FIFO queue, so SQS delivers at most one in-flight
  message per group and outcomes are consumed in publish order. Guard 4 is defence in depth behind
  that ordering.
- **Repeat outcome writes are idempotent.** The outcome inherits the fire's dedup id, so a
  re-execution inside the 5-minute FIFO window publishes only one outcome; and even a second one
  would be rejected by guard 4 or write identical values.
- **Missed fires are the timer's problem.** Before enqueue, delivery failures are retried by
  EventBridge Scheduler per its own policy and, on exhaustion, land in the timer's DLQ. Agent Kernel
  does not reconstruct or replay missed fires from the table. A fire that arrives outside an
  acceptable staleness window (`scheduled_time` older than the derived soft-delete TTL) is logged at
  WARNING by the output consumer and still executed; operators detect gaps from `last_run_at` and
  timer-side metrics.

### Concurrency contract

`ECSOutputConsumer` runs `execution.queues.output.no_of_consumers` threads (default 2,
`core/config.py:343-351`) and `ECSAgentRunner` runs `execution.queues.input.no_of_consumers` threads
(default 5, `:326-334`), all sharing one process. The `Scheduler`, its store and its boto3 clients
are shared across those threads.

- **Construction is not racy.** `SchedulerFactory.build()` memoizes behind a `threading.RLock`, and
  `AWSScheduler.__init__` creates both boto3 clients eagerly. boto3 clients are safe for concurrent
  *calls* but not concurrent *creation*, so no lazy client init is used anywhere in this package.
- **Drivers are already thread-safe.** `BaseDriver` serializes connect/reconnect with a per-instance
  `threading.Lock`, which is why the response stores can already be shared under `ECSOutputConsumer`.
- **`mark_run_completed` is a read-modify-write and is not internally locked.** It does not need to
  be: SQS FIFO delivers at most one in-flight message per `MessageGroupId`, and every outcome for a
  scheduled task carries `MessageGroupId = scheduled_task_id`, so two outcomes for the same task can
  never be processed concurrently — across threads *or* across replicas. Guard 4 additionally makes a
  late redelivery harmless. This dependency is explicit, which is why the FIFO precondition is
  enforced at initialization rather than assumed.
- **The management API can race a consumer.** A `PUT` and an outcome write can interleave. The
  outcome write updates only the `last_run_*` fields and never the definition; `upsert` on an
  existing row writes only the definition fields and never `last_run_*`. Both go through
  `ScheduledTaskStore.update_fields` with disjoint key sets — which is why the ABC carries that
  method and not just `put`, a whole-row write that could not express this. On DynamoDB
  `update_fields` is one `UpdateItem` over the given attributes, with a `ConditionExpression` on
  `scheduled_task_version` when `expected_version` is supplied; on Redis/Valkey the row is a single
  JSON string, so it re-reads and merges under a short `SET NX`-guarded lock key
  (`<prefix>lock:<id>`, 5 s TTL) before writing. Losing the lock raises `SchedulerConflictError` →
  409, which the caller can retry. Only row *creation* uses `put`.

### Per-operation cost

The feature must not tax ordinary traffic. What it adds, per path:

| Path | Added work | Verdict |
|---|---|---|
| Every chat request (both targets) | One `is None` check on `body.schedule`; two extra keys in `known_fields`' set membership test | Negligible |
| Every agent response | One `is None` check in `ResponseBuilder.build_response` | Negligible |
| Every output-queue message | `ScheduledRunMetadata.from_body(body)` — one `dict.get("scheduled_run")` on an already-parsed body, returning on the miss before any validation | Negligible |
| Retry-exhausted messages only | `ScheduledRunMetadata.from_raw_body(...)` — one best-effort JSON parse of the record body | Off the hot path |
| Scheduled outcomes only | One store read + one store write | Proportional; only scheduled traffic pays |
| Process startup, scheduler-enabled components only | One `GetQueueAttributes` call | Once per process, not per request |

No new work lands on the ordinary chat, streaming, or session paths.

---

## Error handling

| Failure | Where | Behaviour |
|---|---|---|
| `scheduler.enabled` with a queue URL unset, a non-`.fifo` input queue, a non-durable `session.type`, or a missing `group_name`/`target_role_arn` | `SchedulerFactory.validate_config()` | `AKConfigError` at component initialization — process startup on ECS, cold start on serverless |
| Backend block missing for the resolved `session.type`, or a non-matching backend block populated | `SchedulerFactory.validate_config()` check #6 | `AKConfigError` at initialization, naming the field. Prevents both a late `table.load()` connection failure and a silently-ignored table name |
| `scheduler.enabled` on the ECS **REST** chat route with no `Authoriser` | `RestHandler.__init__`, `ScheduleRESTRequestHandler.__init__` | `AKConfigError` at initialization. Not applied to `ECSWebSocketRequestHandler`, which authenticates at `$connect` and has no `Authoriser` |
| No user resolvable for an ECS WebSocket connection carrying a `schedule` block | `build_route_context` (`websocket_api.py:320-322`) | `WSRouteError(401)` → error frame, before the schedule branch is reached |
| `GetQueueAttributes` fails | `AWSScheduler.__init__` | Raises — no fallback to a guessed TTL |
| Missing optional dependency for the resolved store | `ScheduledTaskStoreBuilder.build()` | `ImportError` naming the pip extra, via `require_extra` (`core/util/factory.py:49-64`). `dynamodb` → `aws`, `redis` → `redis`, `valkey` → `valkey`. No new extra is introduced: EventBridge Scheduler is reached through boto3, already in the `aws` extra |
| `schedule` block present while scheduling is disabled | chat create path, all **four** surfaces (ECS REST, ECS WS, serverless REST, serverless WS) | 400 (REST) / error frame (WS). Never a silent no-op, and never an immediate execution |
| Invalid or too-fine `ScheduleSpec` | `ScheduledTaskService.create` / `update` | `ScheduleValidationError` → 400, before any AWS call. Never silently rounded |
| Caller does not own a live row | service | `SchedulerPermissionError` → 403 |
| Target id is soft-deleted | service | `SchedulerConflictError` → 409 on create/update; 404 on get (soft-deleted rows are not user-visible) |
| No live row on update | service | `SchedulerNotFoundError` → 404. Update never creates |
| Missing/invalid Bearer token on a management or schedule-create route | `_resolve_user` | 401 |
| No authorizer context on a serverless schedule request | `_maybe_schedule` | 401 |
| Row write succeeds, timer registration fails | `Scheduler.upsert` | The row is restored to its prior state (deleted when it was newly created), then the error propagates. A row without a registration would silently never fire, which is worse than a failed create |
| Timer registration removed, row soft-delete fails | `Scheduler.delete` | Error propagates. The registration is gone, so no further fires; the caller retries the delete. Ordering is deliberate — stopping fires first is the safe half |
| Outcome arrives for an absent, deleted, mismatched or stale row | `Scheduler.mark_run_completed` | Silent no-op: logged at WARNING, returns `False`, message acknowledged. Never retried, never dead-lettered — the run genuinely has nowhere to be recorded |
| Store or AWS failure inside `mark_run_completed` | `Scheduler.mark_run_completed` | **Raises.** The consumer's normal retry path applies. A guard rejection and an infrastructure failure are deliberately different: only the former is a no-op |
| Timer cannot deliver to the input queue | EventBridge Scheduler | Retried per the schedule's retry policy, then the timer's DLQ. Infrastructure behaviour, outside Agent Kernel |
| Agent run fails or exhausts retries | existing runner paths | Error body echoing `scheduled_run` reaches the output consumer like any other outcome and is recorded as `FAILED` with the retry message as `last_error` |
| A tool call fails for any reason | `scheduler/tools.py` | Caught and returned as `{"error": ...}` JSON — tools never raise into the framework |

Exception scope is explicit throughout: `from_raw_body` catches `(json.JSONDecodeError, TypeError,
ValidationError)` and returns `None`; `from_body` catches nothing beyond the absent-key miss, so a
malformed `scheduled_run` block on the ordinary consumer path surfaces as a `ValidationError` rather
than being silently dropped; the guard checks catch nothing (they are plain comparisons on a
loaded row); store calls are not wrapped, so backend errors propagate to the consumer's retry
machinery. No bare `except Exception` is added outside the two `on_permanent_failure` handlers, where
the `QueueConsumer` contract already requires it.

---

## Testing

Run with `cd ak-py && uv run pytest`.

### New test files

| File | Asserts |
|---|---|
| `tests/test_scheduler_config.py` | `validate_config()` raises `AKConfigError` for: disabled-but-used, missing input URL, missing output URL, non-`.fifo` input URL, `session.type` in {`in_memory`, `cosmosdb`, `firestore`, a dotted path}, missing **or empty-string** `group_name`/`target_role_arn`. Returns cleanly for each valid combination. TTL derivation: `visibility_timeout × receives + 300`, floored at 900, where `receives` is the max of the queue's `RedrivePolicy.maxReceiveCount` and the AKConfig value — one case each way (queue value higher, AKConfig value higher), plus **an absent `RedrivePolicy` falling back to the AKConfig value rather than to zero**, which is the default-deployment case since `input_queue_create_dlq` defaults to `false`. `GetQueueAttributes` failure raises instead of defaulting. `enabled()` is False when the block is absent. Check #6 both ways per backend: the matching block missing or empty raises, and a non-matching block populated raises (e.g. `session.type: redis` with `scheduler.dynamodb.table_name` set) — the second is the regression guard against silently ignoring a configured-but-unused table. Plus one probe test recording whether `AK_SCHEDULER__*` env vars alone populate the absent `Optional` block — the result decides whether the examples' placeholder block can be dropped (see *Terraform*) |
| `tests/test_scheduled_task_store.py` | Per backend, against the store contract: put/get round trip, `list_by_owner` scoping and cursor, `soft_delete` sets `deleted`/`deleted_at` and the expiry, and a soft-deleted row is still `get`-able. **`list_by_owner` excludes soft-deleted rows on every backend, and a page is not short because one was filtered.** **`update_fields` writes only the named attributes and leaves every other one untouched** (write `last_run_*`, assert the definition fields are unchanged, and the reverse), and **returns `False` without writing when `expected_version` mismatches**. DynamoDB against a mocked `DynamoDBDriver` (the `test_sessions_dynamodb.py` pattern) — asserts the driver is built with `ttl=0`, that `expiry_time` is written **only** by `soft_delete`, that `soft_delete` issues a `REMOVE owner_id` so the GSI stays sparse, and that `list_by_owner` queries the index with **no** `FilterExpression`. Redis against a fake client (the `test_sessions_valkey.py` / `test_multimodal_redis_store.py` pattern) — asserts `set()` writes no `ex`, that `soft_delete` calls `client.expire` with the derived seconds, that `list_by_owner` prunes owner-set members whose row key is gone while *retaining* members whose row is merely soft-deleted, and that `update_fields` takes and releases the `<prefix>lock:<id>` key |
| `tests/test_scheduler_aws.py` | `AWSScheduler` against mocked boto3 `scheduler`/`sqs` clients and a fake store. `upsert` builds a universal target with `MessageGroupId = scheduled_task_id`, `MessageDeduplicationId = <id>:<scheduled_time>`, both context variables in the payload, `request_id` message attribute present, and `MaximumEventAgeInSeconds = 300`. One-time schedules set `ActionAfterCompletion="DELETE"`. Sub-minute cron/rate raises before any AWS call. `per_run` vs `continuous` session-id shape. Registration failure rolls the row back. **The four guards**, one test each, asserting a `False` return and no store write; plus a store exception propagating rather than no-op'ing. A one-time task's accepted outcome sets `status = COMPLETED` and `completed_at` |
| `tests/test_scheduled_task_service.py` | Id generation (`schedule_<hex>`) vs caller-supplied; fresh version on a new id and **retained** version on a live upsert and on `update`; owner stamped from the parameter and never from the body; `schedule:` prefix on both session-id shapes; 403 on a foreign live row, 409 on a soft-deleted id, 404 on `update` with no live row; a `COMPLETED` one-time task re-armed by `PUT`. `CreateAck`: `session_id` present for `continuous` and **absent** for `per_run`; `next_run_at` equals the `at` value for a one-time schedule, equals `created_at + interval` for a `rate`, and is `None` for a `cron`; `request_id` is always present, echoing a caller-supplied id when given and generated otherwise |
| `tests/test_schedule_router.py` | FastAPI `TestClient` over `ScheduleRESTRequestHandler` (the `test_thread_router.py` pattern, with a `StaticAuthoriser`). Construction without an `Authoriser` raises `AKConfigError`. 401 missing/invalid Bearer; list returns only the caller's rows and excludes soft-deleted; 404 on unknown and on soft-deleted `GET`; 403/409/404 on `PUT`; 403 on `DELETE`; routes absent from the app when `scheduler.enabled` is false |
| `tests/test_scheduler_tools.py` | All four tools route through `ScheduledTaskService` (asserted with a mock service); the owner is bound from the invoking session and cannot be supplied as an argument; a service error is returned as `{"error": ...}` and never raised; `SystemToolFactory.get_all()` includes the tools only when enabled and honours `scheduler.agents` scoping |
| `tests/test_agent_runner_permanent_failure.py` | **New file — neither non-stream runner has an existing test** (`test_akagentrunner_stream.py` and `test_ecs_akagentrunner_stream.py` cover the stream runners only), so behavioural changes #5 and #6 are otherwise untested. Both runners: a record whose body carries `scheduled_run` produces an error body echoing it verbatim; an unparseable body produces the pre-change error body and **does not raise**; the ECS runner's inline error dict and the serverless runner's `_construct_error_message_body` result (`serverless/akagentrunner.py:147-149`) are each asserted in place. Serverless only: `session_id` comes from the parsed body when `scheduled_run` is present, is **omitted** when the body cannot be parsed, and still equals `record_attributes["message_group_id"]` for a non-scheduled record (`:150`) — the regression guard for #6 |
| `tests/test_rest_handler_schedule.py` | **New file** — covers behavioural changes #7 and #8, which `test_schedule_router.py` does not reach (that file covers the management routes only). FastAPI `TestClient` over `ECSQueueRequestHandler`: a body with a `schedule` block returns 201 with the ack and **does not** call `send_message_to_input_queue`; the same body with scheduling disabled returns 400; a body with neither `session_id` nor `schedule` still returns 400 (the pre-change behaviour at `rest_handler.py:45-46`); `RestHandler.__init__` raises `AKConfigError` when `scheduler.enabled` and no `Authoriser` is supplied, and does **not** raise when scheduling is disabled |
| `tests/test_ecs_websocket_schedule.py` | **New file** — covers behavioural change #12. A chat frame carrying a `schedule` block creates the task and **`SQSHandler.send_message_to_input_queue` is never called**; the ack is broadcast as `CHAT_RESPONSE` in `async` mode and as a single `STREAM_CHUNK` with `done: True` in `stream` mode; a frame with a `schedule` block and scheduling disabled returns 400; a connection with no resolvable user still yields 401 from `build_route_context` (`websocket_api.py:320-322`); an ordinary frame with no `schedule` block is enqueued exactly as before; and constructing `ECSWebSocketRequestHandler` with `scheduler.enabled` and no `Authoriser` **does not raise** — the guard against wrongly inheriting the #8 check |

### Changed existing tests

| File | Change |
|---|---|
| `tests/test_akresponsehandler.py` | New cases: a `scheduled_run` response calls `mark_run_completed` and is **neither broadcast nor stored**, in each of `rest_sync`, `async` and `stream` (the `async`/`stream` cases are the regression guard — the pre-change code raises on the missing `endpoint_url`). Existing patch target `agentkernel.deployment.aws.serverless.akresponsehandler.AKConfig` (line 84) is retained; the new cases additionally patch the module's scheduler accessor. All existing assertions unchanged |
| `tests/test_model.py` | `BaseRunRequest` defaults `schedule` and `scheduled_run` to `None` and round-trips both; `ScheduleSpec` rejects zero and multiple timing expressions; `from_body` returns `None` for a dict lacking the block and parses a valid one; `from_raw_body` returns `None` for malformed JSON, a non-dict body, `None`, and a dict lacking the block, and parses both a JSON string and a dict. Plus the **import-order guard**: importing `agentkernel.core.model` loads no module under `agentkernel.scheduler` (assert against `sys.modules` in a subprocess), and `agentkernel.scheduler.model.ScheduleSpec is agentkernel.core.model.ScheduleSpec` |
| `tests/test_chat_service_streaming.py` | Extended (or a sibling `test_chat_service_scheduled.py`) with the **`known_fields` regression guard**: a request carrying `scheduled_run` produces no `AgentRequestAny` for it; `build_response` echoes the block on success and on both error paths, and omits the key entirely when absent; a `schedule:`-prefixed session id skips `get_or_create_thread` and `append_message` |
| `tests/test_ecs_akoutputconsumer.py` | **This file already exists** (8 tests over `process_message` in `stream`/`async` WebSocket modes plus three `on_permanent_failure` cases), so the `ECSOutputConsumer` work extends it rather than creating a new file. New cases: a response carrying `scheduled_run` calls `mark_run_completed` with the derived status and is **neither broadcast nor written to the response store**; a body with an `error` key maps to `FAILED` with `last_error`; an ordinary response is stored/broadcast exactly as before; `on_permanent_failure` is unchanged. The `async`/`stream` cases are the regression guard — `test_broadcast_via_websocket_raises_when_endpoint_url_missing` (`:51`) is the pre-change failure a timer-originated message would hit. All existing assertions unchanged |
| `tests/test_lambda_router.py` | The resource-template fallback resolves `/schedule/{scheduled_task_id}` from `event["resource"]` and passes `pathParameters`; an unmatched path still raises `ValueError` (unchanged) |
| `tests/test_api_http.py` | `RESTAPI.run()` mounts the schedule router when `scheduler.enabled`, skips it when a `ScheduleRESTRequestHandler` was supplied, and does not mount it when disabled |
| `tests/test_serverless_request_handle.py` | A payload carrying a `schedule` block is not enqueued and returns the ack; identity is taken from `requestContext.authorizer.principalId`; a missing authorizer context yields 401 |

Not changed, and verified so: `test_sqs_handler.py` (the `SQSHandler` surface is untouched),
`test_ecs_sqs_consumer_parallel.py` (`process_message`/`delete` semantics are untouched),
`test_akagentrunner_stream.py` and `test_ecs_akagentrunner_stream.py` (both stream runners are
untouched; the non-stream runners' `on_permanent_failure` change is covered by the new
`test_agent_runner_permanent_failure.py`), `test_store_builders.py`
and `test_factory.py` (existing builders are untouched; the new builder is covered in
`test_scheduled_task_store.py`).

---

## Deviations from design.md

Three points where detailing revealed the design cannot be implemented exactly as written. All are
flagged for design re-review rather than silently absorbed.

1. **Identity-resolver enforcement is init-time on ECS but per-request on serverless REST.**
   design.md (*Ownership and identity*) requires that "enabling scheduling without one fails at
   initialization, with the same timing as the queue-mode check", as "a single unambiguous check".
   On ECS this is exact — the `Authoriser` is a Python object, checked in the handler constructor.
   On serverless the identity resolver is an API Gateway authorizer configured in Terraform and
   invisible to the Lambda process, so no initialization check is possible. This spec enforces it as
   a **401 when a schedule request arrives with no authorizer context**, plus a Terraform module that
   attaches the authorizer to the schedule routes. The guarantee (no unowned scheduled task) holds;
   the *timing* of the failure differs per target. WebSocket mode is unaffected — connections are
   already unconditionally authenticated at `$connect`.

2. **The `scheduler` config block carries five fields, not one.** design.md (*Management and
   administration*) states "The `scheduler` config block carries only `enabled`." This spec reads
   that as "carries no scheduled-task definitions" and adds `agents`, `group_name`,
   `target_role_arn`, and the three per-backend location blocks. `group_name` and `target_role_arn`
   are unavoidable — EventBridge Scheduler requires a schedule group and a role ARN to write to SQS,
   and neither is derivable from existing config. The table name / key prefix are needed because the
   design also requires the scheduled-task table to be a **new** table, never a partition of the
   session table, so it cannot be derived from `session.*`. `agents` is added for parity with the
   sandbox capability's tool scoping, which the design's *Agent-callable tools* section relies on
   ("a deployment that … excludes the tool set from its agents").

3. **`CreateAck` omits `session_id` in `per_run` mode.** design.md's *Creation acknowledgement*
   payload carries `session_id` unconditionally. In `per_run` the session id is a substitution
   template (`schedule:<id>:<aws.scheduler.scheduled-time>`) resolved only at fire time, so the only
   two things the field could carry are the raw template — which looks like a usable session id and
   is not — or nothing. This spec omits it and populates it for `continuous` only, where the value is
   stable and meaningful. Callers that need per-run session ids read them from the run history on
   `GET /api/v1/schedule/{scheduled_task_id}`. Flagged because it narrows a payload the design
   specified as unconditional. See *`ScheduledTaskService`* for the field-by-field acknowledgement,
   which also documents `next_run_at` as best-effort for the same reason: EventBridge supplies no
   next-invocation time and this spec adds no cron-evaluator dependency to synthesize one.
