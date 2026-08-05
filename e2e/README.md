# Messaging Integration E2E Test Harness

True end-to-end tests for Agent Kernel's messaging integrations against **real platform
accounts**: a real message is sent by a real user account, delivered by the platform's
webhook to a **deployed ECS instance**, processed by a real OpenAI agent, and the real
reply is read back from the platform. This covers the full transport layer (API Gateway,
webhook routing, Slack signature verification, Telegram secret token) that in-process
tests cannot.

Current coverage: **Slack**, **Telegram**, and **Gmail**. The harness is designed so
further platforms (WhatsApp, Messenger, Instagram) can be added as senders/readers
become available.

## Layout

```
e2e/
  app/                  Deployable agent app: one OpenAI agent + Slack + Telegram handlers
    deploy/             Terraform (yaalalabs/ak-containerized/aws) + Dockerfile + deploy.sh
  tests/                pytest harness that drives the deployed instance
    scripts/            One-time helpers (Telegram login, webhook registration)
```

The deployment is **one-time / long-lived**: deploy once, then run the tests on demand as
often as needed.

## One-time setup

### 1. Slack

1. Create a Slack app in the test workspace (<https://api.slack.com/apps>).
   - Bot token scopes: `chat:write`, `channels:history`, `files:read`.
   - Install to the workspace; note the **bot token** (`xoxb-...`) and **signing secret**.
2. Create a dedicated test channel and `/invite` the bot into it. Note the channel ID.
3. Create a **user token** for the tester account: easiest is to add user token scopes
   (`chat:write`, `channels:history`) to the same app and reinstall; note the `xoxp-...`
   token. The sender must be a user token — the Slack handler ignores bot-authored
   messages, so a second bot cannot drive the test.
4. After deploying (step 3 below), set the app's **Event Subscriptions**:
   - Request URL: the `slack_events_url` terraform output. Slack's URL verification
     challenge is answered automatically by the deployed handler.
   - Subscribe to bot event `message.channels`.

### 2. Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather); note the **bot token** and
   the bot's **username**.
2. Get MTProto API credentials for the tester **user** account at
   <https://my.telegram.org> (API development tools): **api_id** and **api_hash**.
   A real user account is required — Telegram bots cannot message other bots.
3. Generate the tester's session string (one time, interactive):

   ```bash
   cd e2e/tests
   uv sync
   E2E_TELEGRAM_API_ID=... E2E_TELEGRAM_API_HASH=... uv run python scripts/telegram_login.py
   ```

   Export the printed string as `E2E_TELEGRAM_SESSION`. Treat it like a password.
4. Open a chat with the bot from the tester account and send `/start` once (Telegram
   only lets bots message users who initiated a conversation).

### 2b. Gmail (optional)

Needs **two** Gmail accounts: the bot account (the deployment polls its inbox and
replies) and a tester account (the test sends from it and reads the reply). Send-to-self
cannot work — the bot would reply to its own replies in a loop.

1. In [Google Cloud console](https://console.cloud.google.com): create a project, enable
   the **Gmail API**, configure the OAuth consent screen, and create an OAuth client of
   type **Desktop app** — note the client ID and secret.
   - Add both Gmail addresses as test users, and **set the consent screen's publishing
     status to "In production"** — tokens minted while in "Testing" status expire after
     7 days, which silently kills the deployment's Gmail auth.
2. Generate a token for each account (browser opens — pick the right account each time):

   ```bash
   cd e2e/tests
   E2E_GMAIL_CLIENT_ID=... E2E_GMAIL_CLIENT_SECRET=... uv run python scripts/gmail_login.py
   ```

   - Bot account's output → `GMAIL_TOKEN_B64` in `app/.env` (and CI secret
     `E2E_GMAIL_BOT_TOKEN_B64`)
   - Tester account's output → `E2E_GMAIL_TESTER_TOKEN_B64` (test-side env / CI secret)
3. Set `GMAIL_SENDER_FILTER` to the tester's address so the bot ignores stray mail, and
   use a fresh/dedicated bot inbox — on startup the handler processes **all unread
   INBOX mail** passing the filter.

The Gmail integration is optional: when its variables are empty the deployed app runs
Slack + Telegram only.

### 3. Deploy to AWS ECS

The primary deploy path is the `e2e-messaging-deploy` job in the **Weekly Integration
Tests** workflow (`.github/workflows/integration-test-weekly.yaml`) — dispatch it
manually with the `provision_e2e_messaging` input enabled. It must run on a Linux
runner: the
container image vendors Python dependencies at build time, and building from a Mac ships
macOS native extensions that crash the linux/amd64 container.

One-time: add these secrets to the repo's `ci-tests` environment (Settings → Environments
→ ci-tests): `E2E_SLACK_BOT_TOKEN`, `E2E_SLACK_SIGNING_SECRET`, `E2E_TELEGRAM_BOT_TOKEN`,
`E2E_TELEGRAM_WEBHOOK_SECRET` (`OPENAI_API_KEY` already exists). The job applies the
terraform in place (the deployment is long-lived — no destroy step, so the API Gateway
URL and the Slack Event Subscriptions registration stay stable), waits for the ECS
service to stabilize, probes the deployed webhook endpoint, re-registers the Telegram
webhook, and runs the test suite.

Terraform state is remote (`backend.tf` → the shared dev state bucket), so local applies
with `deploy/deploy.sh` (+ `app/.env`, see `.env.example`) operate on the same deployment
— but only use that from a Linux machine, for the wheel reason above.

