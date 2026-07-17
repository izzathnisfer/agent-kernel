# Agent-Initiated Slack Conversations

Demonstrates AK-134 agent-initiated conversations in a single-process REST
deployment: an agent proactively messages another Slack user, and that user's
reply continues the same conversation instead of starting a context-less one.

## How it works

- The `mapping_table:` block in `config.yaml` enables the feature: the
  `initiate_conversation` system tool is registered on all agents, and inbound
  Slack messages resolve their `thread_ts` through the Session ID Mapping.
- `SlackInitiationHandler` (in `server.py`) plays both handler roles:
  - as an `AgentSlackRequestHandler`, it resolves replies to initiated sessions;
  - as an `InitiationSender`, its `send_initiation_message()` is the send point —
    `RESTAPI.run()` detects it and registers the in-process dispatcher, which
    sends and then binds the `session_id <-> thread_ts` mapping automatically.
- When an agent is asked to contact someone, the tool creates a fresh session,
  composes the outbound message by running an agent with your prompt (so the
  new session's history already contains the exchange), and dispatches it.
- Two agents split the roles: `general` faces the requester — it extracts the
  recipient's member id from the Slack mention (`@name` arrives in message text
  as `<@U...>`) and calls `initiate_conversation` with `agent="notifier"`;
  `notifier` faces the recipient — its reply is delivered verbatim, and its
  instructions make it write the notification itself, so the recipient gets a
  clear message ("Hi! The deployment finished successfully.") instead of
  assistant meta-chatter.

## Setup

1. Create a Slack app with a bot token (`chat:write`, `im:write`, event
   subscriptions for messages), and export the usual Slack env vars:

   ```bash
   export SLACK_BOT_TOKEN=xoxb-...
   export SLACK_SIGNING_SECRET=...
   export OPENAI_API_KEY=sk-...
   ```

2. Build and run:

   ```bash
   ./build.sh
   uv run server.py
   ```

3. Point your Slack app's event subscription at `http://<host>:8000/slack/events`.

## Try it

In any channel the bot is in (as user James), @-mention the recipient:

> @bot inform @monroe that the deployment finished successfully

Slack delivers that text as `inform <@U0123456789> that ...`; the `general`
agent extracts the member id and calls `initiate_conversation`, the `notifier`
agent composes the recipient-facing text, and a DM lands in Monroe's inbox:

> Hi! The deployment finished successfully.

When Monroe replies to the DM, the reply resolves through the Session ID
Mapping to the initiated session — the agent knows exactly what it told her.
(A raw member id in place of the mention works too; asking for someone by bare
name without a mention gets a request to @-mention them, since the agent has
no way to resolve names to member ids.)

Uncomment the `thread:` block in `config.yaml` to also record initiated
conversations as AK conversation threads owned by the recipient (readable via
`GET /api/v1/threads?user_id=...`).
