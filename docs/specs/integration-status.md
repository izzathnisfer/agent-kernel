# Integration Status Dashboard

A public, auto-updating status page on the documentation site (kernel.yaala.ai) that shows red/green health for every integration Agent Kernel supports — agent frameworks, cloud deployment variants (serverless / containerized on AWS, Azure, GCP), memory backends, messaging integrations, guardrails, multimodal storage, and core library tests — derived from the latest GitHub Actions runs on the source branch.

> **Source branch**: `develop`. Three places carry the branch name: the `publish_status` input expression in `test.yaml`, the `if:` conditions of the "Publish tile status" steps in the two integration workflows, and the `SOURCE_BRANCH` constant in `docs/src/pages/status.tsx`.

## Goals

1. **Public visibility** — anyone can see, at a glance, which integrations are currently passing or failing, and when that data was last refreshed.
2. **Zero-touch maintenance** — when a developer adds a test to `.github/test-config.yaml` or `.github/integration-test-config.yaml`, the new integration appears on the dashboard automatically. Categorization and labeling live in those same config files.
3. **Complete catalog** — every configured test is visible on the page, including ones that have never published a run (shown as "no data"), so coverage gaps are visible instead of silently absent.
4. **Honest data** — every status is traceable to a real workflow run (each tile links to the run), and stale data is visually flagged rather than silently shown as current.
5. **History** — when a new status is published for an item, the previous latest status is moved into that item's history, so the page can show recent trends (last-N-runs strip, "failing since").

## Data sources

All three pipelines run matrix jobs generated from the config YAMLs by `.github/scripts/generate_test_matrix.py`:

| Workflow | Config source | Tells us | Cadence |
|---|---|---|---|
| `test.yaml` → `test-reusable.yaml` ("Test") | `.github/test-config.yaml` (`e2e` tier) | Framework adapters (OpenAI, CrewAI, LangGraph, ADK), CLI/API features (structured output, hooks, multimodal, MCP, A2A, knowledge base, guardrails), unit tests | Every push to the source branch |
| `integration-test.yaml` ("Nightly Integration Tests") | `.github/integration-test-config.yaml` (`nightly` tier) | Messaging integrations (Slack, Telegram, Messenger, WhatsApp, Instagram, Gmail), multimodal storage (DynamoDB, Redis) | Manual (`workflow_dispatch`) for now |
| `integration-test-weekly.yaml` ("Weekly Integration Tests") | `.github/integration-test-config.yaml` (`weekly` tier) | Cloud deployment variants (AWS/Azure/GCP × serverless/containerized), memory backends (Redis, DynamoDB, Cosmos, Firestore), framework-on-cloud combos | Manual (`workflow_dispatch`) for now |

The integration pipelines' cron schedules are intentionally commented out while the pipelines themselves are being stabilized — that is fine for the dashboard: publishing happens per test job on every run regardless of trigger, so manually dispatched runs populate the page, and the staleness badge communicates data age honestly. Re-enabling the crons later requires no dashboard changes.

The repository is public, so both the GitHub Actions API and raw file contents are publicly readable.

## Architecture

```
.github/test-config.yaml ─────────────┐  (dashboard metadata + tile catalog)
.github/integration-test-config.yaml ─┤
                                      ▼
   ┌───────────────────────────────────────────────────┐
   │ DISTRIBUTED publishing: every test job ends with  │
   │ a "Publish tile status" step (if: always()) that  │
   │ runs publish_integration_status.py to publish     │
   │ that job's OWN tiles the moment its test ends:    │
   │  1. resolves its tiles from the config metadata   │
   │  2. maps job.status / step outcome to a status    │
   │  3. compare-and-swap push onto the orphan         │
   │     `status-data` branch (fetch tip, patch JSON,  │
   │     orphan commit, push --force-with-lease pinned │
   │     to the fetched tip; rejected push -> retry)   │
   └───────────────────────────────────────────────────┘
                                      ▼
   https://raw.githubusercontent.com/yaalalabs/agent-kernel/status-data/…
   (status/<workflow>.json + history/<workflow>.jsonl)
                                      ▼
   ┌───────────────────────────────────────────────────┐
   │ Docusaurus page /status (docs/src/pages/status)   │
   │  - fetches status + history JSON client-side      │
   │  - fetches the config YAMLs from the source       │
   │    branch and builds the full tile catalog, so    │
   │    never-run tests render as "no data"            │
   │  - renders category sections with pass/fail/     │
   │    no-data tiles and per-tile history strips      │
   └───────────────────────────────────────────────────┘
```