Adjust `terraform.tfvars` (region, aliases) before the first apply if needed. To use an
existing VPC, set `vpc_id` and `private_subnet_ids`; otherwise the module creates one.

### 4. Register the webhooks

- **Slack**: paste the `slack_events_url` output into the app's Event Subscriptions
  request URL (step 1.4).
- **Telegram**:

  ```bash
  cd e2e/tests
  export E2E_TELEGRAM_BOT_TOKEN=123456:ABC...
  export E2E_TELEGRAM_WEBHOOK_SECRET=...   # same value as TELEGRAM_WEBHOOK_SECRET in app/.env
  uv run python scripts/set_telegram_webhook.py \
    --url "$(terraform -chdir=../app/deploy output -raw telegram_webhook_url)"
  ```

## Running the tests

**Via GitHub Actions (default):** the `e2e-messaging-test` job in the Weekly Integration
Tests workflow probes the deployment, registers the Telegram webhook, and runs the full
pytest suite — weekly on schedule, or on demand via workflow_dispatch. Deployment is a
separate, optional job (`e2e-messaging-deploy`) that runs only when the workflow is
dispatched with `provision_e2e_messaging` enabled; scheduled runs test the existing
deployment as-is. The tests need these in the `ci-tests` environment, in addition to the
deploy secrets above: secrets `E2E_SLACK_USER_TOKEN`, `E2E_TELEGRAM_API_ID`,
`E2E_TELEGRAM_API_HASH`, `E2E_TELEGRAM_SESSION`; variables `E2E_SLACK_CHANNEL_ID`,
`E2E_TELEGRAM_BOT_USERNAME`.

**Locally:**

```bash
cd e2e/tests
uv sync

# Slack
export E2E_SLACK_USER_TOKEN=xoxp-...      # tester user token (sender + reader)
export E2E_SLACK_CHANNEL_ID=C0123456789   # test channel the bot is a member of
export SLACK_BOT_TOKEN=xoxb-...           # or E2E_SLACK_BOT_USER_ID=U... to skip auth.test

# Telegram
export E2E_TELEGRAM_API_ID=...
export E2E_TELEGRAM_API_HASH=...
export E2E_TELEGRAM_SESSION=...           # from scripts/telegram_login.py
export E2E_TELEGRAM_BOT_USERNAME=@your_e2e_bot

uv run pytest -v
```

Tests whose environment variables are missing are **skipped**, so you can run one
platform at a time. Each test sends a uniquely-tagged message and polls up to 3 minutes
for the agent's reply.

## What is asserted

First cut: *the integration works* — the bot replied with something. Specifically:

- a reply from the bot arrived (Slack: threaded under the test message; Telegram: in the
  tester–bot chat), and
- the reply is not one of the handlers' known error-fallback messages (e.g. Slack's
  `"Error handling your request."`), which would mean the transport worked but the agent
  run failed.

Response *content* is deliberately not asserted.

## Environment variable reference

Deployment variables live in `e2e/app/.env` (see `.env.example`); test variables are plain
environment variables.

| Variable | Used by | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | deploy (`app/.env`) | OpenAI key for the deployed agent |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | deploy (`app/.env`) | Slack app credentials for the deployed handler |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_WEBHOOK_SECRET` | deploy (`app/.env`) | Telegram bot credentials for the deployed handler |
| `E2E_SLACK_USER_TOKEN` | tests | Tester user token (`xoxp-`), sends and reads messages |
| `E2E_SLACK_CHANNEL_ID` | tests | Test channel ID |
| `SLACK_BOT_TOKEN` or `E2E_SLACK_BOT_USER_ID` | tests | Identifies which replies came from the bot |
| `E2E_TELEGRAM_API_ID` / `E2E_TELEGRAM_API_HASH` | tests | MTProto app credentials of the tester account |
| `E2E_TELEGRAM_SESSION` | tests | Telethon StringSession of the tester account |
| `E2E_TELEGRAM_BOT_USERNAME` | tests | Deployed bot's username |
| `E2E_TELEGRAM_BOT_TOKEN` / `E2E_TELEGRAM_WEBHOOK_SECRET` | webhook script | One-time Telegram webhook registration |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_TOKEN_B64` / `GMAIL_SENDER_FILTER` | deploy (`app/.env`) | Bot Gmail account OAuth + sender allowlist (CI: `E2E_GMAIL_CLIENT_ID`, `E2E_GMAIL_CLIENT_SECRET`, `E2E_GMAIL_BOT_TOKEN_B64`, variable `E2E_GMAIL_TESTER_ADDRESS`) |
| `E2E_GMAIL_TESTER_TOKEN_B64` | tests | Tester Gmail account token (base64 pickle) |
| `E2E_GMAIL_BOT_ADDRESS` | tests | Bot Gmail address the deployment polls (CI: variable) |

## Troubleshooting

- **Slack URL verification fails**: the ECS service may still be starting — re-run after
  `deploy.sh` reports the service stable. Check API Gateway/ECS logs in CloudWatch.
- **No Telegram reply**: run `set_telegram_webhook.py` again and inspect the printed
  `getWebhookInfo` — `last_error_message` shows delivery failures (e.g. secret mismatch).
- **Slack test times out but the bot replied in-channel (not in-thread)**: an unthreaded
  bot message is the handler's error path — check the agent/OpenAI key configuration.
