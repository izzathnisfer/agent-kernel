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

## Troubleshooting

See the [containerized deployment README](../../../ak-deployment/ak-aws/containerized/README.md)
and [`../openai-dynamodb-scalable/README.md`](../openai-dynamodb-scalable/README.md)
for general ECS/queue-mode troubleshooting (stale Docker images, autoscaling,
execution modes) — this example reuses the identical infrastructure shape.