### Distributed publishing

Publishing is per test job, not a central end-of-run job: each matrix job (and the unit/script test jobs, and the `deploy-openai` base job) publishes its own tiles as its final step with `if: always()`. Consequences:

- A tile updates the moment its test finishes — no waiting for the whole run.
- Re-running one failed test republishes exactly that tile with the corrected outcome (the step passes `job.status`, which reflects the re-run attempt).
- The weekly `deploy-openai` job composes its outcome in the workflow expression — `(job.status == 'failure' || steps.openai-test.outcome == 'failure') && 'failure' || 'success'` — because its test step runs with `continue-on-error`.
- **Write access is caller-controlled and develop-only.** `test-reusable.yaml` deliberately declares no `permissions` — a called workflow inherits exactly the calling job's token grant. `test.yaml` therefore has two mutually exclusive caller jobs: `run-tests` handles `pull_request` events with `contents: read` and `publish_status: false` (PR code never holds a write token, and the job name keeps required PR checks stable), while `run-tests-publish` handles push/dispatch events with `contents: write` and `publish_status: ${{ github.ref == 'refs/heads/develop' }}`. The safe-to-test flow (`test-trusted-pr.yaml`, which runs labeled fork code in the privileged `pull_request_target` context) explicitly grants `contents: read`, so reviewed fork code still cannot push anywhere. In the dispatch-only integration workflows, the test jobs declare `contents: write` at job level (re-declaring `id-token: write` for cloud OIDC, since a job-level block replaces the workflow-level one); their publish steps are gated on `github.ref == 'refs/heads/develop'`, so dispatches from other branches run tests but never publish.

**Concurrency** is handled with an atomic compare-and-swap instead of locking: the publisher fetches the `status-data` branch tip, patches the JSON in memory, builds a single **orphan** commit containing the previous tree plus the patched files (git plumbing: `read-tree` → `hash-object` → `update-index` → `write-tree` → `commit-tree`, no working-tree checkout), and pushes with `--force-with-lease=refs/heads/status-data:<fetched-tip>`. If another job pushed in between, the lease fails, and the publisher refetches and retries (up to 10 attempts with randomized backoff). Because every commit is an orphan built on the latest fetched tree, the branch always holds exactly one commit — no history growth — while concurrent updates are never lost.

### Why a published-JSON branch instead of alternatives

- **Client-side GitHub Actions API parsing (no workflow changes)** — rejected. Unauthenticated API calls are limited to 60/hr per viewer IP, and job-name string parsing is brittle.
- **Build-time data baked into the docs site** — rejected. `deploy-docs.yml` only runs when `docs/**` changes, so statuses would go stale immediately.
- **Committing JSON into the source branch** — rejected. Pollutes history and triggers CI loops.

`raw.githubusercontent.com` serves the branch with CORS enabled and no meaningful rate limits (responses are CDN-cached for ~5 minutes, which is acceptable).

## Config file changes

Each test entry in both config files accepts a new optional `dashboard` block — either a single mapping or a **list of mappings** when one test should surface as multiple dashboard tiles:

```yaml
# .github/integration-test-config.yaml
nightly:
  tests:
    - type: api
      path: examples/api/slack
      dashboard:
        category: Messaging Integrations
        label: Slack (nightly)
        description: Slack events + Web API round-trip   # optional, tile tooltip

weekly:
  tests:
    # One test, two dashboard tiles: it proves both the Cosmos memory
    # backend and the Azure serverless deployment path.
    - type: azure-serverless
      path: examples/memory/cosmos
      deploy_dir: deploy
      dashboard:
        - category: Agent Memory / Knowledge
          label: Cosmos DB memory
        - category: Azure Serverless
          label: OpenAI + Cosmos memory
    - type: api
      path: examples/api/internal-thing
      dashboard: hidden        # excluded from the dashboard entirely
```

Rules:

