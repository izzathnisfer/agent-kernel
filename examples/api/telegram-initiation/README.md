# Agent-Initiated Telegram Conversations

Demonstrates agent-initiated conversations in a single-process REST
deployment: an agent proactively messages a Telegram chat, and that chat's
reply continues the same conversation instead of starting a context-less one.

## How it works

- `conversation_initiation.enabled: true` in `config.yaml` enables the
  feature for this single-process REST deployment (queue-mode deployments
  auto-enable): the `initiate_conversation` system tool is registered on all
  agents, and inbound Telegram messages resolve through the Session ID
  Mapping.
- `TelegramInitiationHandler` (in `server.py`) plays both handler roles:
  - as an `AgentTelegramRequestHandler`, it resolves inbound messages to
    initiated sessions — Telegram has no per-message "thread" concept, so the
    **chat id itself** is the mapped identifier: any message from that chat
    continues the conversation, no special reply action needed;
  - as an `InitiationSender`, its `send_initiation_message()` is the send
    point — `RESTAPI.run()` detects it and registers the in-process
    dispatcher, which sends and then binds the `session_id <-> chat id`
    mapping automatically.
- When an agent is asked to contact someone, the tool creates a fresh
  session, composes the outbound message by running an agent with your
  prompt (so the new session's history already contains the exchange), and
  dispatches it. There's no separate context channel — the `prompt` argument
  both seeds the new session and produces the outbound message — so it has to
  be self-disambiguating: Telegram has no reliable signal for who is chatting
  with the bot, so the requester must name themselves directly in the
  request.
- Two agents split the roles, grounding identity explicitly instead of
  forwarding raw phrasing: `general` faces the requester — it extracts the
  recipient's chat id and the requester's stated name directly from the
  message text, and composes a third-person prompt naming both explicitly;
  `notifier` faces the recipient — its reply is delivered verbatim, and its
  instructions make it write the notification itself in third person,
  attributed to the requester by name.

## Prerequisites

Same Telegram Bot API setup as the plain [`examples/api/telegram`](../telegram/README.md)
example: a bot created via [@BotFather](https://t.me/botfather) and a webhook
configured to point at this server.

**Bots can't cold-message a user**: Telegram only lets a bot message a chat
that has already started a conversation with it. To try this demo, message
the bot from the recipient's Telegram account first (any message, even
"hi") — the recipient's chat id is only reachable by the bot afterward.

## Setup

1. Export the Telegram and OpenAI credentials:

   ```bash
   export AK_TELEGRAM__BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
   export OPENAI_API_KEY="your_openai_api_key"
   ```

2. Build and run:

   ```bash
   ./build.sh
   uv run server.py
   ```

3. Expose the local server (e.g. `ngrok http 8000`) and register the webhook
   with Telegram, pointed at `https://your-tunnel-url.com/telegram/webhook`.

## Try it

From a Telegram account that has already messaged the bot (see the
"can't cold-message" note above), get that chat's id (e.g. from the server
logs after messaging the bot), then message the bot as James:

> tell chat 555555 that James will be late because of traffic

The `general` agent extracts the recipient's chat id and James's name from
the text, composes a third-person prompt for `notifier` naming both
explicitly. The recipient receives:

> Hi! Just a heads up from James — he will be late, held up in traffic.

When that recipient replies, the reply resolves through the Session ID
Mapping to the initiated session — and because the seeding prompt named James
explicitly, the context is retained correctly:

> Recipient: who said that?
> Bot: James did.
> Recipient: why?
> Bot: Traffic.

Uncomment the `thread:` block in `config.yaml` to also record initiated
conversations as AK conversation threads owned by the recipient (readable via
`GET /api/v1/threads?user_id=...`).
