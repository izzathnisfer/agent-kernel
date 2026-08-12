# Scheduled Tasks — OpenAI Agents on AWS ECS (Queue Mode)

Agents that run on a timer. A **scheduled task** is a stored agent message plus the schedule that
fires it: when it comes due, EventBridge Scheduler puts an ordinary agent message on the same SQS
input queue the REST service uses, and the agent runner executes it with no scheduling-specific code
path at all.

This example shows the whole loop on ECS — creating a task, managing it, and proving it ran — plus
the piece that makes it possible: an **authorizer that resolves a bearer token to a user id**.

## Why the authorizer is mandatory

Every scheduled task is owned by an authenticated identity. The owner is stamped server-side and is
never read from the request body, so it cannot be forged or overridden. That means scheduling cannot
be switched on without an identity resolver — and Agent Kernel refuses to start rather than let an
ownerless task exist:

```
AKConfigError: scheduler.enabled requires an Authoriser on the chat route —
every scheduled task must have an authenticated owner
```

On ECS the resolver is an `Authoriser` — the same abstraction the conversation-thread routes use
(see `examples/api/thread-openai`). Implement one method:

```python
class DemoAuthoriser(Authoriser):
    _TOKENS = {"alice-token": "alice", "bob-token": "bob"}

    def authorise(self, token: str) -> Optional[str]:
        return self._TOKENS.get(token)
```

A real subclass would verify a JWT signature or call your auth provider and return the subject's
user id, or `None` to reject. A static token map stands in here so the example runs unmodified.

The same instance has to reach **two** handlers, which is why `app_rest_service.py` overrides
`AWSRestAPI.get_default_handlers`:

| Handler | Serves | Without an Authoriser |
|---|---|---|
| `ECSQueueRequestHandler` | `POST /api/v1/chat` — the create path | `AKConfigError` at startup |
| `ScheduleRESTRequestHandler` | `/api/v1/schedule` management routes | `AKConfigError` at startup |

Supplying your own `ScheduleRESTRequestHandler` also stops Agent Kernel from auto-mounting an
unauthorised one.

## Architecture Overview

```
POST /api/v1/chat  { prompt, agent, schedule: {...} }
  → REST service registers the task and returns 201 SCHEDULED
    (nothing is enqueued — there is no run yet)

timer fires
  → EventBridge Scheduler → SQS input queue (an ordinary agent message)
  → Agent Runner consumes → runs the agent on the normal path
  → output queue → output consumer records last_run_at / last_run_status on the row
```

Two ECS services, as in any queue-mode deployment. The scheduler adds a DynamoDB table for task
definitions, an EventBridge Scheduler schedule group, and an IAM role the timer assumes to write to
the input queue.

### Deployed Resources

| Resource | Purpose |
|---|---|
| ECS REST service | Chat + `/api/v1/schedule` routes; creates and manages tasks |
| ECS agent runner | Consumes the input queue — ordinary requests and fires alike |
| SQS input / output queues (FIFO) | Request delivery; the timer's target |
| DynamoDB session table | Conversation state |
| DynamoDB response store | REST_SYNC response handoff |
| **DynamoDB scheduled-task table** | Task definitions, last-run status, soft-delete TTL |
| **EventBridge Scheduler group** | This deployment's timers, namespaced for clean destroy |
| **Timer execution role** | Assumed by the scheduler; `sqs:SendMessage` on the input queue only |

## Requirements

Scheduling is deliberately narrow in this version. All of these are enforced, most at startup:

- **AWS only** — Azure and GCP are not supported.
- **Queue mode only** (`queue_mode = true`). Terraform rejects `scheduled_task = true` without it.
- **FIFO input queue** — the containerized module hardcodes this. Fires are grouped by
  `scheduled_task_id` so a task's runs are serialized, and deduplicated by
  `scheduled_task_id` + `scheduled_time` so a duplicate timer delivery cannot run twice.
- **A durable session store** — `dynamodb`, `redis` or `valkey`. `in_memory` fails the check.
- **An authenticated owner** — see above.

## Prerequisites

- An AWS account with credentials configured
- Terraform >= 1.9.5, Docker, `uv`
- An OpenAI API key
- An existing VPC with private subnets

## Deployment Steps

```bash
export TF_VAR_openai_api_key="sk-..."
export TF_VAR_vpc_id="vpc-..."
export TF_VAR_private_subnet_ids='["subnet-...","subnet-..."]'

./build.sh
cd deploy && ./deploy.sh
```

