# #554: Agent Kernel Scheduled Tasks — Implementation Spec

Detailed design for the scheduled-task capability described in [design.md](design.md), which remains
the requirements source: every must/should statement there is traced into a section here. The
change adds one new capability package (`agentkernel/scheduler/`), one optional field pair on the
existing chat request model, one optional echo on the shared response builder, a schedule route
layer per supported deployment target, and outcome recording in the two output consumers. The
one-sentence design idea: **a scheduled task is a stored row plus an EventBridge Scheduler
registration whose target is the existing input queue, so the fire is an ordinary agent message and
the agent runner needs no scheduling awareness.**

Applicability is unchanged from the design: AWS, queue mode, the two scalable deployment styles.
The capability ships with a dedicated example per style —
`examples/aws-serverless/scheduled-openai` and `examples/aws-containerized/openai-scheduled-task` —
rather than switching scheduling on in the pre-existing scalable examples, so the untouched examples
stay byte-identical and the scheduled ones can carry their own config, IAM and smoke tests.

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
├── model.py             # ScheduledTask, ScheduledTaskPage, CreateAck, RunStatus, TaskStatus; re-exports ScheduleSpec/ScheduleMode
├── service.py           # ScheduledTaskService
├── factory.py           # SchedulerFactory: enabled() + validate_config() + build() + service()
├── errors.py            # SchedulerError hierarchy
├── expression.py        # ScheduleExpression — provider-agnostic reading of a ScheduleSpec
├── tools.py             # get_scheduler_tools() → list[SystemTool]
├── testing.py           # InMemoryScheduledTaskStore + SchedulerContract (imports pytest; not re-exported)
├── providers/
│   ├── __init__.py
│   └── aws.py           # AWSScheduler + AWSSchedulerBuilder (EventBridge Scheduler + SQS target)
└── store/
    ├── __init__.py
    ├── base.py          # ScheduledTaskStore ABC, TaskSerializer, PageCursor, ScheduledTaskStoreBuilder
    ├── dynamodb.py      # DynamoDBScheduledTaskStore
    ├── redis_like.py    # shared Redis-protocol store body
    ├── redis.py         # RedisScheduledTaskStore
    └── valkey.py        # ValkeyScheduledTaskStore
```

`agentkernel/api/schedule.py` holds the FastAPI route layer (mirroring `api/thread.py`), keeping the
API surface with the other REST handlers.
`agentkernel/deployment/common/scheduled_run_recorder.py` holds `ScheduledRunRecorder`, the shared
outcome-recording step both output consumers call — it lives under `deployment/common/` because it
is consumer plumbing, not part of the capability's contract.

**`ScheduleExpression` (`expression.py`) is a separate collaborator, not a method on the spec.**
Reading a `ScheduleSpec` — validate, parse a rate into an interval, derive `next_run_at`, test
one-time-ness, normalize to UTC — is needed by the service (before any provider call), by the
provider (when rendering the expression and when completing a one-time task) and by the contract
tests. It takes the minimum granularity as a parameter rather than importing a provider, so nothing
in it knows which timer will run the schedule. Keeping it out of `core/model.py` also keeps
`ScheduleSpec` a plain data shape with only its exactly-one-expression validator.

**`AWSSchedulerBuilder` is split from `AWSScheduler`** for the same reason the stores are split from
`ScheduledTaskStoreBuilder`: the provider takes explicit constructor arguments and never reads
`AKConfig`; the builder is the one place config is read.

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

`message` holds `prompt`, `user_id` (the owner) and `agent` when one was named. It deliberately does
**not** hold `session_id` or `scheduled_run`: only the provider knows how its timer expresses the
fire time, so those two are added when the payload is built at registration (see *AWS provider*).
The row therefore stores the surface-independent half of the message, and a `GET` shows the prompt
and agent that will be delivered rather than a provider-specific template.

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

    @property
    @abstractmethod
    def minimum_granularity(self) -> timedelta:
        """The finest interval this provider's timer supports. Exposed so callers above the
        ABC (the service, before any provider call) can reject a too-fine schedule without
        knowing which provider is in use."""

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
    def remove(self, scheduled_task_id: str) -> None:
        """Physical removal, leaving no tombstone. Idempotent. Used only to roll back a
        creation whose timer registration failed, where a tombstone would block retrying
        the create at the same id."""
    @abstractmethod
    def soft_delete(self, scheduled_task_id: str, deleted_at: datetime, ttl_seconds: int) -> None: ...
```

One concept, one name across all three backends — no per-backend method renames.

`remove` and `soft_delete` are both required and are not interchangeable: `soft_delete` is the
user-facing lifecycle operation, which must leave the row readable by primary key throughout the
grace window for the outcome-write guards; `remove` is the rollback operation, which must leave
nothing behind at all.

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
`owner-index` on `owner_index_key` (S) with sort key `created_at` (S) serves `list_by_owner` and its
cursor. TTL attribute is `expiry_time` — the name `DynamoDBDriver.put` already uses
(`core/util/driver/dynamodb.py:95`).

**The GSI is sparse, and its key is a dedicated mirror attribute rather than `owner_id` itself.**
`put` writes `owner_index_key = owner_id` while the row is live, and `soft_delete` `REMOVE`s that
one attribute. This is how `list_by_owner`'s live-rows-only contract is met on DynamoDB. The
alternative — keeping the index key on tombstones and adding
`FilterExpression=Attr("deleted").ne(True)` — filters *after* the read, so a page of `limit` items
can come back with fewer (or zero) live rows while `LastEvaluatedKey` is still set, forcing the
caller into a paging loop over invisible rows. Removing the attribute drops tombstones out of the
index entirely: no filter expression, no short pages, and no read capacity spent on rows nobody can
see.

**Why a mirror attribute and not `owner_id`:** the row must stay `get`-able by primary key
throughout the grace window (the guards need it) *and* readable enough to answer "who owned this",
so `owner_id` itself is never removed. Removing a separate index key achieves the sparse-index
effect without mutating the row's own data.

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
client init — see *Concurrency contract*):

- `boto3.client("scheduler")` — EventBridge Scheduler
- `boto3.client("sqs")` — used only by the delete path, for `GetQueueAttributes`
- the `ScheduledTaskStore` from `ScheduledTaskStoreBuilder.build()`

The clients are created eagerly; the *call* they make is not. See *Soft-delete TTL derivation*.

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
  "MessageAttributes": {"request_id": {"DataType": "String", "StringValue": "<aws.scheduler.execution-id>"},
                        "user_id":    {"DataType": "String", "StringValue": "<owner_id>"}}
}
```

Angle-bracket tokens prefixed `aws.scheduler.` are EventBridge context variables substituted at fire
time; the rest are baked in at registration. `request_id` is set because **both** runners raise
`ValueError` without it (`containerized/akagentrunner.py:63-64`,
`serverless/akagentrunner.py:53-54`) and both output consumers require it
(`akoutputconsumer.py:158-160`, `akresponsehandler.py:54-56`). Using the execution id makes it unique
per fire. `user_id` is set for the same reason — both runners read it off the record when
propagating attributes to the output queue — and carries the task's owner.

For `ScheduleMode.CONTINUOUS` the `session_id` is the static `schedule:<id>` — no substitution.

**The provider owns the session id, not the service.** `session_id` and `scheduled_run` are added to
the stored `message` here, when the payload is built, because the `per_run` form depends on this
timer's substitution syntax (`<aws.scheduler.scheduled-time>`). Rule #4 — nothing above the ABC is
AWS-aware — would be broken if `ScheduledTaskService` baked an EventBridge token into a row.

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

**Soft-delete TTL derivation.** Derived on first use and cached for the life of the provider:

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
no `AKConfig` field at all. `TTL_SAFETY_MARGIN_SECONDS = 300` and `TTL_FLOOR_SECONDS = 900` are
module constants in `scheduler/providers/aws.py` — they belong with the provider whose queue
semantics they are sized against, not with the config factory.

**The derivation is deferred, not done in `__init__`.** Only `delete()` needs the TTL, and the
output consumers construct a `Scheduler` solely to record run outcomes. Deriving eagerly would make
every one of them call `GetQueueAttributes` on the input queue at startup, which in turn would force
an `sqs:GetQueueAttributes` grant and an input-queue URL onto components that otherwise touch
neither — the response handler is deployed with neither. Deferring keeps the grant table honest
(see *Terraform*). It is derived at most once per process: two threads racing the first read is
harmless, since both would resolve the same value from the same queue attributes.

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

(The two scheduled examples leave `max_receive_count` at the default 3
(`examples/aws-serverless/scheduled-openai/config.yaml:17`,
`examples/aws-containerized/openai-scheduled-task/config.yaml:17`) while the queue's Terraform
default is 5, so the two sources genuinely differ there and the max is doing real work. The older
`openai-dynamodb-scalable` example hardcodes `4`, which happens to equal its injected `5 - 1`; that
agreement is coincidental and does not generalize.)

If `GetQueueAttributes` fails, the delete raises `SchedulerError` rather than falling back to a
guessed TTL. Correctness does not depend on the number: the `scheduled_task_version` guard
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

    def create(self, *, spec: ScheduleSpec, prompt: str, agent: str | None, owner_id: str,
               request_id: str | None = None) -> CreateAck
    def update(self, scheduled_task_id: str, *, owner_id: str, spec: ScheduleSpec | None,
               prompt: str | None, agent: str | None) -> ScheduledTask
    def delete(self, scheduled_task_id: str, *, owner_id: str) -> None
    def get(self, scheduled_task_id: str, *, owner_id: str) -> ScheduledTask
    def list(self, *, owner_id: str, limit: int | None, cursor: str | None) -> ScheduledTaskPage
```

Responsibilities, in `create` order:

1. **Validate** the `ScheduleSpec` via `ScheduleExpression.validate(spec, minimum_granularity)`
   (exactly one expression; `at` in the future; granularity; no provider-native wrapper). The
   granularity comes off the ABC, so this stays provider-agnostic.
2. **Generate the id** when `spec.id` is absent: `f"schedule_{uuid4().hex}"`.
3. **Resolve the incarnation.** `scheduler.get(id, include_deleted=True)`:
   - no row → fresh `scheduled_task_version = uuid4().hex`
   - live row owned by `owner_id` → upsert, **retaining** the existing version *and* `created_at`,
     so in-flight runs still record their outcomes and the row keeps its original creation time
   - live row owned by someone else → `SchedulerPermissionError` → 403
   - soft-deleted row → `SchedulerConflictError` → 409 (deletion is terminal; the id frees up when
     the TTL expires)
4. **Build the message template** — `prompt`, `user_id`, and `agent` when one was named. The
   session id is **not** resolved here: it depends on how the target timer expresses the fire time,
   so the provider fills it in at registration (see *AWS provider*). The reserved `schedule:` prefix
   travels with it.
5. **Stamp the owner** into the row and into `message["user_id"]`. `owner_id` is a parameter
   resolved by the caller from the authenticated identity; the service never reads it from a request
   body, so it cannot be forged or overridden.
6. **Call `scheduler.upsert(task)`** and return the acknowledgement.

`update` does not create: a missing live row raises `SchedulerNotFoundError` → 404. It retains
`scheduled_task_version`. A `PUT` on a one-time task that has already run — `status` is `COMPLETED`,
or its `at` has simply elapsed without the outcome being recorded — must carry a new future `at`;
without one it raises `ScheduleValidationError` → 400, since re-registering the elapsed instant
would be rejected as a schedule in the past. Given the new instant it re-arms: `status` back to
`ACTIVE`, `completed_at` cleared, version retained. A replacement `schedule` that omits `mode` keeps
the task's current mode, so retiming a `continuous` task never moves it to a `per_run` session id.
Updates affect future executions only; an already-enqueued fire continues with the definition it was
enqueued with.

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
| ECS REST | generated — the surface passes none, so the service falls back to `str(uuid.uuid4())`, the same shape `enqueue_and_wait` uses (`rest_handler.py`) |
| Serverless REST | `payload.request_id` from the API Gateway envelope, generated when absent |
| ECS WebSocket, serverless WebSocket | `BaseRequest.request_id` from the inbound frame when present, generated otherwise |

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
        """validate_config(), then construct the provider via AWSSchedulerBuilder.
        Memoized per process. Raises on failure."""

    @staticmethod
    def service() -> Optional[ScheduledTaskService]:
        """A ScheduledTaskService over the configured scheduler, or None when disabled.
        The one place every surface obtains the service, so no route layer repeats the
        enabled check plus construction."""
```

`validate_config()` enforces, in order:

1. `scheduler.enabled` — when false, return immediately (nothing else is checked).
2. `session.type` is one of `dynamodb`, `redis`, `valkey` — scheduling needs a durable store shared
   by all replicas.
3. `scheduler.group_name` and `scheduler.target_role_arn` are non-empty. An **empty string counts as
   unset**, because the examples declare these as `""` placeholders that Terraform fills via
   `AK_SCHEDULER__*` (see *Terraform*); a deployment that enables scheduling in YAML without the
   Terraform wiring must fail here rather than at the first `create_schedule` call.
4. **The backend block matching `session.type` is present and non-empty**, and no *other* backend
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

**Queue mode and the FIFO input queue are not checked here.** Both are genuine preconditions, but
neither is observable from `AKConfig` in every scheduler-enabled component: each serverless Lambda
is given only the queue URL it publishes to, so the response handler legitimately has no input URL
and the request handler legitimately has no output URL. A URL-presence check would therefore reject
a correctly-wired deployment. Both preconditions are enforced at deploy time instead — a
`validation` block requiring `queue_mode = true`, and queue modules that create a FIFO input queue
(see *Terraform*). This is a change from an earlier draft of this spec, which checked both URLs and
the `.fifo` suffix; `test_scheduler_config.py` pins the current behaviour with an explicit
"missing queue URL is accepted" case so the check is not reintroduced by accident.

Timing: "at initialization" means process startup on ECS and cold start on serverless — in both
cases before the first request or scheduled execution is processed. Concretely:

| Component | Call site |
|---|---|
| ECS REST service | `RestHandler.__init__` (`deployment/common/rest_handler.py`), constructed before uvicorn starts by `AWSRestAPI.run()`, which `ECSIOHandler.run()` invokes (`ecs_io_handler.py:42-46`). `ScheduleRESTRequestHandler.__init__` reaches it transitively through `SchedulerFactory.service()` |
| ECS WebSocket service | `ECSWebSocketRequestHandler.__init__`, constructed before uvicorn starts by `AWSWebsocketAPI.run()` (`ecs_io_handler.py:34-40`). `validate_config()` and `SchedulerFactory.service()` only — **not** the `Authoriser` check, which is REST-only |
| ECS output consumer | the first statement of `ECSOutputConsumer.run()`, before the polling loop starts |
| ECS agent runner | `SchedulerFactory.service()` from a tool invocation, reached only when the tool set is registered |
| Serverless request handler | `DefaultEndpointsHandler.__init__` (`serverless/core/router/rest_lambda.py`) and `SystemRoutesHandler.__init__` (`ws_lambda.py`) |
| Serverless response handler | the first statement of `ResponseHandler.handle()`, before the batch is processed |

**Why the two output consumers validate in `run()` / `handle()` rather than in the class body.**
`agentkernel.aws` re-exports every deployment class, so a class-body check fires on *import* — which
means an entry point that imports the package for an unrelated component (an agent runner, say)
would assert on wiring only the scheduler-enabled components are given, and a deployment that omits
those env vars from one function would fail at import in the wrong process. Moving the call to the
first statement of the entry point keeps the failure loud and still ahead of any recorded outcome,
while scoping it to the component that actually needs the wiring.

`build()` is memoized per process (a module-level singleton behind a `threading.RLock`, the
`ConversationThreadManager.get()` pattern) so the boto3 clients and the store are created once per
process, not per request. `service()` is a thin wrapper over it and is what every surface calls.

### Core model changes — `core/model.py`

```python
SCHEDULED_SESSION_PREFIX = "schedule:"

# Volatile-cache key under which ChatService binds the request's authenticated user id to
# the session, so tool code can resolve a trustworthy owner. See Agent-callable tools.
REQUEST_USER_ID_KEY = "ak.request.user_id"

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

`BaseRunRequest` (`core/model.py:309-316`) gains two optional fields, both defaulting to `None`, so
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
(`core/chat_service.py:118-145`) turns every request field not in `known_fields` into an
`AgentRequestAny` handed to the agent. Its pre-change set enumerated exactly
`BaseRunRequest`'s declared fields. Without adding the two new names, a scheduled fire would push its
own `scheduled_run` block into the agent's request list as opaque context:

```python
known_fields = {"request_id", "user_id", "group_id", "thread_name", "prompt", "agent",
                "session_id", "images", "files", "schedule", "scheduled_run"}
```

This is a required change, not an optimization, and it is covered by a dedicated test.

**`ResponseBuilder.build_response`** (`core/chat_service.py:311-347`) gains one optional parameter
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
at all six call sites (success and both error paths in each). The block is read through
`ChatService._scheduled_run(req)`, a `getattr` helper, because the multipart request shape does not
declare the field. This is a generic pass-through of an optional block, not scheduling logic, and it
is the only change on the response side. It runs before the `HTTPException` raise, so an errored
scheduled run still carries its correlation metadata.

**The request's authenticated user is bound to the session.**
`AgentHandler.bind_request_user(req.user_id)` writes `REQUEST_USER_ID_KEY` into the session's
volatile cache, called from all four execution entry points (`execute`, `execute_sync`, and both
stream variants). `user_id` is deliberately kept out of the agent's request context, so this is how
tool code reaches a trustworthy identity — see *Agent-callable tools*. It is a no-op when the
request carries no user, so ordinary traffic is unaffected.

**Thread auto-creation is skipped for scheduled sessions.** The check lives in `ThreadRecorder`
(`integration/thread/recorder.py`), the presentation-layer wrapper that owns thread bookkeeping
around a `ChatService` run — not in `ChatService` itself, which stays the execution core and holds
no thread policy. `pre_run` and `post_run` both return early when `req.session_id` starts with the
reserved `schedule:` prefix, so a scheduled run creates no thread and appends no message and never
appears in the owner's thread listings. Returning from `pre_run` **before** the `user_id` check is
deliberate: a scheduled run has no user turn to record at all, so the requirement is moot rather
than satisfied. The prefix constant (`SCHEDULED_SESSION_PREFIX = "schedule:"`) lives in
`core/model.py` next to `ScheduledRunMetadata`, so neither `core/` nor `integration/` needs an
import from `scheduler/` for this check. This is fixed behaviour, not configurable.

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
scratch without touching the record body (`containerized/akagentrunner.py:122-133`;
`serverless/akagentrunner.py:153-181`). Each now extracts the block best-effort and echoes it:

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
(`serverless/akagentrunner.py:170`); its ECS twin does not. For a scheduled fire the group id is the
`scheduled_task_id`, not a session id, so that line would write a wrong `session_id` into the error
body. **Resolution:** when `scheduled_run` is present, the serverless runner takes `session_id` from
the parsed body instead — via a small `_parse_session_id(record)` helper that tolerates an
unparseable body — and omits the key when it cannot be read. Non-scheduled behaviour is unchanged.

#### Stream runners — a scheduled fire is a non-stream execution

