# Integration Status Dashboard

A public, auto-updating status page on the documentation site (kernel.yaala.ai) that shows red/green health for every integration Agent Kernel supports — agent frameworks, cloud deployment variants (serverless / containerized on AWS, Azure, GCP), memory backends, messaging integrations, guardrails, multimodal storage, and core library tests — derived from the latest GitHub Actions runs on the `develop` branch.

## Goals

1. **Public visibility** — anyone can see, at a glance, which integrations are currently passing or failing, and when that data was last refreshed.
2. **Zero-touch maintenance** — when a developer adds a test to `.github/test-config.yaml` or `.github/integration-test-config.yaml`, the new integration appears on the dashboard automatically. Categorization and labeling live in those same config files.
3. **Honest data** — every status is traceable to a real workflow run (each tile links to the run), and stale data is visually flagged rather than silently shown as current.
4. **History** — when a new status is published for an item, the previous latest status is moved into that item's history, so the page can show recent trends (e.g. "failing since", last-N-runs strip) rather than only a point-in-time snapshot.

## Data sources

All three pipelines already run matrix jobs generated from the config YAMLs by `.github/scripts/generate_test_matrix.py`:

| Workflow | Config source | Tells us | Cadence |
|---|---|---|---|
| `test.yaml` → `test-reusable.yaml` ("Test") | `.github/test-config.yaml` (`e2e` tier) | Framework adapters (OpenAI, CrewAI, LangGraph, ADK), CLI/API features (structured output, hooks, multimodal, MCP, A2A, knowledge base, guardrails), unit tests | Every push to `develop` |
| `integration-test.yaml` ("Nightly Integration Tests") | `.github/integration-test-config.yaml` (`nightly` tier) | Messaging integrations (Slack, Telegram, Messenger, WhatsApp, Instagram, Gmail), multimodal storage (DynamoDB, Redis) | Manual (`workflow_dispatch`) for now |
| `integration-test-weekly.yaml` ("Weekly Integration Tests") | `.github/integration-test-config.yaml` (`weekly` tier) | Cloud deployment variants (AWS/Azure/GCP × serverless/containerized), memory backends (Redis, DynamoDB, Cosmos, Firestore), framework-on-cloud combos | Manual (`workflow_dispatch`) for now |

The integration pipelines' cron schedules are intentionally commented out while the pipelines themselves are being stabilized — that is fine for the dashboard: the publisher runs on every completed run regardless of trigger, so manually dispatched runs populate the page, and the staleness badge (below) communicates data age honestly. Re-enabling the crons later requires no dashboard changes.

The repository is public, so both the GitHub Actions API and raw file contents are publicly readable.

## Architecture

```
.github/test-config.yaml ─────────────┐  (dashboard metadata lives here)
.github/integration-test-config.yaml ─┤
                                      ▼
   ┌───────────────────────────────────────────────────┐
   │ Each workflow gains a final "publish-status" job  │
   │ (if: always(), develop only):                     │
   │  1. reads its own run's job/step conclusions via  │
   │     the Actions API (GITHUB_TOKEN)                │
   │  2. joins them with the config metadata           │
   │  3. rolls the previous status/<workflow>.json     │
   │     into history/<workflow>.jsonl                 │
   │  4. writes the new status/<workflow>.json         │
   │  5. pushes to the orphan `status-data` branch     │
   └───────────────────────────────────────────────────┘
                                      ▼
   https://raw.githubusercontent.com/yaalalabs/agent-kernel/status-data/…
                                      ▼
   ┌───────────────────────────────────────────────────┐
   │ Docusaurus page /status (docs/src/pages/status)   │
   │ fetches the JSON files client-side and renders    │
   │ category sections with green/red/gray/amber tiles │
   │ plus a per-tile recent-history strip              │
   └───────────────────────────────────────────────────┘
```

### Why a published-JSON branch instead of alternatives

