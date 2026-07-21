# Agent-Initiated Slack Conversations (AWS Containerized / ECS)

Demonstrates agent-initiated Slack conversations on the scalable ECS
architecture (see [`../openai-dynamodb-scalable/`](../openai-dynamodb-scalable/)
for the plain version of this same 2-image shape): an agent proactively
messages another Slack user, and that user's reply continues the same
conversation instead of starting a context-less one.

For the single-process REST version of this same scenario, see
[`examples/api/slack-initiation/`](../../api/slack-initiation/) — start there
if you want the simpler synchronous version before tackling the queue
architecture here. For the AWS Lambda serverless version of *this* example,
see [`../../aws-serverless/slack-initiation/`](../../aws-serverless/slack-initiation/).

## Architecture

Two ECS services connected by SQS:

- **REST Service** (`app_rest_service.py`) — two threads:
  - Thread 1: FastAPI serving `/health` and `/slack/events`
    (`slack_request_handler.py`'s `SlackECSRequestHandler`), which resolves
    the inbound `thread_ts` through the Session ID Mapping
    (`InitiationManager.resolve_session_id`, via the `resolve_session_id()`
    method it inherits from `RESTRequestHandler`) and enqueues to the Input
    Queue. It acks Slack and returns immediately — it never blocks on the
    agent round trip, unlike the REST example.
  - Thread 2: `slack_output_consumer.py`'s `SlackECSOutputConsumer` polling
    the Output Queue and delivering replies to Slack.
- **Agent Runner** (`app_agent_runner.py`) — the same two-agent
  identity-grounding design as `examples/api/slack-initiation/server.py`
  (`general` + `notifier` + `get_requester_id`). Additionally carries the
  Slack `channel`/`thread_ts` from the input message's custom SQS attributes
  to the output message's, since the chat response body has no room for
  platform routing data.

`INITIATION`-typed messages (the agent proactively contacting someone via the
`initiate_conversation` tool) are delivered by the same output consumer — see
[`docs/docs/advanced/conversation-initiation.md`](../../../docs/docs/advanced/conversation-initiation.md).
It opens a DM or posts to a channel, then calls
`InitiationManager.get().complete(initiation, thread_ts)` to bind the mapping
so the recipient's threaded reply resolves back to this session.

Threading is required to continue an initiated conversation, exactly as in
the REST example: an un-threaded reply's own `ts` never matches a bound
mapping, so it starts a brand-new, context-less session rather than guessing
which prior conversation it's answering.

None of this required any changes to the `agentkernel` library — every piece
above is a plain subclass or hand-rolled wiring using an extension point that
already exists (`RESTRequestHandler`, `SQSHandler`, `ECSAgentRunner`,
`ECSOutputConsumer`, `ThreadRunner`), the same recipe
`examples/aws-containerized/openai-dynamodb-scalable/` already uses for a
non-Slack agent. The recipe applies identically to any other messaging
platform — only the SDK used for parsing/signing/sending changes.

## Prerequisites

- AWS CLI configured with appropriate credentials
- Terraform (`1.9.5` or higher) installed
- Docker installed (for building container images)
- UV package manager installed
- A Slack app with a bot token (`chat:write`, `im:write`, event subscriptions
  for `message.channels`/`message.im`)

## Deployment Steps

1. Configure environment variables:
    ```bash
    export TF_VAR_openai_api_key=<OPENAI_API_KEY>
    export TF_VAR_slack_bot_token=<SLACK_BOT_TOKEN>       # xoxb-...
    export TF_VAR_slack_signing_secret=<SLACK_SIGNING_SECRET>
    export TF_VAR_vpc_id=<VPC_ID>
    export TF_VAR_private_subnet_ids='["subnet-xxx","subnet-yyy"]'
    export TF_VAR_product_alias="ak-slack-init-ecs"
    export TF_VAR_env_alias="dev"
    export TF_VAR_module_name="examples"
    export TF_VAR_region="us-east-1"
    ```

2. Deploy:
    ```bash
    cd deploy && ./deploy.sh          # ./deploy.sh local for a local agentkernel build
    ```

3. Point your Slack app's Request URL at the `agent_invoke_url` Terraform
   output with the trailing `/chat` replaced by `/slack/events`
   (e.g. `https://<api-id>.execute-api.<region>.amazonaws.com/agents/api/v1/slack/events`).

## Try it

In any channel the bot is in (as user James), @-mention the recipient and
give a reason:

> @bot tell @monroe I'll be late because of traffic

The `general` agent extracts Monroe's member id from the mention, calls
`get_requester_id` to learn the message came from James, and composes a
third-person prompt for `notifier` naming both explicitly. A DM lands in
Monroe's inbox, correctly attributed instead of first person:

> Hi! Just a heads up — <@U0JAMES> will be late, held up in traffic.

When Monroe replies **in a thread** under that DM, the reply resolves through
the Session ID Mapping to the initiated session, and the context is retained
correctly:

> Monroe: who said that?
> Bot: <@U0JAMES> did.
> Monroe: why?
> Bot: Traffic.

Uncomment the `thread:` block in `config.yaml` to also record initiated
conversations as AK conversation threads owned by the recipient (readable via
`GET /api/v1/threads?user_id=...`).

## Continuous integration

This example is part of the **weekly integration test** suite
(`.github/integration-test-config.yaml`, `aws-containerized` entry pointing at
this directory), run by `.github/workflows/integration-test-weekly.yaml` —
Sundays on schedule, or on demand via the Actions tab
("Weekly Integration Tests" → "Run workflow"). Each run deploys this example
fresh (against the shared VPC from the `examples/aws-serverless/openai` base
deployment), runs its tests, then destroys the stack.

Two test files run, for different things:

- **`app_test.py`** — local unit tests (fakes for SQS/Slack), always run,
  no deployed infrastructure needed.
- **`app_deployment_test.py`** — a live smoke test against the actually
  *deployed* ECS REST service: signs a real Slack `url_verification` request
  and posts it to the `/slack/events` gateway route (replace trailing `/chat`
  on `agent_invoke_url`), asserting the challenge is echoed back. This is the
  check that would have caught a broken deployment (e.g. a missing
  `gateway_endpoints` route, or a packaging failure in the container image) —
  a local unit test can't see that, since it imports the same working source
  tree the test runner already has. Skipped automatically unless
  `AK_TEST_ENDPOINT` and `SLACK_SIGNING_SECRET` are set (which only the CI
  workflow does), so a plain local `uv run pytest` is unaffected.

### Running it yourself

The CI workflow is just `.github/scripts/run_single_test.py` driven by
`.github/integration-test-config.yaml` — you can run the exact same
deploy → test → destroy cycle locally (with your own AWS credentials and a
`TF_VAR_vpc_id`/`TF_VAR_private_subnet_ids` from an already-deployed
`examples/aws-serverless/openai`):

```bash
export OPENAI_API_KEY=<...> TF_VAR_openai_api_key=<...>
export TF_VAR_slack_bot_token=<...> TF_VAR_slack_signing_secret=<...>
export TF_VAR_vpc_id=<...> TF_VAR_private_subnet_ids='["subnet-xxx","subnet-yyy"]'

# from the repo root
python .github/scripts/run_single_test.py \
  --type aws-containerized --path examples/aws-containerized/slack-initiation --action deploy

python .github/scripts/run_single_test.py \
  --type aws-containerized --path examples/aws-containerized/slack-initiation --action test

python .github/scripts/run_single_test.py \
  --type aws-containerized --path examples/aws-containerized/slack-initiation --action destroy
```

`--action deploy` runs `terraform init` + `./deploy.sh local`; `--action test`
reads the `agent_invoke_url` Terraform output, waits for the endpoint to come
up, sets `AK_TEST_ENDPOINT`, and runs `uv run pytest` in this directory (both
test files above); `--action destroy` tears everything down. To trigger the
full CI run instead of running locally: GitHub → Actions →
**Weekly Integration Tests** → **Run workflow**.

## Troubleshooting

See the [containerized deployment README](../../../ak-deployment/ak-aws/containerized/README.md)
and [`../openai-dynamodb-scalable/README.md`](../openai-dynamodb-scalable/README.md)
for general ECS/queue-mode troubleshooting (stale Docker images, autoscaling,
execution modes) — this example reuses the identical infrastructure shape.
