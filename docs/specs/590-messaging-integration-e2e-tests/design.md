# #590: E2E test harness for messaging integrations using real platform accounts

Test Agent Kernel's messaging integrations end-to-end against real accounts: a real user sends a
message on the real platform, the platform delivers it via webhook to a **long-lived AWS ECS
deployment** running one OpenAI agent with all integrations enabled, and the test reads the
agent's real reply back from the platform.

## Motivation

- The existing nightly tests for `examples/api/{slack,whatsapp,...}` are health-check-only smoke
  tests — nothing drives a message through a platform and verifies a reply arrives.
- Mocked tests can't catch webhook payload drift, credential/scope issues, or transport-layer
  breakage (API Gateway routing, Slack signature verification, Telegram secret token). Only a
  deployed instance receiving real platform webhooks covers that layer.

## Design

- **One one-time deployment** (`e2e/app/`): a single `app.py` registers one OpenAI agent
  (`gpt-4.1-mini`) and serves all enabled integrations via
  `RESTAPI.run([AgentSlackRequestHandler(), AgentTelegramRequestHandler()])`.
- **Infra** (`e2e/app/deploy/`): the existing `yaalalabs/ak-containerized/aws` terraform module
  (ECS + HTTPS API Gateway via VPC link), with two custom `gateway_endpoints` exposing
  `/slack/events` and `/telegram/webhook`. Webhook URLs are terraform outputs. Secrets live in a
  gitignored `e2e/app/.env` that `deploy.sh` loads and exports as `TF_VAR_*`.
- **Test harness** (`e2e/tests/`): plain pytest, run by the `e2e-messaging` job in the weekly
  integration test workflow (schedule + manual dispatch) or locally on demand. Credentials come
  from env vars (same mechanism as existing e2e tests); tests skip when creds are missing.
  - Slack: send as a real user via a user token (`xoxp-`) — required because the handler reads
    `body["user"]`, absent on bot-authored messages — then poll `conversations.replies` for the
    bot's threaded reply.
  - Telegram: send from a real user account via Telethon/MTProto (bots can't message bots), then
    poll the chat for the bot's reply. One-time helpers generate the user session string and
    register the webhook.
- **Assertion depth (first cut)**: the bot replied with *something*, and the reply is not one of
  the handlers' known error-fallback strings (which would mean transport OK but agent run failed).
  Reply content is not asserted.

## Scope

- Covered: **Slack + Telegram + Gmail + WhatsApp + Messenger**. Instagram follows the same
  Meta-webhook pattern once added.
- Messenger: constructible and deployable, but its inbound leg can NEVER be automated — the
  Messenger Platform has no API to send a message to a Page as a user, so only a real human DM
  triggers it (worse than WhatsApp, which a production sender number would unlock). Verified
  manually; the automated test skips unless `E2E_MESSENGER_AUTOMATED=1` (a log-based check of a
  recent human-triggered round trip).
- Teams remains blocked: `core/config.py` still has no `_TeamsConfig` / `teams:` field, so its
  handler cannot be constructed.
- WhatsApp: sender is a second business number in a **separate Meta app** (shared app → the bot
  answers its own replies in a loop); inbound is the pre-approved `hello_world` template
  (business-initiated messages must be templates). No read-back API exists, so verification polls
  the deployment's CloudWatch logs for the handler's Graph-API send-success line (contains the
  recipient wa_id) and fails on an agent-error log — proves webhook → agent → accepted send, not
  human receipt.
- Gmail: polling-based (no webhook route); the app starts the poll loop in a background thread.
  Two Gmail accounts (bot + tester — send-to-self would loop); OAuth token.pickle is generated
  interactively once and injected base64 via env; sender filter restricts processing to the
  tester address.
- Requester supplies the real accounts: Slack app + tester user token + channel, Telegram bot +
  tester user's MTProto credentials, Google OAuth client + two Gmail accounts.

## Non-goals

- No per-PR/nightly wiring — weekly schedule plus manual dispatch only.
- No content/quality assertions on agent replies.
- Does not replace the previously drafted in-process nightly send tests (cheap outbound-only
  coverage, no deployment); that remains a possible complementary follow-up.
- Teams out of scope (not constructible today — no `_TeamsConfig`).