- **Client-side GitHub Actions API parsing (no workflow changes)** — rejected. Unauthenticated API calls are limited to 60/hr per viewer IP, job-name string parsing (`run-tests (0, api, examples/api/slack, deploy)`) is brittle, and job conclusions are wrong in at least one known case: the weekly `deploy-openai` job runs its test step with `continue-on-error`, so the job can be green while the test failed. Step-level inspection is needed, which the publisher job does once, server-side.
- **Build-time data baked into the docs site** — rejected. `deploy-docs.yml` only runs when `docs/**` changes, so statuses would go stale immediately.
- **Committing JSON into `develop`** — rejected. Pollutes history and triggers CI loops.

The `status-data` branch holds only the status and history files, is force-pushed (single-commit git history, no growth — file contents including `history/*.jsonl` are carried forward across pushes), and `raw.githubusercontent.com` serves it with CORS enabled and no meaningful rate limits.

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
        label: Slack
        description: Slack events + Web API round-trip   # optional, tile tooltip
    - type: api
      path: examples/api/multimodal/dynamodb
      requires_aws: true
      dashboard:
        category: Multimodal Storage
        label: DynamoDB attachment store

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

- **`dashboard`** — a single mapping, a list of mappings, or the literal `hidden`. Each mapping produces one tile; all tiles from the same test share the same underlying status and link to the same job. This lets a single run vouch for several axes (e.g. a memory backend *and* the cloud variant it runs on) without duplicating test executions.
- **`category`** (string) — the section the tile renders under. Free-form; new categories appear on the page automatically, no website change needed.
- **`label`** (string) — tile display name. Must be unique within its category (validator-enforced) so tiles are unambiguous. The pair `(category, label)` is the tile's **identity key** — it is also what history entries are matched on, so renaming either starts a fresh history for that tile.
- **Defaults when `dashboard` is omitted** — the entry still appears as a single tile (goal 2 must not depend on developers remembering the block):
  - `label`: derived from the path (last two segments, e.g. `examples/api/multimodal/redis` → `multimodal / redis`).
  - `category`: derived from `type` via a small default map, e.g. `cli`/`api`/`memory`/`containerized` → "Core & Examples", `aws-serverless` → "AWS Serverless", `azure-containerized` → "Azure Containerized", etc.
- **`dashboard: hidden`** — opt-out for entries that shouldn't be public-facing.
- The `deployment_base` entry (`examples/aws-serverless/openai`) also gets a `dashboard` block so the base AWS serverless deployment shows up (see step-level handling below).

Initial implementation adds explicit `dashboard` blocks to every existing entry, with categories along the lines of:

- **Core & Frameworks** — unit tests, `cli/openai*`, `cli/adk`, `cli/crewai`, `cli/langgraph`, `cli/multi`
- **API Features** — structured output, hooks, A2A, MCP
- **Multimodal** — `api/multimodal/*` (per-framework and per-store)
- **Guardrails** — `cli/guardrail/*`
- **Messaging Integrations** — Slack, Telegram, Messenger, WhatsApp, Instagram, Gmail
- **Agent Memory / Knowledge** — key-value cache, Redis memory, DynamoDB memory, Cosmos memory, Firestore memory (via `gcp-serverless/openai-firestore`), knowledge base (`cli/knowledgebase/openai/chromadb`)
- **AWS Serverless / AWS Containerized / Azure Serverless / Azure Containerized / GCP Serverless / GCP Containerized** — the weekly deployment matrix

Multi-category mapping is used deliberately here: the memory tests under `examples/memory/*` and `gcp-serverless/openai-firestore` each carry two dashboard entries — one under **Agent Memory / Knowledge** and one under their cloud-variant category — so both axes stay populated from a single test run.

`validate_integration_config.py` is extended to validate the block for both config files — accepted shapes (mapping, list of mappings, `hidden` literal), string types, non-empty `category`/`label`, no duplicate `category` within one test's list, and unique `label` per category across the whole file set. The same validation step is added to the e2e `setup` job, which currently doesn't validate `.github/test-config.yaml` at all.