- **`dashboard`** — a single mapping, a list of mappings, or the literal `hidden`. Each mapping produces one tile; all tiles from the same test share the same underlying status and link to the same job.
- **`category`** (string) — the section the tile renders under. Free-form; new categories appear on the page automatically.
- **`label`** (string) — tile display name. Must be unique within its category (validator-enforced). The pair `(category, label)` is the tile's **identity key** — history events are matched on it, so renaming either starts a fresh history for that tile.
- **`logo`** (string, optional) — tile logo: a file name under `docs/static/img/integrations/` (e.g. `redis.svg`) or a site-absolute path starting with `/`. When omitted, the status page falls back to keyword matching on the test's path/label and then to a cloud mark by test type; the validator warns when a referenced logo file doesn't exist.
- **`description`** (string, optional) — shown in the tile's hover tooltip along with the example path.
- **Defaults when `dashboard` is omitted** — the entry still appears as a single tile: `label` derived from the path (last two segments), `category` derived from `type` via a small default map. The same defaults are implemented in `dashboard_config.py` (used by the validator and publisher) and mirrored in `status.tsx` (used for the catalog).
- **`dashboard: hidden`** — opt-out for entries that shouldn't be public-facing.
- The `deployment_base` entry (`examples/aws-serverless/openai`) also gets a `dashboard` block so the base AWS serverless deployment shows up.

Categories in the initial mapping: **Core & Frameworks**, **API Features**, **Multimodal**, **Guardrails**, **Messaging Integrations**, **Agent Memory / Knowledge** (memory backends + knowledge base), and one section per cloud variant (**AWS/Azure/GCP × Serverless/Containerized**). The memory tests each carry two dashboard entries — one under Agent Memory / Knowledge and one under their cloud-variant category — so both axes stay populated from a single test run.

`validate_integration_config.py` validates both config files by default — `dashboard` shapes, string types, no duplicate `category` within one test's list, and unique `(category, label)` across the whole file set — and runs in the `setup` jobs of all three pipelines.

## Publisher

`.github/scripts/publish_integration_status.py`, invoked per job:

```bash
python3 .github/scripts/publish_integration_status.py \
  --workflow integration-test \
  --path examples/api/slack \        # or --synthetic unit-tests|script-tests
  --outcome ${{ job.status }}
```

1. Resolves the job's tiles from the config metadata (`--path`) or the built-in synthetic tile table (`--synthetic`, for the non-matrix `unit-tests` / `script-tests` jobs).
2. Maps the outcome: `success` → `pass`, `failure` → `fail`, `cancelled`/`skipped` → `skipped`.
3. Best-effort job-link lookup: queries the run's jobs (`filter=latest`, so re-runs link the latest attempt) and matches its own job by the test path embedded in matrix job names, falling back to `GITHUB_JOB`.
4. CAS-updates `status/<workflow>.json` and `history/<workflow>.jsonl` as described above.

### Data format

`status/<workflow>.json` — one entry per tile with per-tile run metadata:

```json
{
  "workflow": "integration-test-weekly",
  "workflow_name": "Weekly Integration Tests",
  "branch": "develop",
  "commit": "d728a3e9",
  "updated_at": "2026-07-13T03:41:22Z",
  "expected_cadence_hours": 192,
  "results": [
    {
      "path": "examples/memory/cosmos",
      "type": "azure-serverless",
      "category": "Agent Memory / Knowledge",
      "label": "Cosmos DB memory",
      "description": null,
      "status": "pass",
      "run_id": 1234567,
      "run_url": "https://github.com/yaalalabs/agent-kernel/actions/runs/1234567",
      "job_url": "https://github.com/yaalalabs/agent-kernel/actions/runs/1234567/job/98766",
      "completed_at": "2026-07-13T03:40:10Z"
    }
  ]
}
```

`history/<workflow>.jsonl` — per-tile events. **When a tile is published for a NEW run_id, its superseded entry is appended here** (per-item roll-over, exactly the "latest moves to history" rule). Republishing under the same run_id (a re-run) replaces the entry in place with no history event — history records one event per run, not per attempt. Idempotent on `(key, run_id)`; trimmed to the most recent 15 events per tile.

```jsonl
{"key":"Agent Memory / Knowledge|Cosmos DB memory","status":"fail","run_id":1234500,"run_url":"…","completed_at":"2026-07-06T03:12:00Z"}
```

`expected_cadence_hours` lets the frontend flag staleness per tile without hardcoding cadences (test: none; nightly: 48h; weekly: 192h).

