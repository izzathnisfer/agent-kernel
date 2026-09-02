# AGENTS.md — RescueMesh

This folder is a competition use case built **with** Agent Kernel. Do not modify Agent Kernel core to change RescueMesh behavior.

## Non-negotiable product boundaries

- RescueMesh is coordination decision support, not an emergency dispatch system.
- Never remove the explicit human confirmation requirement from `confirm_match`.
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

## Before considering a change complete

```bash
uv run black --check .
uv run isort --check-only .
uv run pytest
uv run python demo_scenario.py
```

Do not commit credentials, generated virtual environments, caches, or local logs.