## Status publisher

New script `.github/scripts/publish_integration_status.py`:

1. **Inputs**: workflow key (`test` | `integration-test` | `integration-test-weekly`), current `GITHUB_RUN_ID`, `GITHUB_TOKEN`.
2. Calls `GET /repos/{repo}/actions/runs/{run_id}/jobs?per_page=100` (paginated) — response includes per-step conclusions.
3. For each config entry of the relevant tier, finds its matrix job by matching the entry `path` in the job name, resolves one status, then **fans out one result per `dashboard` entry** (a test with N dashboard mappings emits N results sharing the same `status`, `path`, and `job_url`). Status resolution:
   - job `success` → `pass`
   - job `failure` → `fail`
   - job `cancelled`/`skipped` → `skipped`
   - entry present in config but no matching job (e.g. added after this run started) → `unknown`
   - **special case** — weekly/nightly `deploy-openai` base job: read the named test step's conclusion (it is `continue-on-error`), not the job conclusion.
   - For `test.yaml`, also emit synthetic entries for `unit-tests` and `script-tests` jobs under "Core & Frameworks".
4. **Rolls the superseded snapshot into history** (see next section), then writes the new `status/<workflow-key>.json`:

```json
{
  "workflow": "integration-test-weekly",
  "workflow_name": "Weekly Integration Tests",
  "run_id": 1234567,
  "run_url": "https://github.com/yaalalabs/agent-kernel/actions/runs/1234567",
  "branch": "develop",
  "commit": "d728a3e9",
  "completed_at": "2026-07-10T03:41:22Z",
  "expected_cadence_hours": 192,
  "results": [
    {
      "path": "examples/aws-containerized/adk",
      "type": "aws-containerized",
      "category": "AWS Containerized",
      "label": "Google ADK on ECS",
      "description": null,
      "status": "pass",
      "job_url": "https://github.com/yaalalabs/agent-kernel/actions/runs/1234567/job/98765"
    },
    {
      "path": "examples/memory/cosmos",
      "type": "azure-serverless",
      "category": "Agent Memory / Knowledge",
      "label": "Cosmos DB memory",
      "description": null,
      "status": "pass",
      "job_url": "https://github.com/yaalalabs/agent-kernel/actions/runs/1234567/job/98766"
    },
    {
      "path": "examples/memory/cosmos",
      "type": "azure-serverless",
      "category": "Azure Serverless",
      "label": "OpenAI + Cosmos memory",
      "description": null,
      "status": "pass",
      "job_url": "https://github.com/yaalalabs/agent-kernel/actions/runs/1234567/job/98766"
    }
  ]
}
```

`results` is tile-grained, not test-grained: the two `examples/memory/cosmos` entries above come from one job. The frontend therefore needs no join logic — it just groups `results` by `category`.

`expected_cadence_hours` lets the frontend flag staleness per workflow without hardcoding cadences in the website (test: no cadence / informational; nightly: 48h grace; weekly: 192h = 8 days grace). While the crons remain disabled these thresholds will often be exceeded — that is the intended, honest signal.

### History

Rule: **when a new status is published, the snapshot it replaces is appended to history.** Concretely, before overwriting `status/<workflow-key>.json` the publisher reads the existing file (if any) and appends one compact line to `history/<workflow-key>.jsonl`:

```jsonl
{"run_id":1234566,"run_url":"…","commit":"a1b2c3d","completed_at":"2026-07-03T03:40:10Z","results":{"Agent Memory / Knowledge|Cosmos DB memory":"pass","Azure Serverless|OpenAI + Cosmos memory":"pass","AWS Containerized|Google ADK on ECS":"fail"}}
```