Both stream runners **do** receive fires, and both must route them to the non-stream path.

It is tempting to reason that a fire is never produced in `stream` mode because the acknowledgement
is delivered at creation time and nothing is enqueued for a stream (see *Creation acknowledgement*).
That conflates two moments: the *ack* is produced at create time, but the *fire* is produced later
by the timer, which knows nothing about `execution.mode`. In a stream deployment the input-queue
consumer **is** the stream runner — `ECSAgentRunner.run()` dispatches to `ECSStreamAgentRunner` when
`execution.mode == STREAM` (`containerized/akagentrunner.py:141-142`), and
`ServerlessAgentRunner.handle()` does the same (`serverless/akagentrunner.py:27-29`) — so every fire
lands there.

Left alone, a fire cannot run *or* be recorded, for two independent reasons:

1. Both stream runners require `endpoint_url` in `_get_record_attributes`
   (`containerized/akagentrunner.py:174-177`, `serverless/akagentrunner.py:224-225`), and a timer
   payload carries only `request_id` and `user_id` — there is no client connection at registration
   time to name one. Every fire therefore raises on receive and exhausts its retries.
2. `on_permanent_failure` calls `_get_record_attributes` as its first statement inside its own
   `try`, so it raises the identical error and publishes nothing. Even with an `endpoint_url`, the
   stream failure path emits a `StreamChunk`, which has no field to carry the `scheduled_run` block
   the output consumers record outcomes from. The outcome is never written and `last_run_*` stays
   stale forever, while the row still reads `status: ACTIVE` — indistinguishable from a schedule
   whose first fire has not arrived.

**Resolution:** a scheduled fire is treated as an ordinary non-stream execution. Each stream runner
detects one by the presence of a `scheduled_run` block in the record body — the same signal the
output consumers already fan out on, ahead of their own execution-mode branch — and delegates the
whole record to the non-stream implementation, whose response echoes that block and is therefore
recordable. This is also the only correct destination: `process_chat_request` passes `scheduled_run`
into `ResponseBuilder.build_response` (`chat_service.py:489-492`), whereas the streaming generators
yield `StreamChunk`s that cannot carry it.

The delegation names the non-stream class outright rather than going through `super()`. This matters
on ECS, where `ECSStreamAgentRunner` *is* a subclass: `super().process_message(record)` would leave
`cls` bound to the stream class, so `cls._get_record_attributes` would still resolve to the override
demanding `endpoint_url` — the very thing being avoided. Naming `ECSAgentRunner` binds `cls` to it
and picks up its `endpoint_url`-optional extraction and whole-response send. On serverless
`ServerlessStreamAgentRunner` is a *sibling* of `ServerlessAgentRunner`, so explicit naming is the
only option there anyway; the two platforms end up with the same shape.

Interactive streaming is untouched: the branch keys off the `scheduled_run` block alone, never off a
missing `endpoint_url`, so an ordinary stream request keeps both its chunk fan-out and its
`endpoint_url` requirement.

#### Output consumers — the only readers of `scheduled_run`

Both consumers gain the same branch. The recognition-and-record step itself is **written once**, as
`ScheduledRunRecorder` in `deployment/common/scheduled_run_recorder.py`, so the two targets cannot
drift; each consumer contributes only the call site and its early return. The presence of a
`scheduled_run` block in the response body is exactly how a consumer tells a scheduled run from an
ordinary one; the outcome status is derived from the ordinary response shape — an `error` key means
`FAILED`, otherwise `COMPLETED` — so no scheduling-specific status or error field is introduced.

```python
class ScheduledRunRecorder:
    @classmethod
    def record(cls, body: Any) -> bool:
        """Record the outcome when the body carries a scheduled_run block.
        Returns True when this was a scheduled run and the consumer should stop here."""

    @classmethod
    def record_before_discard(cls, raw_body: Any) -> bool:
        """Last-chance record from on_permanent_failure. Never raises."""
```

```python
# in both consumers, before the existing broadcast/store logic
if ScheduledRunRecorder.record(record.get("body")):
    return  # not broadcast, not written to the response store
```

The recorder returns a `bool` rather than the parsed block because that is all a consumer needs: the
only decision it makes is whether to stop. It holds **no** outcome-write policy — it derives the
status from the body and forwards a single `Scheduler.mark_run_completed` call; the guards, the
field updates and the one-time `COMPLETED` transition all live inside the provider.

**`ResponseHandler.process_message`** (`serverless/akresponsehandler.py:109-135`). The branch goes
first, before the execution-mode fan-out. This is necessary, not cosmetic: in `async`/`stream` mode
the handler broadcasts to the originating connection using an `endpoint_url` message attribute
(`:80-107`, dispatched at `:127-130`) that a timer-originated message does not carry, so without the
branch a scheduled response raises `ValueError("endpoint_url is required in SQS message
attributes")`. In the REST modes it would write to the response store, where nobody is polling for
it.

**`ECSOutputConsumer.process_message`** (`containerized/akoutputconsumer.py:72-100`) gets the same
branch, and for the same reason rather than merely for symmetry: in WebSocket modes it too broadcasts
using an `endpoint_url` message attribute and raises without one. The existing test
`test_broadcast_via_websocket_raises_when_endpoint_url_missing`
(`tests/test_ecs_akoutputconsumer.py:51`) is that failure, already pinned. So the branch must go
**first** on this consumer as well, before the mode fan-out. The design describes this as the one
branch the feature adds to an existing component; it is one branch per target, symmetric in both.

**`on_permanent_failure` on both consumers gains the same branch, via `record_before_discard`.** An
earlier draft of this spec left it unchanged, reasoning that a second store write would double the
failure modes. That was wrong on the facts: the consumers' retry limit is deliberately **one below
the output queue's** (`max(1, output_queue_max_receive_count - 1)`), so a message reaching
`on_permanent_failure` is swallowed and deleted, **not** dead-lettered. Without this call the
outcome is lost outright and the row keeps a stale `last_run_*` forever — the run is not "visible as
no update", it is indistinguishable from one that never fired.

Three properties keep that write safe:

- **It never raises.** Extraction goes through `from_raw_body`, and the write itself is wrapped —
  a failure is logged with the full `scheduled_run` identity, which is then the run's only surviving
  trace.
- **The status still comes from the body**, not from the fact that this is the failure path. A body
  reporting a result describes a run the agent completed where only the *recording* failed;
  inventing `FAILED` there would report a broken task to a caller whose agent ran fine.
- **It returns early for one more reason than `process_message` does.** A fire's group id is the
  `scheduled_task_id`, so the existing error-entry path would file the failure under a session that
  does not exist.

Additionally, `ResponseHandler.on_permanent_failure` now reads the session id from the record's
**system** attributes (`MessageGroupId`) instead of `message_attributes["message_group_id"]`, and
omits the key when absent. The custom-attribute map never contained that key, so the pre-change line
raised `KeyError` on every permanent failure — a latent bug this path had to touch anyway.

Both consumers depend only on the `Scheduler` interface. Neither resolves, imports or calls the
`ScheduledTaskStore`, and neither holds any outcome-write policy — loading the row, applying the four
guards, updating the fields and setting `status = COMPLETED` all happen inside the `Scheduler`. The
consumer's only job is to recognise the block and forward it. Consequence: the guard rules live in
one place, are shared by both targets, and a non-AWS provider can change how outcomes are persisted
without touching either consumer.

#### Chat create path — ECS

`RestHandler` (`deployment/common/rest_handler.py:20`) — the shared queue-aware REST base that
`ECSQueueRequestHandler` extends — gains an optional `authoriser` constructor parameter and a
schedule branch. It also mixes in `BearerIdentityMixin` for `_resolve_user`:

```python
class RestHandler(BearerIdentityMixin, AgentRESTRequestHandler):
    def __init__(self, logger_name: str = "ak.deployment.queue_handler", authoriser: Optional[Authoriser] = None):
        super().__init__()
        self._log = logging.getLogger(logger_name)
        self._config = AKConfig.get()
        self._authoriser = authoriser
        SchedulerFactory.validate_config()
        if SchedulerFactory.enabled() and authoriser is None:
            raise AKConfigError("scheduler.enabled requires an Authoriser on the chat route — "
                                "every scheduled task must have an authenticated owner")
        self._schedule_service = SchedulerFactory.service()   # None when disabled
```

**Why the shared base rather than `ECSQueueRequestHandler`.** `RestHandler` lives under
`deployment/common/`, which is provider-agnostic, and `ECSQueueRequestHandler`
(`containerized/core/api/rest_api.py:12`) is its only subclass today — so this is a real placement
decision, not a default. It goes in the base because the branch contains nothing AWS-specific: it
calls `ScheduledTaskService`, which rule #4 keeps provider-agnostic, and never names EventBridge,
SQS or boto3. A future queue-mode target inheriting `RestHandler` gets the create path for free. On
any deployment with no `scheduler` block `SchedulerFactory.enabled()` is `False`,
`_schedule_service` is `None`, and `enqueue_and_wait` behaves exactly as it does today.

In `POST /api/v1/chat` — the route declared by `AgentRESTRequestHandler.get_router()` and served by
`RestHandler.enqueue_and_wait` — the branch is the first statement inside the existing `try`,
**before** the `session_id` check (`rest_handler.py:79`) and before
`request_id = str(uuid.uuid4())` (`:84`). It must precede the `session_id` check because a
scheduled create legitimately has no session id — the service derives one. `enqueue_and_wait` gains
a second parameter, `request: Request = None`, so the branch can resolve the caller:

```python
if body.schedule is not None:
    return await self._create_scheduled_task(body, request)

# _create_scheduled_task:
if self._schedule_service is None:
    raise HTTPException(status_code=400, detail="Scheduling is not enabled for this deployment")
owner_id = self._resolve_user(request)              # 401 when the token is missing or rejected
try:
    ack = self._schedule_service.create(spec=body.schedule, prompt=body.prompt,
                                        agent=body.agent, owner_id=owner_id)
except ScheduleValidationError as e:                # 400 — bad input, not a server fault
    raise HTTPException(status_code=400, detail=str(e))
except SchedulerPermissionError as e:               # 403 — a live row owned by someone else
    raise HTTPException(status_code=403, detail=str(e))
except SchedulerConflictError as e:                 # 409 — the id is soft-deleted
    raise HTTPException(status_code=409, detail=str(e))
return JSONResponse(status_code=201, content=ack.model_dump(mode="json", exclude_none=True))
```

The three `except` clauses are explicit rather than routed through the generic handler below them,
which turns any unclassified exception into a 500: a bad schedule expression and a foreign id are
caller errors and must not read as server faults.

Nothing is enqueued: the first message on the input queue appears when the timer fires. In
`rest_sync` the handler does **not** wait on the response store — there is no run to wait for, so
the sync wait is skipped entirely. In `rest_async` the same 201 body is returned;
`GET /api/v1/chat/{session_id}` is not used for scheduling, and run outcomes are read from
`GET /api/v1/schedule/{scheduled_task_id}`. `_resolve_user` is the 401-on-missing/invalid-Bearer
helper that used to live on `ThreadRESTRequestHandler`, lifted verbatim into `BearerIdentityMixin`
(`api/handler.py`) so the thread, schedule and chat-create routes cannot drift.

**The `Authoriser` ABC moves with it**, from `integration/thread/authoriser.py` to
`agentkernel/auth/authoriser.py` and re-exported from `agentkernel.auth`. It was never
thread-specific — it resolves a Bearer token to a subject — and the schedule routes are now a second
consumer. `integration/thread/authoriser.py` becomes a one-line re-export so
`agentkernel.thread.Authoriser` and `agentkernel.integration.thread.Authoriser` keep working as
documented. The one contract difference is stated on the ABC: with no `Authoriser` configured thread
routes remain open, while the schedule routes require one.

`ECSQueueRequestHandler.__init__` (`containerized/core/api/rest_api.py:15-21`) forwards the
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
# in _handle_chat, after the `ctx.message.body is None` check,
# before the `session_id is required` check
if ctx.message.body.schedule is not None:
    return await self._create_scheduled_task(ctx)

# _create_scheduled_task:
if self._schedule_service is None:
    return self.build_error_http_response(400, "Scheduling is not enabled for this deployment", user_id=ctx.user_id)
try:
    ack = self._schedule_service.create(
        spec=ctx.message.body.schedule, prompt=ctx.message.body.prompt,
        agent=ctx.message.body.agent, owner_id=ctx.user_id, request_id=ctx.message.request_id,
    )
except (SchedulerError, ValueError) as e:
    return self.build_error_http_response(400, str(e), user_id=ctx.user_id)
await self._broadcast_ack(ack, ctx)
return self.build_success_http_response("Request scheduled successfully", user_id=ctx.user_id, status_code=201)
```

The acknowledgement goes out on the connection *and* the route returns a 201 envelope: the broadcast
is the payload the client consumes, the return value is the frame-handling result this route
already produces for every other branch. `SchedulerError` is caught as one family here rather than
per-subclass, because a WebSocket frame has no status-code vocabulary to map 403/404/409 onto — the
message text carries the distinction.

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

`_maybe_schedule` returns `None` when `payload.body.schedule` is absent, raises a 400-mapped
`ValueError` when scheduling is disabled, and otherwise resolves the owner from
`event["requestContext"]["authorizer"]["principalId"]` — the value `APIGatewayAuthorizer._build_policy`
sets from `ValidationResult.subject` (`serverless/akauthorizer.py:75-92`). It returns an explicit
`(201, ack)` pair, which is why `_handle_request` now accepts either a bare body (answered 200, the
pre-change shape) or a `(statusCode, body)` tuple.

Identity enforcement differs from ECS here (see *Deviations from design.md* #1, now reflected in
design.md). Python cannot observe whether Terraform attached the API Gateway authorizer to the
route, so the check cannot be an initialization check on serverless. It is enforced per request: a
`schedule` block with no authorizer context on the event raises `UnauthenticatedScheduleError` — a
dedicated exception type, so it maps to **401** rather than being swallowed by the generic 400 or
500 handlers — and the Terraform module attaches the authorizer to the schedule routes.

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
| `PUT /api/v1/schedule/{scheduled_task_id}` | Body may change the `schedule` block and the message fields (`prompt`, `agent`). Calls `Scheduler.upsert`: the row is written and the registration replaced, so the next fire reflects the new definition. Version retained; a `schedule` omitting `mode` keeps the current one; a one-time task that has already run is re-armed only when the body carries a new future `at`. | 400 invalid/too-fine or a run-out one-time task with no new `at`, 401, 403 not owner, 404 no live row, 409 soft-deleted |
| `DELETE /api/v1/schedule/{scheduled_task_id}` | Removes the registration, then soft-deletes the row. Idempotent. | 401, 403 not owner |

All routes require the configured identity resolver; update and delete additionally check ownership.

**ECS mounting.** `agentkernel/api/schedule.py` defines
`ScheduleRESTRequestHandler(BearerIdentityMixin, RESTRequestHandler)`:

```python
def __init__(self, authoriser: Optional[Authoriser] = None, service: Optional[ScheduledTaskService] = None):
    if authoriser is None:
        raise AKConfigError("scheduler.enabled requires an Authoriser on the schedule routes — "
                            "every scheduled task must have an authenticated owner")
    self._authoriser = authoriser
    self._service = service or SchedulerFactory.service()
```

**`authoriser` is typed `Optional` but is not optional.** Making it a required positional parameter
would move the failure to a `TypeError` from Python's argument binding — an error that reads as a
programming mistake in the framework rather than a deployment misconfiguration. The `Optional` +
explicit-raise shape produces the same loud failure at the same moment, with a message that names
the actual problem and matches the equivalent check on `RestHandler`. `service` is injectable for
tests and resolved from config otherwise.

`RESTAPI.run()` auto-mounts the handler when `scheduler.enabled`, unless the caller supplied one —
the exact shape of the existing thread auto-mount (`api/http.py:105-112`), resolved in
`_get_schedule_router` so `run()` stays flat:

```python
if not SchedulerFactory.enabled():
    return None
if not cls._auto_mount_schedule_routes:      # subclass opt-out; see below
    return None
if any(isinstance(h, ScheduleRESTRequestHandler) for h in handlers):
    return None
return ScheduleRESTRequestHandler().get_router()
```

`ScheduleRESTRequestHandler()` with no `Authoriser` raises `AKConfigError` at construction — the
loud initialization failure the design requires, before uvicorn binds. So an application that
enables scheduling and never supplies a handler fails to start, rather than serving routes that
cannot establish an owner: supplying the handler through the `handlers` list *is* how the
`Authoriser` is provided.

**The opt-out exists because that loud failure is only correct on a REST surface.**
`AWSWebsocketAPI` inherits `RESTAPI.run()` and calls it as `super().run(handlers=cls.get_default_handlers())`,
whose handler list can never contain a `ScheduleRESTRequestHandler` — `run()` takes no `handlers`
argument there, so an application has no way to supply one. Left unguarded, the auto-mount would
therefore construct an `Authoriser`-less handler and make **every** scheduling-enabled ECS WebSocket
deployment fail to boot, which is exactly the outcome behavioural change #8 rules out one layer
down. `RESTAPI` declares `_auto_mount_schedule_routes = True` and `AWSWebsocketAPI` overrides it to
`False`: a WebSocket deployment creates schedules through its chat route and manages them from a
separate REST service, so it never serves these routes at all.

Route bodies are thin: each resolves the owner via `_resolve_user`, calls one service method, and
runs inside `_mapped_errors()` — a small context manager holding the single
`{SchedulerNotFoundError: 404, SchedulerPermissionError: 403, SchedulerConflictError: 409,
ScheduleValidationError: 400}` table, so the mapping is declared once rather than per route.

**Serverless mounting.** The serverless REST surface is not FastAPI, so `ScheduleEndpointsHandler`
(`serverless/core/router/schedule_lambda.py`) mirrors `api/schedule.py`'s behaviour against the
hand-rolled route table, carrying its own copy of the same status mapping plus a 401 when the
authorizer context is missing and a 404 when scheduling is disabled. `RESTLambdaRouter.__init__`
merges its routes in when `SchedulerFactory.enabled()`.

`RESTLambdaRouter` (`serverless/core/router/rest_lambda.py`) keeps a hand-rolled
`{path: {method: handler}}` table and dispatches on an exact string lookup of the resolved path,
with no path-parameter support. `DELETE /api/v1/schedule/{id}` therefore cannot match today.

`dispatch` gains a **resource-template fallback**, applied only where it currently raises:

```python
handler = methods.get(method)
if not handler:
    # API Gateway supplies the matched resource template and its extracted parameters.
    handler = self._resolve_by_resource_template(event, method, env_base_path)
if not handler:
    raise ValueError(...)   # unchanged

# _resolve_by_resource_template:
resource = event.get("resource")
if not resource or not env_base_path:
    return None
return self._routes.get(resource.removeprefix(env_base_path), {}).get(method)
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

**Owner binding.** A tool cannot set an arbitrary owner. `ToolContext.get().session` identifies the
invoking session, and the owner is read from that session's **volatile cache** under
`REQUEST_USER_ID_KEY`, which `AgentHandler.bind_request_user` wrote from the request's `user_id` at
the start of the run (see *Core model changes*).