## Documentation site changes

Page at `docs/src/pages/status.tsx` (route `/status`, title **"Integration Status"**), styled with the homepage's design system (badge pill, gradient glass titles, topGlow section breakers, frameworks-strip tile recipe, glass source cards). Navbar item "Status"; footer link "Integration Status". `js-yaml` is a docs dependency for parsing the config files in the browser.

Behavior:

- Fetches in parallel (`Promise.allSettled`, one failure never blanks the page):
  - `status/*.json` and `history/*.jsonl` from the `status-data` branch;
  - **the two config YAMLs from the source branch**, from which it builds the full tile catalog (mirroring `dashboard_config.py` defaults, plus the synthetic unit/script-test tiles).
- Tiles = catalog ∪ published data. Configured-but-never-published tiles render as **"no data"** (hollow gray dot, "no runs yet", linking to the workflow's runs page). Published tiles missing from the catalog (e.g. removed tests) still render with their data.
- Tile states: 🟢 pass · 🔴 fail · ⚪ skipped · ◯ no data · 🟠 stale badge (per-tile `completed_at` older than the workflow's cadence; the dot still shows the last known result).
- Each tile: status dot, label, "updated X ago" / "failing since ‹date›" / "no runs yet", a last-10-runs history strip built from the tile's history events, and a hover ↗ affordance; the tile links to the exact job when known.
- Header: overall summary chips (passing / failing / no data / skipped / stale) and three glass source cards (one per workflow: last update, commit, stale marker, linking to the workflow's runs).
- Mock data renders only with `?mock=1` (for styling work); real published data is never shadowed.

## Failures and re-runs

- **Tests failing never blocks publishing** — the publish step runs with `if: always()` inside the same job, so a failing test publishes its red tile immediately.
- **Re-running a failed test** republishes that tile with the corrected outcome under the same run_id (replace in place, no duplicate history event).
- **Cancelled runs**: `always()` steps still execute during cancellation where possible; a tile that never got to publish simply keeps its previous state, and per-tile timestamps keep the display honest.

## Edge cases and failure modes

- **Fork PRs / feature branches** — PR caller jobs grant read-only tokens and `publish_status: false`; the safe-to-test fork flow grants read-only explicitly; integration-workflow publish steps are gated to `develop`. Only develop-branch runs can write to `status-data`.
- **Concurrent publishes** — CAS push with retry; concurrent updates are never lost (covered by a unit test that races two publishers).
- **Test removed from config** — its tile keeps rendering while its published data remains in `status/<workflow>.json`; it stops being part of the catalog. Old history keys are ignored.
- **Test renamed/moved** — same as removed + added; history restarts under the new identity key.
- **`status-data` branch missing** — the first publish bootstraps it (orphan commit including a README); the page shows the full catalog as "no data" until then.
- **raw.githubusercontent.com caching** — ~5 min CDN cache; per-tile `completed_at` keeps "last updated" honest.

## Implementation notes

- `dashboard_config.py` — shared dashboard-block resolution (defaults, `hidden`, fan-out) used by the validator and publisher; mirrored in `status.tsx` for the catalog.
- `test_publish_integration_status.py` — unit tests for tile resolution, status merging, per-item history roll-over/idempotency/trimming, and an end-to-end publish against a local bare repo including a simulated concurrent-push race. Runs in the `script-tests` CI job.
- `.github/INTEGRATION_TESTS.md` and `DEVELOPER_GUIDE.md` document the `dashboard` block in the add-a-test flow.

## Resolved decisions

1. **Cron schedules stay disabled for now** — the dashboard consumes whatever completed runs exist (including manual dispatches); the staleness badge communicates age.
2. **Naming: "Integration Status"** at `/status`, navbar item "Status".
3. **History is per item** — publishing a new status for a tile rolls the superseded one into `history/<workflow>.jsonl` (15 events per tile), powering the history strip and "failing since".
4. **Publishing is distributed** — each test job publishes its own tiles on completion via CAS pushes, rather than a central end-of-run collector. Chosen for immediate updates and precise re-run semantics, accepting that test jobs carry a write-capable token (branch-gated steps, PR runs never publish).
5. **Full catalog rendering** — the page shows every configured test, with "no data" for never-published tiles.