- `results` in a history line is a map from the tile identity key `"<category>|<label>"` to its status — compact by design; per-tile job URLs are not retained in history (the `run_url` is).
- **Idempotency**: if the history file already contains a line with the same `run_id`, the publisher skips the append (re-runs of a publish job must not duplicate history).
- **Retention**: history is trimmed to the most recent **50 runs per workflow** on each publish, keeping files small on the CDN-served branch. That comfortably covers months of nightly/weekly data.
- Tiles removed from config simply stop appearing in new lines; their old keys remain in old lines and are ignored by the frontend (which joins history onto the current tile set by identity key).

## Documentation site changes

New page at `docs/src/pages/status.tsx` (route `https://kernel.yaala.ai/status`, page title **"Integration Status"**) plus a `status.module.css`, following the styling conventions of the existing custom pages (`features.tsx`, `use-cases.tsx`). It is a standalone page, so it is not affected by docs versioning.

Behavior:

- On mount, fetch the three `status/*.json` files and the three `history/*.jsonl` files from `https://raw.githubusercontent.com/yaalalabs/agent-kernel/status-data/…` in parallel (`Promise.allSettled` — one missing file must not blank the page; history is progressive enhancement and its absence only hides the history strip).
- Merge all `results`, group by `category`, render one section per category (categories sorted by a fixed preferred order, unknown categories appended alphabetically).
- Each tile: status dot + label, plus a **recent-history strip** — the last ~10 statuses for that tile (current + history lines, newest last) rendered as small colored squares, each square's tooltip showing date and linking to its run. Tooltip/expand shows description, example path, source workflow, and links to the exact job/run. When the current status is `fail`, use history to show "failing since ‹date of first consecutive fail›".
  - 🟢 `pass` — green
  - 🔴 `fail` — red
  - ⚪ `skipped` / `unknown` — gray
  - 🟠 stale — amber ring/badge when `now - completed_at > expected_cadence_hours` (status dot still shows last known result; badge says "stale — last run X days ago")
- Page header: overall summary ("42 passing · 2 failing · 1 stale") and per-source freshness lines: "Core tests: last run 3 h ago (commit d728a3e) · Messaging: 26 h ago · Cloud deployments: 5 d ago", each linking to its run.
- All timestamps rendered in the viewer's local timezone with relative time ("3 hours ago").
- Loading skeleton while fetching; if all fetches fail, a friendly error with a link to the Actions page.
- Navbar: add a "Status" item in `docusaurus.config.js` (and optionally a footer link). The page is intentionally public and indexable.

No new npm dependencies are required (plain `fetch` + React; YAML never reaches the browser since the publisher pre-joins config metadata into the JSON).

## Workflow changes

One job appended to each of the three workflows:

```yaml
  publish-status:
    needs: [<all test jobs>]
    if: always() && github.ref == 'refs/heads/develop'   # never on PRs / forks
    runs-on: ubuntu-latest
    permissions:
      contents: write      # push to status-data branch
      actions: read        # read this run's jobs
    concurrency:
      group: status-data-publish    # serialize pushes across all three workflows
    steps:
      - checkout (develop, for config files + script)
      - checkout/fetch status-data branch into a subdirectory
      - python .github/scripts/publish_integration_status.py --workflow <key>
        # reads previous status JSON, appends history, writes new status JSON
      - commit and push status-data
```

Notes:

- Workflow-level `permissions` in these files are currently `contents: read`; the write grant is scoped to this one job.
- For `test.yaml` the condition is `github.event_name == 'push'` on develop — PR runs never publish. `test-reusable.yaml` stays untouched; publishing is the caller's concern.
- The `status-data` branch is bootstrapped once as an orphan branch containing a README, `status/`, and `history/` directories. The publish step carries existing files forward, overwrites the status file, appends to the history file, and force-pushes with `--force-with-lease` inside the concurrency group, so the branch stays at a handful of commits while file contents persist.
- If a workflow is cancelled before `publish-status` runs, the previous JSON simply remains — the dashboard shows the older timestamp, which is correct behavior.

## Failures and re-runs

