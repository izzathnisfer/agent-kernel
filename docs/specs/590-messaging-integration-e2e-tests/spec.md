# #590: E2E test harness for messaging integrations — Implementation Spec

Implements `design.md`. Everything lives under `e2e/` at the repo root.

## Layout

```
e2e/
  README.md               Setup guide, env-var reference, troubleshooting
  app/                    Deployable agent app
    app.py                One OpenAI agent ("general", gpt-4.1-mini) + Slack + Telegram handlers
    config.yaml           slack.agent / telegram.agent = "general"; no slack acknowledgement
    deploy/               Terraform + Dockerfile + deploy.sh (module yaalalabs/ak-containerized/aws)
  tests/
    conftest.py           require_env() skip helper; shared poll timeouts (180s / 5s)
    test_slack.py         User-token send → poll conversations.replies for threaded bot reply
    test_telegram.py      Telethon user-session send → poll chat for bot reply
    test_gmail.py         Tester-account send → poll tester's thread for bot reply (300s budget)
    test_whatsapp.py      Sender-app template send → poll CloudWatch logs for send-success (no read-back API)
    test_messenger.py     Log-based check of a human-triggered round trip (no user→Page send API); skips by default
    scripts/telegram_login.py        One-time StringSession generation
    scripts/set_telegram_webhook.py  One-time setWebhook registration
    scripts/gmail_login.py           One-time OAuth token generation (bot + tester accounts)
```

## Deployment

- Gateway endpoints: `POST …/api/v1/slack/events → /slack/events`,
  `POST …/api/v1/telegram/webhook → /telegram/webhook`; both exported as terraform outputs
  (`slack_events_url`, `telegram_webhook_url`).
- Container env: `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
  `AK_TELEGRAM__BOT_TOKEN`, `AK_TELEGRAM__WEBHOOK_SECRET` — all sourced from sensitive terraform
  variables. Primary deploy path is the optional `e2e-messaging-deploy` job in the weekly
  integration test workflow (`.github/workflows/integration-test-weekly.yaml`) — runs only on
  manual dispatch with the `provision_e2e_messaging` input enabled (`ci-tests` environment
  secrets); it must build on Linux so vendored native wheels match the linux/amd64 image. Unlike
  the workflow's matrix jobs the deployment is never destroyed — the API Gateway URL must stay
  stable because Slack's Event Subscriptions registration is manual. Terraform state is remote
  (`backend.tf` → shared dev state bucket), so local `deploy.sh` (+ gitignored `e2e/app/.env`)
  applies the same deployment — Linux-only for the wheel reason.
- `slack.agent_acknowledgement` deliberately unset so the only *threaded* bot reply is the real
  agent response (the Slack error fallback posts unthreaded), keeping the assertion unambiguous.

## Tests

- Env vars (tests skip if missing): `E2E_SLACK_USER_TOKEN`, `E2E_SLACK_CHANNEL_ID`,
  `SLACK_BOT_TOKEN` (or `E2E_SLACK_BOT_USER_ID`), `E2E_TELEGRAM_API_ID`, `E2E_TELEGRAM_API_HASH`,
  `E2E_TELEGRAM_SESSION`, `E2E_TELEGRAM_BOT_USERNAME`.
- Each test sends a uniquely-tagged prompt, polls up to 180s, and fails if the reply matches a
  known handler error-fallback string (transport OK but agent run failed).
- The `e2e-messaging-test` job probes the deployment, re-registers the Telegram webhook
  (idempotent), and runs the suite weekly (or on manual dispatch); it runs after
  `e2e-messaging-deploy` when that job was requested, otherwise against the existing deployment.
  Tests can also run locally with the same env vars.
- Slack sender must be a user token: the handler reads `body["user"]`, absent on bot messages.
- Telegram sender must be a real user account (MTProto): bots cannot message bots.

## Verification performed

- `terraform validate` passes; `uv lock` resolves both packages; `pytest` collects and skips
  cleanly without credentials; black/isort clean at line length 120.
