# RescueMesh — Evaluator Guide

This file is a shortcut for IDEALIZE 2026 judges. It does not replace the required `README.md`; it maps the published 40/30/20/10 rubric to concrete, runnable evidence.

## 90-second path — no model key

```bash
uv sync --python 3.12
uv run python command_center.py
```

Open `http://localhost:8000/rescuemesh` and press **Load 60-second scenario**.

Look for all of these on one screen:

- three concurrent incidents with transparent P1/P2 priority;
- a likely duplicate merged into the existing hostel incident;
- verified and verification-pending states kept separate from urgency;
- six community resources;
- one network-wide dry-run plan where no scarce resource appears twice;
- unmet needs disclosed rather than silently ignored;
- **Human confirm** required before any resource is reserved;
- privacy-safe locations and no private contact details on the board;
- the installable offline-first Android Field Relay.

## Five-minute technical verification

```bash
uv run pytest
uv run python demo_scenario.py
```

Then inspect:

- `agent.py` — five-agent handoff graph and native Agent Kernel tool binding;
- `tests/test_agentkernel_integration.py` — schedule-tool injection, Telegram config, and Agent Kernel REST routes;
- `tests/test_session_memory.py` — real `ToolContext` / `Session` continuity across different community users;
- `tools.py` — deterministic priority, duplicate detection, privacy, matching, network allocation, and field idempotency;
- `mobile/README.md` — offline/reconnect Android demo.

A container path is also available:

```bash
docker compose up --build
```

It exposes the same no-key Command Center on port 8000 and keeps the operational ledger in a named `/data` volume.

## Published rubric evidence

| Criterion | Weight | Concrete RescueMesh evidence |
| --- | ---: | --- |
| Idea / Use Case Value | 40% | Disaster coordination overload; trust-aware duplicate merge; verification separate from urgency; privacy-safe public state; scarce-resource allocation across incidents; human decision boundary; SDGs 3/11/13/17. |
| Agent Kernel Usage | 30% | `OpenAIModule`, `OpenAIToolBuilder`, five agents/handoffs, `ToolContext` session memory, native Telegram request handler, Agent Kernel REST/queue pipeline, and injected schedule tools. |
| End Product / Working Solution | 20% | No-key visual Command Center, deterministic terminal scenario, installable Android Field Relay with offline outbox, persistent ledger option, tested Docker image, and automated tests. |
| Documentation / Submission Quality | 10% | Required four README sections plus `SPEC.md`, `AGENTS.md`, `ARCHITECTURE.md`, this evaluator guide, mobile guide, and one-command run paths. |

## Honest prototype boundaries

- RescueMesh is not an emergency number and never autonomously dispatches a responder.
- The local judge deployment uses an atomic JSON ledger rather than a production distributed database.
- OpenAI-backed conversational runs need an API key; Telegram needs bot credentials. The deterministic judge surfaces do not.
- The Android app is a sideloaded competition build, not a Play Store release. Its local HTTP allowance exists so a phone can reach a laptop on the same LAN; production should use HTTPS.
- No claim is made that this prototype is already integrated with government emergency services.

These limitations are intentional and visible rather than hidden behind demo-only claims.