Publishing is deliberately a separate final job rather than a step inside each
matrix job, and it still reflects individual test failures and re-runs
correctly:

- **Tests failing does not stop publishing.** `publish-status` runs with
  `if: always()`, so it executes and publishes the red tiles even when every
  test job failed (that is the whole point of the dashboard).
- **Re-running a failed test republishes the corrected status.** GitHub re-runs
  a job's dependent jobs along with it, for both "Re-run failed jobs" and
  single-job re-runs. `publish-status` depends on the test jobs, so it re-runs
  after the re-run tests finish, and the publisher queries the jobs API with
  `filter=latest`, which returns each job's most recent attempt. A test that
  failed and was re-run green therefore publishes as `pass`. Since the run_id
  is unchanged, the history roll-over is skipped (idempotency) and the snapshot
  is replaced in place — history records one line per run, not per attempt.
- **Why not publish from inside each matrix job?** Two concrete costs.
  Permissions are static per job, so every matrix job would need
  `contents: write` on GITHUB_TOKEN — handing a push-capable token to jobs that
  execute example code and third-party dependencies, instead of confining it to
  the one job that only runs our publisher script. And N matrix jobs pushing to
  the same branch concurrently would need fetch-patch-retry loops (job-level
  `concurrency` groups would serialize whole test jobs, not just the publish
  step). The dependent-job re-run semantics above deliver the same outcome
  without either cost.

## Edge cases and failure modes

- **Fork PRs / feature branches** — publisher never runs; dashboard only ever reflects `develop`.
- **Partially failed run** — `fail-fast: false` is already set on all matrices, so one red tile doesn't hide the rest; `if: always()` ensures publishing happens even when tests fail (the whole point).
- **Test removed from config** — it disappears from the next published JSON, hence from the dashboard; stale keys in old history lines are ignored.
- **Test renamed/moved** — same as removed + added; history restarts under the new identity key.
- **Concurrent publishes** — serialized by the shared `concurrency` group.
- **Re-run of a publish job** — history append is idempotent on `run_id`.
- **raw.githubusercontent.com caching** — responses are cached ~5 minutes by their CDN; acceptable for this cadence. `completed_at` in the payload keeps "last updated" honest regardless.
- **Weekly base-deployment nuance** — handled by step-level status for `deploy-openai` (see publisher §3).

## Implementation plan

Phased so each PR is independently mergeable:

1. **Config + validation** — add `dashboard` blocks to both config YAMLs; extend `validate_integration_config.py`; wire validation of `test-config.yaml` into the e2e `setup` job. No behavior change to test runs.
2. **Publisher** — add `publish_integration_status.py` (status + history roll-over) + unit test (following `scripts/test_*.py` convention); bootstrap `status-data` orphan branch; append `publish-status` job to `test.yaml`, `integration-test.yaml`, and `integration-test-weekly.yaml`. Verify with a `workflow_dispatch` run of each — these manual runs are the data source while the pipelines are being stabilized.
3. **Dashboard page** — `docs/src/pages/status.tsx` + CSS + navbar entry, including the history strip and "failing since" logic. Develop against the real published JSON; include a mock-data fallback for local `npm start` when the branch doesn't exist yet.
4. **Docs & guides** — update `.github/INTEGRATION_TESTS.md` and `DEVELOPER_GUIDE.md`: "when adding a test, set the `dashboard` block"; add a short page on the docs site explaining how to read the dashboard, linked from the page footer.

## Resolved decisions

1. **Cron schedules stay disabled for now** — the integration pipelines are still being stabilized; the dashboard consumes whatever completed runs exist (including manual dispatches), and the staleness badge communicates age. Re-enabling crons later needs no dashboard change.
2. **Naming: "Integration Status"** at `/status`, navbar item "Status".
3. **History is in scope** — on each publish, the superseded latest snapshot for a workflow rolls into `history/<workflow>.jsonl` (per-tile statuses keyed by category+label, 50-run retention), powering the per-tile history strip and "failing since" display.