The volatile cache is the seam because the obvious alternative does not exist: `user_id` is in
`known_fields`, so `_attach_additional_context` never turns it into an `AgentRequestAny` — it is
deliberately kept out of the agent's request context, and adding it there to serve this feature
would leak the caller's identity into every prompt. The session is already the per-request object
tool code can reach, and the *volatile* half is right because the binding must not outlive the
process or be persisted with the session's durable state.

On a scheduled fire the bound user is the fire's own `user_id`, i.e. the task's owner, so a task
created from a scheduled run stays with the same person. There is no synthetic agent identity and no
ownership handover: the agent is the mechanism, the human remains the principal. When no
authenticated `user_id` is resolvable — no `ToolContext`, no session, or nothing bound — the tool
returns `{"error": "no authenticated owner available for this session"}` rather than creating an
unowned task.

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
                    "sparse GSI 'owner-index' on 'owner_index_key' (S) / 'created_at' (S), TTL attribute "
                    "'expiry_time'. Never a partition of the session or response-store table.")

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
    region: Optional[str] = Field(default=None,
        description="AWS region for the scheduler and its table; defaults to the boto3 environment default")
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
  description = "Enable scheduled tasks: creates the scheduled-task table, EventBridge Scheduler schedule group, timer execution role, component IAM grants, and the /schedule API Gateway routes. Requires queue_mode = true."
  type        = bool
  default     = false

  validation {
    condition     = !var.scheduled_task || var.queue_mode
    error_message = "scheduled_task = true requires queue_mode = true."
  }
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

`scheduled_task = true` with `queue_mode = false` is rejected by the `validation` block on
`scheduled_task` itself, matching the existing `scaling_config.enabled requires queue_mode = true`
precondition (`containerized/variables.tf:396-401`). **This is where the queue-mode precondition is
enforced, not in `validate_config()`** — see *`SchedulerFactory`* for why the application cannot
check it. The deploy fails before the app is ever built.

#### Resources created when `scheduled_task = true`

A new `modules/scheduler/` per target, gated `count = var.scheduled_task ? 1 : 0`
(`containerized/scheduled_task.tf`, `serverless/scheduled_task.tf`).

1. **Scheduled-task table.** `aws_dynamodb_table` — partition key `scheduled_task_id` (S), sparse
   GSI `owner-index` on `owner_index_key` (S) / `created_at` (S), TTL on `expiry_time`. Created only
   when the deployment's session store is DynamoDB; when sessions are Redis/Valkey the module
   provisions **nothing** here, because the existing cluster is reused with a separate keyspace. The
   module takes that decision from `create_dynamodb_memory_table` — the same variable the deployment
   already uses to pick its session backend — rather than a scheduler-specific one, so the two can
   never disagree.
2. **`aws_scheduler_schedule_group`** — one per deployment, named from
   `scheduled_task_config.schedule_group_name`. It exists for namespacing and destroy-time cleanup:
   deleting the group removes every registration the deployment created, so `terraform destroy`
   leaves no orphaned schedules behind.
3. **Timer execution role** — `aws_iam_role` assumable by `scheduler.amazonaws.com`, with a single
   inline policy allowing `sqs:SendMessage` on the input queue **only**.
4. **Component IAM grants** — attached to the existing task/execution roles, deliberately unequal so
   no component gets more than its role needs:

| Component | Table | EventBridge Scheduler | Other |
|---|---|---|---|
| REST service / request handler | `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query`, `DescribeTable` (table + `/index/*`) | `CreateSchedule`, `UpdateSchedule`, `DeleteSchedule`, `GetSchedule` on the schedule ARN pattern | `iam:PassRole` on the timer role; `sqs:GetQueueAttributes` on the input queue |
| Response handler | `GetItem`, `UpdateItem`, `DescribeTable` | **none** — it never registers or removes a schedule | **none** — it never derives the TTL |
| Agent runner | none, unless `enable_agent_tools = true` → the full set | none, unless `enable_agent_tools = true` → the full set | only when `enable_agent_tools = true` |

Three details the table encodes:

- **`Resource` is the schedule ARN pattern, not the schedule group ARN.** A schedule is not
  addressed as a child of its group: the group is `…:schedule-group/<group>` while a schedule is
  `…:schedule/<group>/<name>`. A grant scoped to `<group-arn>/*` matches no schedule at all and
  denies every scheduler call. The module derives the correct pattern and exports it as
  `schedule_arn_pattern`.
- **`iam:PassRole` is required by every component that registers a schedule**, because registering
  hands EventBridge Scheduler the role it assumes to deliver the fire. It is scoped to that one
  role.
- **The response handler gets no `sqs:GetQueueAttributes`**, and is given no input-queue URL either.
  Only the delete path derives the TTL, and it is derived lazily, so a component that merely records
  outcomes never makes the call. This is the grant the deferred derivation buys.

**On containerized the REST task carries the response-handler role too**, because it hosts both the
schedule routes and the output-consumer thread in one task. It therefore gets the full grant rather
than a split one — the split above is real on serverless, where the two are separate Lambdas.

The agent runner's grants sit behind `scheduled_task_config.enable_agent_tools` because the tools
are opt-in in the application too (`scheduler.agents: []` scopes them away). The same flag also
gates the `AK_SCHEDULER__*` env vars on the runner, so a deployment that enables scheduling but not
the agent tools leaves the runner with neither the permissions nor the configuration — the Terraform
gate and the app-level gate line up, so neither grants access the other doesn't.
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

The module additionally exports `schedule_arn_pattern` for the component IAM grants. It is not a
root output — nothing outside the deployment consumes it — but it is the value every scheduler grant
is scoped to (see above).

**Backend-specific env vars are injected conditionally, never as a null, and never more than one
at a time.** Only the block matching the deployment's session backend is injected — the DynamoDB
table name is *not* set on a Redis/Valkey deployment, and the keyspace prefix is not set on a
DynamoDB one. This follows the conditional-merge shape the session store env vars already use
(`containerized/modules/rest-service/main.tf:1-25`, e.g.
`var.dynamodb_memory_table_arn != null ? { AK_SESSION__DYNAMODB__TABLE_NAME = ... } : {}`), with one
difference that matters: **the gate is the session backend, not the mere existence of a store.**
Each module therefore resolves exactly one backend first, in the same precedence the scheduled-task
table's own gate follows, and keys all three blocks off that:

```hcl
scheduler_store_backend = (
  var.scheduled_task_table_name != null ? "dynamodb" :
  var.redis_url != null ? "redis" :
  var.valkey_url != null ? "valkey" : null
)

scheduler_environment = var.scheduled_task ? merge(
  {
    AK_SCHEDULER__ENABLED         = "true"
    AK_SCHEDULER__GROUP_NAME      = var.scheduled_task_schedule_group_name
    AK_SCHEDULER__TARGET_ROLE_ARN = var.scheduled_task_target_role_arn
  },
  local.scheduler_store_backend == "dynamodb" ? { AK_SCHEDULER__DYNAMODB__TABLE_NAME = var.scheduled_task_table_name } : {},
  local.scheduler_store_backend == "redis" ? { AK_SCHEDULER__REDIS__PREFIX = "ak:scheduled_tasks:" } : {},
  local.scheduler_store_backend == "valkey" ? { AK_SCHEDULER__VALKEY__PREFIX = "ak:scheduled_tasks:" } : {},
) : {}
```

Gating a prefix on a URL being non-null is *not* equivalent, and on serverless it is wrong: there,
`local.redis_url` is non-null when `create_redis_cluster || create_redis_response_store`, so a
supported combination — DynamoDB sessions plus a Redis *response store* — would inject
`AK_SCHEDULER__DYNAMODB__TABLE_NAME` and `AK_SCHEDULER__REDIS__PREFIX` together. Two populated
blocks is precisely what check #4 rejects: `SchedulerFactory._validate_backend_block` raises
`AKConfigError` for a populated block that does not match `session.type`, so every
scheduler-enabled component would crash-loop at startup. The serverless root therefore resolves the
backend from the *session* variables directly
(`var.create_dynamodb_memory_table` → `var.create_redis_cluster` → `var.create_valkey_cluster`),
which is the same input the scheduled-task table is already gated on.

Injecting a `null` is not merely untidy: the ECS `environment` map and Lambda `environment.variables`
both reject null values, so the unconditional form would fail `terraform apply` on every
Redis/Valkey deployment. The conditional form also keeps the running container's environment honest
— an operator reading it sees exactly the one backend that is in use.

