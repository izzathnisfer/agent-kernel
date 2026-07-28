# Agent-Initiated Slack Conversations

Demonstrates agent-initiated conversations in a single-process REST
deployment: an agent proactively messages another Slack user, and that user's
reply continues the same conversation instead of starting a context-less one.

## How it works

- `session.initiation.enabled: true` in `config.yaml` enables the feature for this
  single-process REST deployment (queue-mode deployments auto-enable): the
  `initiate_conversation` system tool is registered on all agents, and inbound
  Slack messages resolve their `thread_ts` through the Session ID Mapping.
- `SlackInitiationHandler` (in `server.py`) plays both handler roles:
  - as an `AgentSlackRequestHandler`, it resolves threaded replies to initiated
    sessions — DMs and channels behave identically, and an un-threaded reply
    starts a new session rather than guessing which conversation it continues;
  - as an `InitiationSender`, its `send_initiation_message()` is the send point —
    `RESTAPI.run()` detects it and registers the in-process dispatcher, which
    sends and then binds the `session_id <-> thread_ts` mapping automatically.
- When an agent is asked to contact someone, the tool creates a fresh session,
  composes the outbound message by running an agent with your prompt (so the
  new session's history already contains the exchange), and dispatches it.
  There's no separate context channel — the `prompt` argument both seeds the
  new session and produces the outbound message — so it has to be
  self-disambiguating: a raw first-person forward of the requester's words
  ("Inform them that I'll be late") leaves nothing in the session saying who
  "I" is, and once the recipient starts replying (also a "user" turn, same
  session), the model can't tell the requester and the recipient apart.
- Two agents split the roles, grounding identity explicitly instead of
  forwarding raw phrasing: `general` faces the requester — it extracts the
  recipient's member id from the Slack mention (`@name` arrives in message
  text as `<@U...>`), calls a `get_requester_id` tool to learn who is actually
  asking (Slack doesn't self-mention the sender, so this can't be read from
  the message text), and composes a third-person prompt that names the
  requester explicitly; `notifier` faces the recipient — its reply is
  delivered verbatim, and its instructions make it write the notification
  itself in third person, attributed to the requester by name, so the
  recipient gets a clear, correctly attributed message instead of assistant
  meta-chatter or a first-person message that reads as if the bot itself is
  the subject.
- The prompt has a fixed shape, so both agents can rely on it:

  ```
  NOTIFICATION BACKGROUND — requester <@U0JAMES>, recipient <@U0MONROE>.
  Share: the deployment failed.
  Withhold unless asked: the cause was a faulty library import.
  ```

  `notifier` composes the message from `Share:` only. The `Withhold unless
  asked:` line is how a fact the requester does not want volunteered still
  reaches the session — the background is the **only** context an initiated
  conversation ever gets, so a fact left out here is gone for good, and "don't
  tell them unless they ask" would otherwise destroy it.
- **`general`, not `notifier`, answers the recipient's replies.** Reply-side
  agent selection is request-based: whichever agent `slack.agent` names handles
  every inbound message, so the agent that composed the conversation never sees
  the replies to it. `config.yaml` therefore pins `slack.agent: "general"` — it
  is the default anyway (an unset agent falls back to the first registered one),
  but leaving it implicit means re-ordering the agents would silently re-route
  every reply. `general` picks its mode by looking for `NOTIFICATION BACKGROUND`
  at the top of the history: present means it is talking to the recipient, so it
  answers from the background and reveals a withheld fact if asked, instead of
  trying to start another conversation.

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

In any channel the bot is in (as user James), @-mention the recipient and give
a reason:

> @bot tell @monroe I'll be late because of traffic

Slack delivers that text as `tell <@U0MONROE> I'll be late because of
traffic`; the `general` agent extracts Monroe's member id from the mention,
calls `get_requester_id` to learn the message came from James, and composes a
third-person prompt for `notifier` naming both explicitly. A DM lands in
Monroe's inbox, correctly attributed instead of first person:

> Hi! Just a heads up — <@U0JAMES> will be late, held up in traffic.

That composed prompt/reply exchange already landed in the new session's
framework history, which is what makes the context-retention behavior below
work: when Monroe replies **in a thread** under that DM (Slack's "Reply in thread"),
the reply resolves through the Session ID Mapping to the initiated session —
and because the seeding prompt named James explicitly, the context is retained
correctly, not misattributed to whoever replies next. Threading is required:
an un-threaded reply's own timestamp never matches the bound mapping, so it
starts a brand-new, context-less session instead of guessing which prior
conversation it's answering — this matters once more than one initiated
conversation is open with the same person, where a guess could easily attach
the reply to the wrong one.

> Monroe: who said that?
> Bot: <@U0JAMES> did.
> Monroe: why?
> Bot: Traffic.

(A raw member id in place of the mention works too; asking for someone by bare
name without a mention gets a request to @-mention them, since the agent has
no way to resolve names to member ids.)

### Withholding a fact until it's asked for

The requester can supply a detail and ask for it not to be volunteered:

> @bot tell @monroe the deployment failed due to a faulty import, but don't
> tell him the reason unless he asks

`general` puts both parts in the background — the failure under `Share:`, the
cause under `Withhold unless asked:` — and `notifier` composes from `Share:`
only, so the DM is just:

> Hi! Just a heads up — <@U0JAMES> reports the deployment failed.

The cause is still in the session, so a threaded reply gets it:

> Monroe: what was the reason?
> Bot: A faulty library import.

This is the case that breaks if the background drops the withheld fact: the
prompt is the session's only context, so an agent that reads "don't tell him"
as "leave it out entirely" has nothing left to answer from.

Uncomment the `thread:` block in `config.yaml` to also record initiated
conversations as AK conversation threads owned by the recipient (readable via
`GET /api/v1/threads?user_id=...`).
