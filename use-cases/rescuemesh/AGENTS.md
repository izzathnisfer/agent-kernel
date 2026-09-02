# AGENTS.md — RescueMesh

This folder is a competition use case built **with** Agent Kernel. Do not modify Agent Kernel core to change RescueMesh behavior.

## Non-negotiable product boundaries

- RescueMesh is coordination decision support, not an emergency dispatch system.
- Never remove the explicit human confirmation requirement from `confirm_match`.
- Keep `network_allocation_plan` dry-run only: it may propose, but never reserve or dispatch a resource.
- Never let an LLM generate the authoritative priority score; priority comes from the deterministic tool.
- Never expose `reporter_contact` or resource `contact` in `public_incident_brief`.
- Never fabricate verification evidence.
- Keep all project changes inside this folder.

## Agent Kernel conventions

- Register agents through `OpenAIModule`.
- Bind Python tools through `OpenAIToolBuilder.bind`.
- Preserve the specialist handoff architecture instead of turning the system into one giant prompt.
- Use `ToolContext` session state when available and retain the direct-call fallback so tests remain no-key and deterministic.
- `config.yaml` is the CLI/Telegram configuration.
- `api_server.py` overrides the config path to `config.schedule.yaml` before importing Agent Kernel so scheduled tasks use the required queue pipeline.
- `command_center.py` uses `config.command-center.yaml` and mounts a judge-facing router into Agent Kernel `RESTAPI`; keep that path runnable without an LLM key.
- Operational state is in-memory unless `RESCUEMESH_LEDGER_PATH` is set. Keep persisted ledger files out of git and keep per-user Agent Kernel session memory separate from community state.
- The Android Field Relay is a thin offline-first client. Do not move priority, duplicate, verification, allocation, or dispatch decisions onto the device. Preserve request-id idempotency for field retries.

## Before considering a change complete

```bash
uv run black --check .
uv run isort --check-only .
uv run pytest
uv run python demo_scenario.py
```

Do not commit credentials, generated virtual environments, caches, or local logs.