**Every component that registers a schedule is given the input queue URL.** The timer's target is
the input queue, so a registration built without `execution.queues.input.url` carries a blank
`QueueUrl`; EventBridge Scheduler accepts it (a universal target's `Input` is an opaque string) and
fails only at fire time. On serverless the request handler already receives
`AK_EXECUTION__QUEUES__INPUT__URL`, but the agent-runner module injected only `MAX_RECEIVE_COUNT`
and the *output* URL, so an agent calling `create_scheduled_task` would register a schedule that
never delivers. The runner module now injects the input URL whenever its `scheduled_task` gate is on
— the same gate that already grants it `sqs:GetQueueAttributes` on that queue for the soft-delete
TTL derivation — and `AWSScheduler.upsert` refuses a blank URL up front (see *Fails loudly*). The
containerized modules already injected it unconditionally in queue mode. The response handler and
output consumer are still deliberately denied it: they only record outcomes, never register.

The full env contract, beyond the four `AK_SCHEDULER__*` values in the outputs table above:

| Env var | Value | Injected on |
|---|---|---|
| `AK_SCHEDULER__REDIS__PREFIX` | `ak:scheduled_tasks:` | Redis-session deployments only |
| `AK_SCHEDULER__VALKEY__PREFIX` | `ak:scheduled_tasks:` | Valkey-session deployments only |
| `AK_EXECUTION__QUEUES__INPUT__URL` | input queue URL | every component that registers schedules — request handler / REST service always, agent runner when `enable_agent_tools` |

The two dedicated examples (`examples/aws-serverless/scheduled-openai`,
`examples/aws-containerized/openai-scheduled-task`) set `scheduled_task = true` in their
Terraform and declare a **placeholder `scheduler` block** in `config.yaml`, with the
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

**The probe test resolved this: `AK_SCHEDULER__*` env vars alone *do* populate the absent `Optional`
block, nested sub-blocks included** (`test_env_vars_alone_populate_the_absent_scheduler_block`). The
placeholder is therefore no longer load-bearing and could be dropped, but it is kept deliberately:
it is the only place a reader of the example's `config.yaml` learns that the deployment schedules at
all, and which values Terraform supplies. Empty strings do not weaken the enablement check either
way — `validate_config()` treats an empty `group_name` or `target_role_arn` as unset and raises
`AKConfigError`, so enabling scheduling in YAML without the Terraform wiring still fails loudly at
startup.

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
   `message_group_id`** when the failed message is a scheduled fire
   (`serverless/akagentrunner.py:170`). For a fire the group id is the `scheduled_task_id`, so the
   pre-change line would write a wrong `session_id`. Non-scheduled behaviour is unchanged. This
   resolves the divergence with the ECS twin, which never set the field.
7. **`POST /api/v1/chat` accepts a body with no `session_id`** when a `schedule` block is present
   (`rest_handler.py:79` is now reached only on the non-schedule path). *Intentional:* the
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
    thread auto-mount — on REST surfaces only. `RESTAPI._auto_mount_schedule_routes` (`True`) is
    overridden to `False` on `AWSWebsocketAPI`, which inherits `run()` but has no `Authoriser` to
    give the handler and no way for an application to supply one; without the opt-out every
    scheduling-enabled WebSocket deployment would fail to boot.
11. **Sessions whose id starts with `schedule:` skip thread creation and message appending**
    (`integration/thread/recorder.py`). *Intentional:* scheduled activity is kept out of the owner's
    regular conversation history. Fixed behaviour, not configurable.
12. **`ECSWebSocketRequestHandler._handle_chat` no longer enqueues a frame carrying a `schedule`
    block** (`containerized/core/api/websocket_api.py:404-425`); it creates the schedule and
    broadcasts the acknowledgement instead. *Intentional:* containerized supports `async`/`stream`
    under `queue_mode = true`, so without this branch a schedule sent over an ECS WebSocket would be
    executed immediately rather than scheduled. Frames with no `schedule` block are unaffected.
13. **Both output consumers' `on_permanent_failure` now record a scheduled outcome before
    discarding the message.** *Intentional and load-bearing:* the consumers' retry limit sits one
    below the output queue's, so the message is deleted rather than dead-lettered and an unwritten
    outcome is lost for good. Never raises; ordinary messages take the pre-change path untouched.
14. **`ResponseHandler.on_permanent_failure` reads the session id from the record's system
    attributes (`MessageGroupId`) instead of `message_attributes["message_group_id"]`, and omits the
    key when absent.** *Intentional:* the custom-attribute map never carried that key, so the
    pre-change line raised `KeyError` on every permanent failure. A latent bug on a path this
    feature had to touch.
15. **`AgentHandler.bind_request_user` writes the request's `user_id` into the session's volatile
    cache** on every run (`core/chat_service.py`). *Intentional:* it is how tool code reaches a
    trustworthy owner without putting `user_id` into the agent's request context. A no-op when the
    request carries no user.
16. **The `Authoriser` ABC moves from `integration/thread/authoriser.py` to
    `agentkernel/auth/authoriser.py`.** *Intentional:* two route layers now need the same contract.
    Purely a move — the old module re-exports it, so both documented import paths still work.
17. **`RestHandler.enqueue_and_wait` takes a second parameter, `request: Request = None`.**
    *Intentional:* the create branch needs the incoming request to resolve the owner. Defaulted, so
    existing overrides and call sites are unaffected.
18. **Both stream runners route a record carrying `scheduled_run` to the non-stream runner**
    (`containerized/akagentrunner.py`, `serverless/akagentrunner.py`). *Intentional:* a fire has no
    `endpoint_url`, so the pre-change stream path raised on every receive, exhausted its retries, and
    then swallowed the identical error in `on_permanent_failure` — publishing nothing, so the outcome
    was never recorded and `last_run_*` stayed stale while the row still read `ACTIVE`. Interactive
    streaming is unaffected: the branch keys off the block alone, never off a missing `endpoint_url`.

**Non-changes** — fixed by this spec and verified against the base branch:

- `BaseRequest` envelope shape and `BaseRequest.from_payload` semantics (`core/model.py:225-264`).
- `SQSHandler`'s public surface and its default `message_group_id = session_id` for ordinary traffic
  (`sqs_handler.py:346,388`); `QueueHandler.QueueMessageBody` and `SendMessageAttributes`.
- `ResponseStore` ABC and all three backends; the response-store data layout.
- Session store, thread store and attachment store layouts, config and behaviour.
- Agent-runner happy path on both targets. Both stream runners for **interactive** traffic: their
  chunk fan-out, dedup-suffix scheme and `endpoint_url` requirement are unchanged, and only a record
  carrying a `scheduled_run` block takes the new non-stream branch (behavioural change #18).
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
  The deferred TTL derivation is not an exception to that rule: it defers a *call* on an
  already-constructed client, and two threads racing the first read resolve the same value from the
  same queue attributes.
- **Drivers are already thread-safe.** `BaseDriver` serializes connect/reconnect with a per-instance
  `threading.Lock`, which is why the response stores can already be shared under `ECSOutputConsumer`.
- **`mark_run_completed` is a read-modify-write and is not internally locked.** It does not need to
  be: SQS FIFO delivers at most one in-flight message per `MessageGroupId`, and every outcome for a
  scheduled task carries `MessageGroupId = scheduled_task_id`, so two outcomes for the same task can
  never be processed concurrently — across threads *or* across replicas. Guard 4 additionally makes a
  late redelivery harmless, and the `expected_version` condition on the write makes the
  read-modify-write itself atomic with respect to a concurrent incarnation change. This dependency
  on FIFO is explicit, which is why the queue modules create a FIFO input queue rather than leaving
  it to a deployer.
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
| Scheduled outcomes only | One store read + one conditional store write | Proportional; only scheduled traffic pays |
| First `delete` in a process, on components that can delete | One `GetQueueAttributes` call, then cached | Once per process; components that only record outcomes never make it |

No new work lands on the ordinary chat, streaming, or session paths.

---

## Error handling

| Failure | Where | Behaviour |
|---|---|---|
| `scheduler.enabled` with a non-durable `session.type`, or a missing/blank `group_name`/`target_role_arn` | `SchedulerFactory.validate_config()` | `AKConfigError` at component initialization — process startup on ECS, cold start on serverless |
| `scheduled_task = true` with `queue_mode = false` | Terraform `validation` on `scheduled_task` | The deploy fails. Queue mode is **not** re-checked in `validate_config()` — each Lambda holds only the one queue URL it publishes to |
| Backend block missing for the resolved `session.type`, or a non-matching backend block populated | `SchedulerFactory.validate_config()` check #4 | `AKConfigError` at initialization, naming the field. Prevents both a late `table.load()` connection failure and a silently-ignored table name |
| `scheduler.enabled` on the ECS **REST** chat route with no `Authoriser` | `RestHandler.__init__`, `ScheduleRESTRequestHandler.__init__` | `AKConfigError` at initialization. Not applied to `ECSWebSocketRequestHandler`, which authenticates at `$connect` and has no `Authoriser` |
| No user resolvable for an ECS WebSocket connection carrying a `schedule` block | `build_route_context` (`websocket_api.py:320-322`) | `WSRouteError(401)` → error frame, before the schedule branch is reached |
| `GetQueueAttributes` fails | `AWSScheduler.soft_delete_ttl_seconds`, on the first `delete` | `SchedulerError` — no fallback to a guessed TTL. Reached only on the delete path, since the derivation is deferred |
| No input queue URL on a component that registers a schedule | `AWSScheduler.upsert` → `_require_input_queue_url` | `SchedulerError` **before the store write and before any AWS call**. A universal target's `Input` is an opaque string, so EventBridge Scheduler would accept a blank `QueueUrl`, acknowledge the create, and fail only at fire time — a row that exists and never runs. The recording-only components (response handler, output consumer) are unaffected: they call `mark_run_completed`, never `upsert` |
| Registration rejected by EventBridge Scheduler as malformed | `AWSScheduler._register` | A `ValidationException` from the API is re-raised as `ScheduleValidationError` → 400. Local validation cannot cover every provider rule, and a rejected expression is bad input, not a server fault |
| Another writer holds the row's update lock (Redis/Valkey) | `_RowLock` | `SchedulerConflictError` → 409 after 20 attempts over ~1 s; the caller retries |
| Missing optional dependency for the resolved store | `ScheduledTaskStoreBuilder.build()` | `ImportError` naming the pip extra, via `require_extra` (`core/util/factory.py:49-64`). `dynamodb` → `aws`, `redis` → `redis`, `valkey` → `valkey`. No new extra is introduced: EventBridge Scheduler is reached through boto3, already in the `aws` extra |
| `schedule` block present while scheduling is disabled | chat create path, all **four** surfaces (ECS REST, ECS WS, serverless REST, serverless WS) | 400 (REST) / error frame (WS). Never a silent no-op, and never an immediate execution |
| Invalid or too-fine `ScheduleSpec` | `ScheduledTaskService.create` / `update` | `ScheduleValidationError` → 400, before any AWS call. Never silently rounded |
| Caller does not own a live row | service | `SchedulerPermissionError` → 403 |
| Target id is soft-deleted | service | `SchedulerConflictError` → 409 on create/update; 404 on get (soft-deleted rows are not user-visible) |
| No live row on update | service | `SchedulerNotFoundError` → 404. Update never creates |
| Missing/invalid Bearer token on a management or schedule-create route | `_resolve_user` | 401 |
| No authorizer context on a serverless schedule request | `_maybe_schedule` / `ScheduleEndpointsHandler._handle` | `UnauthenticatedScheduleError` → 401 on the chat create path; 401 directly on the management routes |
| Scheduling disabled on a serverless management route | `ScheduleEndpointsHandler._handle` | 404 — the route exists in the gateway but the capability does not |
| Row write succeeds, timer registration fails | `Scheduler.upsert` | The row is restored to its prior state (deleted when it was newly created), then the error propagates. A row without a registration would silently never fire, which is worse than a failed create |
| Timer registration removed, row soft-delete fails | `Scheduler.delete` | Error propagates. The registration is gone, so no further fires; the caller retries the delete. Ordering is deliberate — stopping fires first is the safe half |
| Outcome arrives for an absent, deleted, mismatched or stale row | `Scheduler.mark_run_completed` | Silent no-op: logged at WARNING, returns `False`, message acknowledged. Never retried, never dead-lettered — the run genuinely has nowhere to be recorded |
| Outcome write fails on the output consumer's own permanent-failure path | `ScheduledRunRecorder.record_before_discard` | Swallowed and logged with the full `scheduled_run` identity. There is nothing left to record it on, so the log line is the run's only surviving trace |
| Store or AWS failure inside `mark_run_completed` | `Scheduler.mark_run_completed` | **Raises.** The consumer's normal retry path applies. A guard rejection and an infrastructure failure are deliberately different: only the former is a no-op |
| Timer cannot deliver to the input queue | EventBridge Scheduler | Retried per the schedule's retry policy, then the timer's DLQ. Infrastructure behaviour, outside Agent Kernel |
| Agent run fails or exhausts retries | existing runner paths | Error body echoing `scheduled_run` reaches the output consumer like any other outcome and is recorded as `FAILED` with the retry message as `last_error` |
| A tool call fails for any reason | `scheduler/tools.py` | Caught and returned as `{"error": ...}` JSON — tools never raise into the framework |

Exception scope is explicit throughout: `from_raw_body` catches `(json.JSONDecodeError, TypeError,
ValidationError)` and returns `None`; `from_body` catches nothing beyond the absent-key miss, so a
malformed `scheduled_run` block on the ordinary consumer path surfaces as a `ValidationError` rather
than being silently dropped; the guard checks catch nothing (they are plain comparisons on a
loaded row); store calls are not wrapped, so backend errors propagate to the consumer's retry
machinery. Broad `except Exception` appears in exactly three places, each a give-up path with no
error channel left: the runners' and consumers' `on_permanent_failure` handlers (where the
`QueueConsumer` contract already requires it), `ScheduledRunRecorder.record_before_discard`, and
`AWSScheduler._restore`, where a failed rollback must not mask the registration error that caused
it. Each logs before swallowing.

---

## Testing

Run with `cd ak-py && uv run pytest`.

### New test files

| File | Asserts |
|---|---|
| `tests/test_scheduler_config.py` | `validate_config()` raises `AKConfigError` for: `session.type` in {`in_memory`, `cosmosdb`, `firestore`, a dotted path}, and missing **or empty-string** `group_name`/`target_role_arn`. Returns cleanly for each valid combination, **including with either queue URL unset** — the explicit regression guard that the queue-mode check is not reintroduced. TTL derivation: `visibility_timeout × receives + 300`, floored at 900, where `receives` is the max of the queue's `RedrivePolicy.maxReceiveCount` and the AKConfig value — one case each way (queue value higher, AKConfig value higher), plus **an absent `RedrivePolicy` falling back to the AKConfig value rather than to zero**, which is the default-deployment case since `input_queue_create_dlq` defaults to `false`. `GetQueueAttributes` failure raises instead of defaulting, and **construction makes no such call at all** — the guard that the derivation stays deferred, with a second read served from the cache. `enabled()` is False when the block is absent. Check #4 both ways per backend: the matching block missing or empty raises, and a non-matching block populated raises (e.g. `session.type: redis` with `scheduler.dynamodb.table_name` set) — the second is the regression guard against silently ignoring a configured-but-unused table. Plus the probe test confirming `AK_SCHEDULER__*` env vars alone populate the absent `Optional` block, nested sub-blocks included |
| `tests/test_scheduled_task_store.py` | Per backend, against the store contract: put/get round trip, `list_by_owner` scoping and cursor, `soft_delete` sets `deleted`/`deleted_at` and the expiry, and a soft-deleted row is still `get`-able. **`list_by_owner` excludes soft-deleted rows on every backend, and a page is not short because one was filtered.** **`update_fields` writes only the named attributes and leaves every other one untouched** (write `last_run_*`, assert the definition fields are unchanged, and the reverse), and **returns `False` without writing when `expected_version` mismatches**. DynamoDB against a mocked `DynamoDBDriver` (the `test_sessions_dynamodb.py` pattern) — asserts the driver is built with `ttl=0`, that `expiry_time` is written **only** by `soft_delete`, that `soft_delete` issues a `REMOVE owner_index_key` so the GSI stays sparse while `owner_id` itself survives on the row, and that `list_by_owner` queries the index with **no** `FilterExpression`. Redis against a fake client (the `test_sessions_valkey.py` / `test_multimodal_redis_store.py` pattern) — asserts `set()` writes no `ex`, that `soft_delete` calls `client.expire` with the derived seconds, that `list_by_owner` prunes owner-set members whose row key is gone while *retaining* members whose row is merely soft-deleted, and that `update_fields` takes and releases the `<prefix>lock:<id>` key |
| `tests/test_scheduler_aws.py` | `AWSScheduler` against mocked boto3 `scheduler`/`sqs` clients and the `InMemoryScheduledTaskStore`, plus a subclass of `SchedulerContract` so the provider is held to the shared obligations. `upsert` builds a universal target with `MessageGroupId = scheduled_task_id`, `MessageDeduplicationId = <id>:<scheduled_time>`, both context variables in the payload, `request_id`/`user_id` message attributes present, and `MaximumEventAgeInSeconds = 300`. One-time schedules set `ActionAfterCompletion="DELETE"`. Sub-minute cron/rate raises before any AWS call, and a provider-side `ValidationException` surfaces as `ScheduleValidationError`. `per_run` vs `continuous` session-id shape. Registration failure rolls the row back — restored to its prior state on an update, removed outright on a fresh id. **The four guards**, one test each, asserting a `False` return and no store write; plus a store exception propagating rather than no-op'ing. A one-time task's accepted outcome sets `status = COMPLETED` and `completed_at`. A blank, whitespace-only or `None` `input_queue_url` raises `SchedulerError` from `upsert` with **nothing written to the store and neither `create_schedule` nor `update_schedule` called** — the guard against registering a schedule that could never deliver |
| `tests/test_scheduled_task_service.py` | Id generation (`schedule_<hex>`) vs caller-supplied; fresh version on a new id and **retained** version on a live upsert and on `update`; owner stamped from the parameter and never from the body; `schedule:` prefix on both session-id shapes; 403 on a foreign live row, 409 on a soft-deleted id, 404 on `update` with no live row; a run-out one-time task re-armed by a `PUT` carrying a new future `at` and **rejected** by one that omits it; a replacement schedule omitting `mode` keeping the task's current mode. `CreateAck`: `session_id` present for `continuous` and **absent** for `per_run`; `next_run_at` equals the `at` value for a one-time schedule, equals `updated_at + interval` for a `rate` (re-based when a create replaces a live definition), and is `None` for a `cron`; `request_id` is always present, echoing a caller-supplied id when given and generated otherwise |
| `tests/test_schedule_router.py` | FastAPI `TestClient` over `ScheduleRESTRequestHandler` (the `test_thread_router.py` pattern, with a `StaticAuthoriser`). Construction without an `Authoriser` raises `AKConfigError`. 401 missing/invalid Bearer; list returns only the caller's rows and excludes soft-deleted; 404 on unknown and on soft-deleted `GET`; 403/409/404 on `PUT`; 403 on `DELETE`; routes absent from the app when `scheduler.enabled` is false |
| `tests/test_scheduler_tools.py` | All four tools route through `ScheduledTaskService` (asserted with a mock service); the owner is bound from the invoking session and cannot be supplied as an argument; a service error is returned as `{"error": ...}` and never raised; `SystemToolFactory.get_all()` includes the tools only when enabled and honours `scheduler.agents` scoping |
| `tests/test_agent_runner_permanent_failure.py` | **New file — neither non-stream runner has an existing test** (`test_akagentrunner_stream.py` and `test_ecs_akagentrunner_stream.py` cover the stream runners only), so behavioural changes #5 and #6 are otherwise untested. Both runners: a record whose body carries `scheduled_run` produces an error body echoing it verbatim; an unparseable body produces the pre-change error body and **does not raise**; the ECS runner's inline error dict and the serverless runner's `_construct_error_message_body` result (`serverless/akagentrunner.py:147-149`) are each asserted in place. Serverless only: `session_id` comes from the parsed body when `scheduled_run` is present, is **omitted** when the body cannot be parsed, and still equals `record_attributes["message_group_id"]` for a non-scheduled record (`:150`) — the regression guard for #6 |
| `tests/test_ecs_akagentrunner_stream.py`, `tests/test_akagentrunner_stream.py` | **These files already exist** and cover the stream runners, so the scheduled-fire routing extends them rather than adding a file. Each gains a `_make_fire_record()` builder carrying a `scheduled_run` block and, deliberately, **no `endpoint_url`** — the shape the timer actually delivers. New cases, identical on both platforms: a fire calls `process_chat_request` and **not** `process_stream_chat_sync`, sending exactly one whole-response message whose body echoes `scheduled_run`; a fire's `on_permanent_failure` publishes an error body carrying `scheduled_run` and **no `done` key**, asserting it is the non-stream error body and not a `StreamChunk`; and an ordinary stream request still fans out chunks and never reaches `process_chat_request` — the regression guard that the branch keys off the block alone, not off a missing `endpoint_url`. All existing assertions unchanged, including the two that pin the `endpoint_url` requirement for interactive streaming |
| `tests/test_rest_handler_schedule.py` | **New file** — covers behavioural changes #7 and #8, which `test_schedule_router.py` does not reach (that file covers the management routes only). FastAPI `TestClient` over `ECSQueueRequestHandler`: a body with a `schedule` block returns 201 with the ack and **does not** call `send_message_to_input_queue`; the same body with scheduling disabled returns 400; a body with neither `session_id` nor `schedule` still returns 400 (the pre-change behaviour at `rest_handler.py:45-46`); `RestHandler.__init__` raises `AKConfigError` when `scheduler.enabled` and no `Authoriser` is supplied, and does **not** raise when scheduling is disabled |
| `tests/conftest_scheduler.py` | **Shared fixtures, not a test file.** `enable_scheduler_config()` / `reset_scheduler_config()` build and tear down a valid `scheduler` block, `make_scheduler()` assembles an `AWSScheduler` over mocked clients and the in-memory store, and `install_scheduler()` seeds the `SchedulerFactory` singleton. It exists because eight test modules need the same enabled-and-wired config, and duplicating that setup is how the suite drifts from `validate_config()` |
| `tests/test_scheduled_run_recorder.py` | `ScheduledRunRecorder.record_before_discard`, the path with no error channel left: the status comes from the body and not from the fact that this is the failure path, an `error` key still maps to `FAILED`, an ordinary/non-dict/unparseable body is left to the caller, a malformed block never raises here, and a failed write is swallowed **and logged with the full identity** |
| `tests/test_scheduler_import_isolation.py` | The enablement check fires on start, not on import: importing either output-consumer module with the wiring absent must **not** raise, while `ECSOutputConsumer.run()` and `ResponseHandler.handle()` raise `AKConfigError` before polling or processing, and proceed normally when the wiring is present. The regression guard for the `agentkernel.aws` re-export problem — the authorizer Lambda imports those classes and is deliberately given no `AK_SCHEDULER__*` environment |
| `tests/test_ws_lambda_schedule.py` | The serverless WebSocket create path: a frame carrying a `schedule` block is **not** enqueued, the ack is broadcast as `CHAT_RESPONSE` in `async` mode and as a single `STREAM_CHUNK` with `done: True` in `stream` mode, the owner is the `$connect`-resolved `user_id`, and a disabled deployment returns 400 |
| `tests/test_chat_service_scheduled.py` | The `known_fields` regression guard — neither `scheduled_run` nor `schedule` reaches the agent as context, while a genuinely unknown field still does; the `build_response` echo on success and on the error path, and omission for ordinary traffic; and, against `ThreadRecorder`, that a `schedule:`-prefixed session creates no thread and appends no assistant message while an ordinary session still does |
| `tests/test_ecs_websocket_schedule.py` | **New file** — covers behavioural change #12. A chat frame carrying a `schedule` block creates the task and **`SQSHandler.send_message_to_input_queue` is never called**; the ack is broadcast as `CHAT_RESPONSE` in `async` mode and as a single `STREAM_CHUNK` with `done: True` in `stream` mode; a frame with a `schedule` block and scheduling disabled returns 400; a connection with no resolvable user still yields 401 from `build_route_context` (`websocket_api.py:320-322`); an ordinary frame with no `schedule` block is enqueued exactly as before; and constructing `ECSWebSocketRequestHandler` with `scheduler.enabled` and no `Authoriser` **does not raise** — the guard against wrongly inheriting the #8 check. Plus the boot test one layer up: with scheduling enabled, `AWSWebsocketAPI.run()` reaches `uvicorn.run` without raising and the assembled app answers 404 on `/api/v1/schedule` — the guard that the REST auto-mount is not reintroduced here (behavioural change #10) |

### Changed existing tests

| File | Change |
|---|---|
| `tests/test_akresponsehandler.py` | New cases: a `scheduled_run` response calls `mark_run_completed` and is **neither broadcast nor stored**, in each of `rest_sync`, `async` and `stream` (the `async`/`stream` cases are the regression guard — the pre-change code raises on the missing `endpoint_url`). Existing patch target `agentkernel.deployment.aws.serverless.akresponsehandler.AKConfig` (line 84) is retained; the new cases additionally patch the module's scheduler accessor. All existing assertions unchanged |
| `tests/test_model.py` | `BaseRunRequest` defaults `schedule` and `scheduled_run` to `None` and round-trips both; `ScheduleSpec` rejects zero and multiple timing expressions; `from_body` returns `None` for a dict lacking the block and parses a valid one; `from_raw_body` returns `None` for malformed JSON, a non-dict body, `None`, and a dict lacking the block, and parses both a JSON string and a dict. Plus the **import-order guard**: importing `agentkernel.core.model` loads no module under `agentkernel.scheduler` (assert against `sys.modules` in a subprocess), and `agentkernel.scheduler.model.ScheduleSpec is agentkernel.core.model.ScheduleSpec` |
| `tests/test_chat_service_streaming.py` | Extended with the `bind_request_user` assertion — the request's `user_id` lands in the session's volatile cache under `REQUEST_USER_ID_KEY` on the streaming paths too. The rest of the scheduling assertions live in the sibling `test_chat_service_scheduled.py` above |
| `tests/test_ecs_akoutputconsumer.py` | **This file already exists** (8 tests over `process_message` in `stream`/`async` WebSocket modes plus three `on_permanent_failure` cases), so the `ECSOutputConsumer` work extends it rather than creating a new file. New cases: a response carrying `scheduled_run` calls `mark_run_completed` with the derived status and is **neither broadcast nor written to the response store**; a body with an `error` key maps to `FAILED` with `last_error`; an ordinary response is stored/broadcast exactly as before; `on_permanent_failure` is unchanged. The `async`/`stream` cases are the regression guard — `test_broadcast_via_websocket_raises_when_endpoint_url_missing` (`:51`) is the pre-change failure a timer-originated message would hit. `on_permanent_failure` gains cases for behavioural change #13: a scheduled body is recorded and returns early, an ordinary one takes the pre-change path. All existing assertions unchanged |
| `tests/test_lambda_router.py` | The resource-template fallback resolves `/schedule/{scheduled_task_id}` from `event["resource"]` and passes `pathParameters`; an unmatched path still raises `ValueError` (unchanged) |
| `tests/test_api_http.py` | `RESTAPI.run()` mounts the schedule router when `scheduler.enabled`, skips it when a `ScheduleRESTRequestHandler` was supplied, and does not mount it when disabled. Plus the failure case: **auto-mounting with no supplied handler raises `AKConfigError` before uvicorn binds**, since the auto-mounted instance has no `Authoriser`. The subclass opt-out (`_auto_mount_schedule_routes = False`) is covered end to end in `test_ecs_websocket_schedule.py`, where the WebSocket API actually inherits `run()` |
| `tests/test_serverless_request_handle.py` | A payload carrying a `schedule` block is not enqueued and returns the 201 ack; identity is taken from `requestContext.authorizer.principalId`; a missing authorizer context yields 401; `_handle_request` still answers 200 for an operation returning a bare body |
| `tests/test_rest_handler_poll.py` | Updated for `enqueue_and_wait`'s added `request` parameter (behavioural change #17); poll behaviour is otherwise unchanged |

Not changed, and verified so: `test_sqs_handler.py` (the `SQSHandler` surface is untouched),
`test_ecs_sqs_consumer_parallel.py` (`process_message`/`delete` semantics are untouched),
`test_store_builders.py`
and `test_factory.py` (existing builders are untouched; the new builder is covered in
`test_scheduled_task_store.py`).

---

## Deviations from design.md

Four points where detailing or implementation revealed the design could not be built exactly as
written. All were raised rather than silently absorbed, and **all four are now reflected in
design.md** — this section is kept as the record of what changed and why, not as an open list.

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

2. **The `scheduler` config block carries deployment wiring, not just `enabled`.** design.md
   originally stated "The `scheduler` config block carries only `enabled`." This spec reads that as
   "carries no scheduled-task definitions" and adds `agents`, `group_name`, `target_role_arn`,
   `region` and the three per-backend location blocks. `group_name` and `target_role_arn` are
   unavoidable — EventBridge Scheduler requires a schedule group and a role ARN to write to SQS, and
   neither is derivable from existing config. The table name / key prefix are needed because the
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

4. **Queue mode and the FIFO input queue are enforced in Terraform, not in `validate_config()`.**
   design.md originally required both as initialization checks in the application:
   "`scheduler.enabled` with either queue URL unset raises `AKConfigError`", extended with a
   `.fifo` suffix check. Both were implemented that way and then removed, because they reject a
   correctly-wired serverless
   deployment: each Lambda receives only the queue URL it publishes to, so the response handler has
   no input URL and the request handler no output URL, and an absent URL is not evidence of a
   misconfiguration. The preconditions are unchanged; only the place they are checked moved, to a
   `validation` block on `scheduled_task` and to queue modules that create a FIFO input queue. The
   guarantee is arguably stronger — the deploy fails before the application is ever built — but the
   failure is no longer visible from the app's own config validation, so it is recorded here.