`deploy.sh` builds two images (REST service and agent runner), pushes them, applies the Terraform,
and waits for both ECS services to stabilise. Use `./build.sh local` / `./deploy.sh local` to build
against a local `ak-py` wheel.

The invoke URL comes from the `agent_invoke_url` output:

```bash
URL=$(terraform output -raw agent_invoke_url)
```

## Creating a scheduled task

There is **no creation endpoint**. `POST /api/v1/chat` accepts an optional `schedule` block; when
it's present the message is registered to run later instead of being run now.

```bash
curl -X POST "$URL" \
  -H "Authorization: Bearer alice-token" \
  -H "Content-Type: application/json" \
  -d '{
        "prompt": "Summarise the overnight error log",
        "agent": "report",
        "schedule": { "cron": "0 9 * * ? *", "mode": "per_run" }
      }'
```

```json
{
  "status": "SCHEDULED",
  "scheduled_task_id": "schedule_5f1c...",
  "scheduled_task_version": "...",
  "next_run_at": null,
  "request_id": "..."
}
```

The acknowledgement confirms **registration, not execution**. Note `next_run_at` is `null` for cron
expressions — that means "not derivable without evaluating the expression", never "not scheduled".
`session_id` is returned for `continuous` mode only.

### The schedule block

| Field | Meaning |
|---|---|
| `cron` / `rate` / `at` | The timing. **Exactly one** is required. |
| `mode` | `per_run` (default) — every run starts a fresh conversation. `continuous` — all runs share one. |
| `id` | Optional caller-chosen `scheduled_task_id`. Reusing one replaces the definition instead of duplicating it. |
| `timezone` | Defaults to `UTC`. Applies to the wall-clock expressions, `cron` and `rate`; an `at` is an absolute instant and is registered in UTC either way. |

Minimum granularity is **one minute**, EventBridge Scheduler's floor. Anything finer is rejected at
creation, not silently rounded.

Give `cron` and `rate` the **bare** expression — `"0 9 * * ? *"`, not `"cron(0 9 * * ? *)"`. The
provider adds its own wrapper, so a pre-wrapped value is rejected with a 400. A `rate` unit has to
agree in number with its amount — `"1 minute"` and `"5 minutes"`, never `"1 minutes"` — which is
also rejected with a 400.

```json
{ "cron": "0 9 * * ? *" }               // 09:00 daily
{ "rate": "15 minutes" }                // every 15 minutes
{ "at": "2026-09-01T09:00:00Z" }        // once, then done
```

A one-time task keeps its row after firing with `status: COMPLETED`, so you can still ask whether it
ran and whether it succeeded.

## Managing scheduled tasks

All four routes require the bearer token and are scoped to its owner.

```bash
# List your tasks (soft-deleted ones are never returned)
curl "$BASE/api/v1/schedule" -H "Authorization: Bearer alice-token"

# Read one, including last-run state
curl "$BASE/api/v1/schedule/$ID" -H "Authorization: Bearer alice-token"

# Change the schedule and/or the message — omitted fields keep their value
curl -X PUT "$BASE/api/v1/schedule/$ID" \
  -H "Authorization: Bearer alice-token" \
  -d '{"prompt": "Summarise the overnight warnings instead"}'

# A one-time task that has already run needs a new future `at` before it can be changed
curl -X PUT "$BASE/api/v1/schedule/$ONCE_ID" \
  -H "Authorization: Bearer alice-token" \
  -d '{"schedule": {"at": "2026-08-10T09:00:00Z"}, "prompt": "Run it again tomorrow"}'

# Stop it
curl -X DELETE "$BASE/api/v1/schedule/$ID" -H "Authorization: Bearer alice-token"
```

| Status | When |
|---|---|
| `401` | Missing, malformed, or rejected bearer token |
| `403` | The task belongs to somebody else |
| `404` | Unknown id, or a `PUT` on a task that doesn't exist (update never creates) |
| `409` | The task was deleted and its id is still in its grace period |
| `400` | The schedule is invalid or finer than a minute, or a one-time task that has already run was updated without a new future `at` |

A replacement `schedule` block that omits `mode` keeps the task's current mode, so retiming a
`continuous` task never moves it to a fresh per-run conversation.

Ownership is real, not cosmetic — `bob-token` cannot list, read, update or delete alice's tasks.

### Did it run?

Run outcomes live on the row, not in the response store:

```bash
curl "$BASE/api/v1/schedule/$ID" -H "Authorization: Bearer alice-token"
```

`last_run_at`, `last_run_status` (`COMPLETED` / `FAILED`) and `last_error` answer "did this run,
when, and did it succeed". A run that exhausts its SQS retries is recorded as `FAILED` with the
retry message in `last_error` — no dead-letter queue archaeology needed.

Output itself is not stored: a scheduled run has no live client, so whatever the agent does is the
result. Have the agent write somewhere durable if you need to keep it.

### Deletion is terminal

Deleting removes the timer registration and soft-deletes the row with a TTL. During that window the
id is reserved and cannot be recreated (`409`); after it expires the id is reusable and gets a fresh
incarnation. A fire already on the queue when a delete lands still executes, but its outcome is
discarded — a queued run can never resurrect a deleted task.

## Letting the agent schedule its own work

`scheduled_task_config.enable_agent_tools = true` in `deploy/main.tf` grants the agent runner the
table and scheduler permissions, which attaches four tools to the agents:
`create_scheduled_task`, `update_scheduled_task`, `delete_scheduled_task`, `list_scheduled_tasks`.

```bash
curl -X POST "$URL" \
  -H "Authorization: Bearer alice-token" \
  -d '{"prompt": "Every weekday at 8am, summarise my overnight errors",
       "agent": "assistant", "session_id": "abc", "user_id": "alice"}'
```

The agent creates the task itself. **An agent is never an owner** — the task binds to the human
identity that owns the invoking session, exactly as if that person had called the endpoint. The
agent is the mechanism; the human stays the principal.

Two independent gates control this. Terraform's `enable_agent_tools` decides whether the runner has
any scheduler access at all; `scheduler.agents` in `config.yaml` scopes which agents get the tools
(omit for all, `[]` for none). Leave `enable_agent_tools` at its default `false` and the runner gets
no permissions while the REST routes keep working.

The agent's instructions say nothing about these tools — they carry their own guidance, so
describing them again in a prompt only competes with it.

## Scheduled runs and conversation history

Sessions created for scheduled runs use a reserved `schedule:` prefix and are deliberately excluded
from conversation thread listings, so a nightly job never clutters the owner's chat history. This is
fixed behaviour, not configurable.

## Using Redis or Valkey instead of DynamoDB

The scheduled-task store follows the session store — you don't configure it separately. Swap
`create_dynamodb_memory_table` for a Redis/Valkey cluster and **no new table is created**; scheduled
tasks share that cluster under their own keyspace:

```yaml
scheduler:
  enabled: true
  group_name: ""
  target_role_arn: ""
  redis:
    prefix: "ak:scheduled_tasks:"
```

Only the block matching `session.type` may be populated — setting `scheduler.dynamodb` on a Redis
deployment fails at startup rather than being silently ignored.

## Testing

```bash
export AK_TEST_ENDPOINT=$(cd deploy && terraform output -raw agent_invoke_url)
uv run pytest -s
```

`app_test.py` covers the ordinary chat path (unchanged by scheduling), 401 without a token, the
201 acknowledgement, owner-scoped listing, 403 for a non-owner on every route, update/delete, and
finally a `1 minute` rate task polled until `last_run_status` turns `COMPLETED` — the one test that
proves a fire actually reaches the agent runner. That last test takes a few minutes by construction.

## Troubleshooting

| Symptom | Cause |
|---|---|
| REST service crash-loops with `AKConfigError` about an `Authoriser` | Scheduling is enabled but a handler was built without one — check `get_default_handlers` |
| `400 Scheduling is not enabled for this deployment` | `scheduler.enabled` is false, or `AK_SCHEDULER__*` never reached the container |
| `AKConfigError` about the input queue at startup | Not queue mode, or the input queue is not FIFO |
| `terraform apply` rejects `scheduled_task` | `queue_mode = false` — scheduling requires the queue |
| Task created but never runs | Schedule finer than a minute, or check the EventBridge Scheduler group and the timer role's `sqs:SendMessage` grant |
| Every task owned by the same user | The authorizer isn't returning a per-user id |

## Related

- `examples/api/thread-openai` — the `Authoriser` pattern this example builds on
- `examples/aws-serverless/scheduled-openai` — the same feature on Lambda, where identity comes from
  an API Gateway authorizer instead
- `examples/aws-containerized/openai-dynamodb-scalable` — the same queue-mode deployment without
  scheduling
