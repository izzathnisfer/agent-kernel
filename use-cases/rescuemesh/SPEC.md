# RescueMesh Specification

## Product goal

Build a trust-aware community disaster coordination agent using Agent Kernel. The system must convert unstructured incident and volunteer/resource messages into a shared coordination workflow while preserving a human decision boundary around real-world dispatch.

## SDG alignment

- SDG 3: Good Health and Well-being
- SDG 11: Sustainable Cities and Communities
- SDG 13: Climate Action
- SDG 17: Partnerships for the Goals

## Primary user journeys

### Incident reporter

A resident or volunteer reports an affected area, needs, approximate number of people, severity, vulnerable groups, and optional contact/notes. The system registers a structured incident and returns a transparent priority band.

### Duplicate reporter

A second person reports substantially the same location and needs. RescueMesh detects a likely duplicate, merges the report into the existing incident, increments the report count, and preserves the additional report signal.

### Verifier

A trusted volunteer, eyewitness, media reviewer, or official coordinator records a verification event. The system must not fabricate verification evidence.

### Resource donor

A person or organisation offers a boat, vehicle, drinking water, food, shelter, medical assistance, power equipment, or another useful resource. The resource is added to the available inventory with location and availability delay.

### Human coordinator

A coordinator asks for ranked resource matches or a network-wide scarce-resource plan, reviews proposals, explicitly confirms selected pairings, updates incident lifecycle status, requests privacy-safe public briefs, views metrics, and may schedule later follow-ups.

## Agent architecture

Create five agents using OpenAI Agents SDK through Agent Kernel:

1. `rescuemesh`: public-facing routing/triage agent.
2. `rescuemesh_incident_intake`: gathers incident fields and calls reporting tools.
3. `rescuemesh_verifier`: records verification and creates privacy-safe briefs.
4. `rescuemesh_resource_desk`: registers and lists resource offers.
5. `rescuemesh_coordinator`: proposes single-incident matches and network-wide allocations, confirms human-approved matches, changes status, reports metrics, and can access injected scheduling tools when scheduling is enabled.

The routing agent must hand off to specialists rather than duplicate specialist behavior.

## Required Agent Kernel capabilities

- `OpenAIModule` registration.
- `OpenAIToolBuilder.bind` for native coordination tools.
- Agent handoffs among specialist agents.
- `ToolContext` non-volatile session cache for state when an Agent Kernel session exists.
- CLI entry point.
- Telegram user-facing integration with `AgentTelegramRequestHandler`.
- Local REST/queue pipeline using `IOHandler`.
- Judge command center mounted through Agent Kernel `RESTAPI`.
- Local scheduled tasks with `ScheduleRESTRequestHandler`, local provider, in-memory task store, and in-memory queue transport.

## Functional tools

### `report_incident`

Inputs: location, needs, people count, severity, vulnerable groups, optional reporter contact, optional notes.

Must:

- calculate a deterministic priority score and band;
- return visible score reasons;
- compare against active incidents for likely duplicates;
- merge high-similarity duplicates instead of blindly creating a new incident;
- never perform or claim dispatch.

### `verify_incident`

Must attach a verification event supplied by a human/user and change an open incident to verified. It must not infer verification from nothing.

### `register_resource`

Must register resource type, quantity, location, availability delay, provider, optional contact, and notes.

### `match_resources`

Must rank compatible available resources based on need tags, rough location overlap, availability, and quantity. It must only propose matches.

### `network_allocation_plan`

Must consider all active incidents and available resources together, avoid proposing the same resource more than once in a plan, account for incident priority and verification, include a first-coverage fairness signal, report unmet needs, and never mutate/reserve resources.

### `command_center_snapshot`

Must expose a privacy-safe incident/resource/timeline view suitable for the local operations dashboard. It must omit contacts and coarsen fine-grained incident locations.

### `confirm_match`

Must require a non-empty reviewer and reserve a resource only after explicit human approval.

### `public_incident_brief`

Must exclude reporter contact, coarsen fine-grained location, and return only information appropriate for a public volunteer channel.

### `operations_snapshot`

Must expose privacy-safe counts for active incidents, verified incidents, people reported, available resources, confirmed matches, merged duplicates, and priority distribution.

## State model

Use one process-shared community state document with:

- `incidents`: incident ID -> structured incident;
- `resources`: resource ID -> structured resource;
- `timeline`: append-only event records for the running demo/service.

The community ledger must be shared across chat sessions so one resident's incident can be matched with another volunteer's resource offer. Agent Kernel non-volatile session cache is used for per-user conversational context (`last_incident_id`, `last_resource_id`, and `last_area`). This lets a user say "match resources for that incident" without repeating the ID while preserving cross-user coordination. The ledger is in-process by default. If `RESCUEMESH_LEDGER_PATH` is set, state must also be atomically persisted as JSON and recover after process-memory reset. The judge command center enables this local durable mode. A production deployment should replace it with a shared durable backend such as Redis, DynamoDB, or PostgreSQL.

## Human-in-the-loop requirements

- Priority is advisory and transparent.
- Matching is advisory until explicit confirmation.
- `confirm_match` requires a human reviewer identifier.
- The agents must not claim a responder has been dispatched unless a confirmed match exists.
- Immediate-danger responses should also advise contacting the relevant local emergency authority.

## Privacy requirements

- Voluntary contacts may be retained only in the private incident/resource record.
- Public briefs must never include contact fields.
- Public briefs must coarsen house/unit-level location details.
- Verification/public notes should redact obvious phone numbers and email addresses.

## Local run modes

1. No-key visual command center: `uv run python command_center.py`, then open `/rescuemesh`.
2. No-key deterministic terminal scenario: `uv run python demo_scenario.py`.
3. Agent Kernel CLI: `uv run python demo.py` with `OPENAI_API_KEY`.
4. Telegram: `uv run python telegram_server.py` with OpenAI + Telegram credentials.
5. REST/scheduling: `uv run python api_server.py` with OpenAI credentials.

## Quality requirements

- Python 3.12–3.13.x.
- `uv` dependency management.
- Black/isort-compatible code.
- Pytest coverage for core deterministic behavior.
- No secrets in the repository.
- All competition work stays inside `use-cases/rescuemesh`.
