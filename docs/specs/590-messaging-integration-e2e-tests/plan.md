# #590: E2E test harness for messaging integrations — Implementation Plan

## Iteration 1: Slack + Telegram harness (done)

- `e2e/app/` deployable app, `e2e/app/deploy/` terraform (ECS + API Gateway), `e2e/tests/` pytest
  harness with Telethon/user-token senders, one-time helper scripts, `e2e/README.md`.
- Verified: `terraform validate`, `uv lock`, pytest collection (skips without creds), lint.

## Iteration 2: Go live (requester)

- Create Slack app (bot + tester user token) and test channel; create Telegram bot and tester
  user session; deploy via `build.sh` + `deploy.sh`; register both webhooks; run `uv run pytest`.

## Later (separate follow-ups, not scheduled)

- Add WhatsApp / Messenger / Instagram / Gmail once sender accounts exist, following the same
  send-and-poll pattern.
- Optional: the previously drafted in-process nightly send tests as cheap complementary coverage.
