# RescueMesh Architecture

## Design objective

Emergency coordination has two different kinds of uncertainty: **language uncertainty** (people describe situations informally) and **operational uncertainty** (reports may be duplicates, unverified, stale, or mismatched with resources). RescueMesh uses an LLM for conversational understanding and routing, while keeping the operational state transitions in deterministic tools.

That split is deliberate: the generative model can interpret human messages, but the code owns priority calculation, duplicate similarity, privacy filtering, matching scores, and the confirmation gate.

## Components

### 1. Public triage agent

`rescuemesh` is the Telegram/CLI-facing entry point. It has no operational tools of its own. Its job is to identify intent and hand the conversation to the correct specialist.

### 2. Incident intake agent

Turns a natural-language report into tool arguments. `report_incident` then computes a transparent priority, searches for likely duplicates among active incidents, and either creates or merges the incident.

### 3. Verification agent

Keeps trust separate from urgency. A report can be high priority while still unverified. `verify_incident` records who/what supplied a verification signal; it does not manufacture evidence.

### 4. Resource desk agent

Structures resource offers from volunteers or organisations. Contact details stay in the private record and are omitted from public inventory output.

### 5. Coordinator agent

Combines incident and resource state. `match_resources` ranks options but does not mutate dispatch state. `confirm_match` performs the actual reservation only after an explicit human reviewer is supplied.

## Deterministic priority model

The score combines:

- severity baseline;
- capped people-count contribution;
- extra weight for rescue/evacuation and medical needs;
- smaller weight for water and shelter needs; and
- capped vulnerable-group contribution.

The tool returns both a numeric score and human-readable reasons. The score maps to P1/P2/P3/P4 review bands. It is not presented as a medical or official emergency classification.

## Duplicate model

For each active incident, RescueMesh calculates:

- token Jaccard similarity of the reported area/location; and
- Jaccard similarity of normalized need tags.

Location carries more weight than need tags. A high combined threshold merges the new report into the existing incident, increments `report_count`, and records a duplicate event. This reduces coordination noise without discarding corroboration.

## Resource matching model

Only available resources with at least one compatible need tag are considered. The score combines:

- strong base weight for need compatibility;
- rough location-token overlap;
- availability delay; and
- a small capped quantity bonus.

The result is a ranked proposal. The proposal cannot reserve a resource.

## Privacy model

Private state may contain voluntarily supplied contacts. Public briefs:

- omit the contact fields entirely;
- remove common house/unit number patterns and standalone fine-grained numbers from the location; and
- include only area, needs, people count, priority band, status, verification state, and report count.

Obvious phone numbers and email addresses are redacted from verification/status notes that may be surfaced later.

## Shared operational state vs. session memory

A community coordination system cannot silo each Telegram user's report inside that user's private chat session. RescueMesh therefore separates two kinds of state:

- **Process-shared operational ledger:** incidents, resources, and the event timeline are visible to all sessions in the running service, enabling cross-user matching.
- **Agent Kernel session memory:** each user session remembers the last incident ID, last resource ID, and last area it interacted with. Specialist tools can reuse that context when the user omits an incident ID.

This competition implementation uses an in-process community ledger for zero-infrastructure local execution. Production would replace that ledger with Redis/DynamoDB or another shared durable store while keeping Agent Kernel session memory for per-user conversational context.

## Agent Kernel runtime modes

### CLI / Telegram

`config.yaml` uses Agent Kernel in-memory session storage for per-user conversational context. Telegram is configured with `rescuemesh` as the default agent; the service process shares the operational ledger across those sessions.

### Scheduled REST pipeline

`api_server.py` sets `AK_CONFIG_PATH_OVERRIDE=config.schedule.yaml` before importing Agent Kernel. That config enables:

- local schedule provider;
- in-memory schedule task store;
- `rest_sync` execution;
- in-memory queue transport; and
- scheduling tools only for `rescuemesh_coordinator`.

`ScheduleRESTRequestHandler` adds schedule-management routes while `IOHandler` boots the single-process queue pipeline required for scheduled occurrences.

## Judge-friendly verification

`demo_scenario.py` calls the exact deterministic tools used by the agents, so the highest-value behavior can be inspected without creating third-party accounts or providing an LLM key. The real Agent Kernel CLI and Telegram modes then demonstrate the conversational/messaging layer on top of the same logic.
