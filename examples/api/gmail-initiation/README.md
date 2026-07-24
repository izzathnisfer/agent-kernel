# Agent-Initiated Gmail Conversations

Demonstrates agent-initiated conversations in a single-process polling
deployment: an agent proactively emails someone, and that person's reply
continues the same conversation instead of starting a context-less one.

> **⚠️ This bot reads AND REPLIES TO real email the moment it starts.** Unlike
> the webhook-based examples (which only act when a request arrives), Gmail's
> poll loop starts acting on whatever matches `label_filter` as soon as you run
> `server.py` — including unread mail already sitting there, not just messages
> you send afterward. Do **not** point this at your real inbox. See "Scope it
> before you run it" below.

## Scope it before you run it

1. In Gmail, create a label (any name; `ak-initiation-test` matches the
   `config.yaml` default) and manually apply it only to emails you want this
   bot to see. `label_filter` in `config.yaml` is set to that label, not
   `INBOX` — leave it that way.
2. Add a second layer of scoping via env vars (both optional, both
   comma-separated, checked in `_should_process_email`):
   ```bash
   export AK_GMAIL__SENDER_FILTER="your-own-test-address@gmail.com"
   export AK_GMAIL__SUBJECT_FILTER="AK-TEST"
   ```
   With these set, an email only gets processed if it's *also* from an
   allowed sender / has a matching subject keyword — so even a mislabeled
   email from someone else is skipped.
3. Safest option: use a dedicated test Gmail account for this, the same way
   you'd use a dedicated test WhatsApp/Telegram account — not your primary
   inbox.

If you already ran this against your real inbox: **Ctrl+C the process now**
(or `pkill -f "python3 server.py"` if it's backgrounded). That stops further
polling, but does not undo replies already sent — check your Sent folder for
anything that went to a real person.

## How it works

- `conversation_initiation.enabled: true` in `config.yaml` enables the
  feature: the `initiate_conversation` system tool is registered on all
  agents, and inbound emails resolve through the Session ID Mapping by Gmail
  thread id (already the identifier `AgentGmailRequestHandler` uses for
  session continuity).
- Gmail has no webhook/REST surface in this integration — it's a polling
  loop, not a `RESTAPI.run()` server — so this example can't rely on the
  automatic `InitiationSender` detection the other `-initiation` examples
  use. `register_initiation_dispatcher()` in `server.py` wires the dispatcher
  manually at startup, applying the same send-then-bind contract
  `RESTAPI._register_initiation_sender()` applies for REST deployments.
- `GmailInitiationHandler` implements `InitiationSender.send_initiation_message()`
  by composing a fresh email via the authenticated Gmail API client and
  returning the new message's `threadId` — a reply landing in that thread
  resolves back to the initiated session automatically.
- When an agent is asked to contact someone, the tool creates a fresh
  session, composes the outbound message by running an agent with your
  prompt (so the new session's history already contains the exchange), and
  dispatches it. There's no separate context channel — the `prompt` argument
  both seeds the new session and produces the outbound message — so it has to
  be self-disambiguating: the recipient has no way to see who asked unless
  the agent restates it explicitly, so the requester should name themselves
  in the request (or the agent falls back to the inbound email's own
  `From:` header when available).
- Two agents split the roles, grounding identity explicitly instead of
  forwarding raw phrasing: `general` faces the requester — it extracts the
  recipient's email address and the requester's identity from the request
  text (or the `From:` header already visible in its own inbound prompt),
  and composes a third-person prompt naming both explicitly; `notifier`
  faces the recipient — its reply is delivered verbatim as the email body,
  attributed to the requester by name.

## Prerequisites

Same Google Cloud / OAuth2 setup as the plain [`examples/api/gmail`](../gmail/README.md)
example: a Google Cloud project with the Gmail API enabled and OAuth2
credentials (client ID and secret).

## Setup

1. Export the OAuth2 and OpenAI credentials (see the plain Gmail example's
   README for the full Google Cloud Console walkthrough):

   ```bash
   export AK_GMAIL__CLIENT_ID="your-google-client-id"
   export AK_GMAIL__CLIENT_SECRET="your-google-client-secret"
   export OPENAI_API_KEY="your_openai_api_key"
   ```

2. Build and run:

   ```bash
   ./build.sh
   uv run server.py
   ```

3. On first run, a browser window opens for Google sign-in; approve access
   and the bot starts polling.

## Try it

Send the bot's Gmail address a test email from your allowed test sender
address (and/or with your subject filter keyword — see "Scope it before you
run it"), and apply your test label to it. As James:

> Subject: Notify Monroe [AK-TEST]
>
> please email monroe@example.com and tell her the report is ready — James

The `general` agent extracts Monroe's address and James's name, composes a
third-person prompt for `notifier` naming both explicitly. Monroe receives a
new email:

> Hi! Just a heads up from James — the report is ready.

When Monroe replies to that email, the reply resolves through the Session ID
Mapping to the initiated session — and because the seeding prompt named James
explicitly, the context is retained correctly:

> Monroe: who said that?
> Bot: James did.
> Monroe: what report?
> Bot: The one James asked me to let you know was ready.

Add a `thread:` block to `config.yaml` to also record initiated conversations
as AK conversation threads owned by the recipient — reading them back over
`GET /api/v1/threads?user_id=...` needs a separate REST process mounting
`ThreadRESTRequestHandler`, since this example is a polling process with no
REST surface of its own.
