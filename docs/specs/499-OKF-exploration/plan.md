# 499-OKF-exploration: OKF example on Agent Kernel — Implementation Plan

Ordered breakdown of the `design.md` requirements into working, testable iterations. All work is
confined to a new self-contained `uv` project at `examples/cli/okf/openai/` (including its `deploy/`
Terraform module for S3 provisioning); **no ak-py library changes** (see design Non-goals). Each
iteration leaves the branch importable and its offline tests green. No separate `spec.md` was
produced — `design.md` carries the interface-level detail this plan orders, so section references
below point into `design.md`.

## Iteration 1: Project scaffold

- **Goal:** An empty-but-installable `uv` example mirroring `examples/cli/openai/` and the
  `examples/cli/knowledgebase/openai/<backend>/` layout.
- **Files:** `examples/cli/okf/openai/{pyproject.toml, build.sh, README.md (stub), okf/__init__.py, .gitignore}`.
- **Steps:**
  1. Copy `pyproject.toml` shape from `examples/cli/openai/pyproject.toml`; deps
     `agentkernel[cli,openai]` + `boto3` + `pyyaml`; dev group `agentkernel[test]`, black/isort/mypy
     (design → Example package).
  2. Copy `build.sh` from `examples/cli/openai/build.sh` unchanged.
  3. Create the `okf/` implementation package with an empty `__init__.py`.
- **Verify:** `bash build.sh` (or `uv sync --all-extras`) succeeds; `uv run python -c "import okf"`.

## Iteration 2: Storage abstraction + knowledge cache

- **Goal:** Blob-store backends and the read-through cache exist and are unit-tested offline.
- **Files:** `okf/storage.py`, `okf/cache.py`.
- **Steps:**
  1. `OKFStorage` ABC: `read`/`write`/`list`/`exists`, bundle-relative POSIX paths, no mtime surface
     (design → Storage abstraction).
  2. `FileSystemStorage` (local dir; test + no-AWS path) and `S3Storage` (boto3; explicit
     bucket/prefix/region constructor params, never global config).
  3. In-memory KV cache: read-through (hit/miss/fill), write updates/invalidates; fronts the **bundle**
     storage only (design → Knowledge cache).
- **Verify:** unit tests — filesystem round-trip; cache serves 2nd read without a storage call and
  refreshes on write.

## Iteration 3: Format layer + write guardrails + `OKFBundle`

- **Goal:** OKF document parse/serialize, link resolution, validation, and the single shared bundle
  object wiring storage + source + cache + validation.
- **Files:** `okf/format.py` (frontmatter + link resolution), `okf/validation.py`, `okf/bundle.py`.
- **Steps:**
  1. Frontmatter parse/serialize (YAML); `type`-only-required model with standard optional fields
     (design → The OKF format).
  2. Shared link resolution: absolute-from-root and relative, normalized to bundle-relative POSIX;
     absolute-from-root is canonical (design → Link resolution).
  3. Write guardrails as in-write-path validation: reject missing/invalid frontmatter, absent `type`,
     malformed optional fields, out-of-bundle / non-`.md` links; warn (don't reject) on missing
     optional fields and links to not-yet-existing docs (design → Write guardrails).
  4. `OKFBundle`: holds bundle `OKFStorage` (behind cache) + source `OKFStorage` (uncached) +
     validation (design → Agent-facing tools intro).
- **Verify:** unit tests — valid doc round-trip with parsed metadata; guardrail rejections (missing
  frontmatter, missing `type`, out-of-bundle absolute **and** relative links) each return a reason.

## Iteration 4: Agent-facing tools

- **Goal:** All tool functions over one `OKFBundle`, with the `index.md` invariant enforced in the
  write path.
- **Files:** `okf/tools.py`.
- **Steps:**
  1. Read tools: `list_concept` (index.md or generated listing when absent), `read_concept`,
     `search_concept` (path-scoped case-insensitive substring, default cap 20 docs + truncation note),
     `get_related` (design → Read tools).
  2. `write_concept`: guardrail-gated create/replace → persist → cache update → **regenerate the
     touched directory's `index.md`** as a flat mechanical listing (design → invariant note).
  3. `append_log`: append under today's date section in root `log.md`.
  4. Source tools `list_source_files()` / `read_source_file(path)` over the uncached source instance.
  5. All tools return descriptive error strings, never raise (design → tool errors).
- **Verify:** unit tests — `write_concept` of a new doc regenerates its directory `index.md` to include
  the entry (asserted directly); cache refresh on write.

## Iteration 5: Sync flow (Curator, Use case 1)

- **Goal:** On-demand, timestamp-based idempotent sync of the source folder into the `synced/` subtree.
- **Files:** `okf/sync.py` (a function/tool the Curator invokes); adds `OKFStorage.last_modified`.
- **Steps:**
  1. Add `last_modified(path)` to `OKFStorage` (file mtime / S3 `LastModified`), surfaced on the
     bundle as `source_last_modified`.
  2. List source `.md`; for each, transform to an OKF doc (derive `title`, default `type: Document`,
     record the source mtime as `source_timestamp`, stamp the write `timestamp`); `write_concept`
     under `synced/` mirroring source layout.
  3. Idempotency by comparing the current source mtime to the recorded `source_timestamp`; skip
     unchanged; conflict policy source-wins; `append_log` one per-run created/updated/skipped summary.
- **Verify:** unit test — seed a source dir with pinned mtimes → sync creates OKF docs + regenerated
  `index.md` + `log.md` summary; re-sync with unchanged mtimes writes nothing (idempotency); bumping
  one file's mtime re-syncs just that file. Plus a `last_modified` unit test.

## Iteration 6: Agents, CLI wiring, and `sample_source/`

- **Goal:** Runnable interactive demo with the three role agents and committed sample source markdown
  the Curator syncs into a (git-ignored, generated) bundle.
- **Files:** `examples/cli/okf/openai/demo.py`, `examples/cli/okf/openai/sample_source/` (a small set
  of plain-markdown documents).
- **Steps:**
  1. Build `OKFBundle` on `FileSystemStorage` pointed at `sample_bundle/` (the no-AWS default —
     generated by the sync, git-ignored); document the `S3Storage` swap as the only local↔cloud
     difference.
  2. Define Consumer / Producer / Curator `Agent`s differing only in prompt and the tool subset bound
     via `OpenAIToolBuilder.bind([...])` — the tool split is the permission model; only the Curator
     gets source tools, read-only (design → Agents).
  3. Register with `OpenAIModule([...])`; launch `CLI.main()` with agent selection (pattern from
     `examples/cli/openai/demo.py`).
  4. Author `sample_source/` as a small set of plain-markdown documents (the Curator derives OKF
     frontmatter on sync).
- **Verify:** `uv run python demo.py` locally: the Curator syncs `sample_source/` and the Consumer
  then answers a question grounded in the synced content.

## Iteration 7: Tests

- **Goal:** The offline suite plus the one live-model smoke test, matching the other CLI examples.
- **Files:** `examples/cli/okf/openai/demo_test.py` (agent smoke test) and the offline unit test
  module(s) accumulated in Iterations 2–5.
- **Steps:**
  1. Consolidate offline unit tests (format round-trip, guardrails, `index.md` invariant, cache,
     `last_modified`, timestamp-based sync idempotency) — all on `FileSystemStorage`, no AWS, no
     model key (design → Testing).
  2. `demo_test.py` using the `Test("demo.py")` harness (pattern from
     `examples/cli/openai/demo_test.py`): the full three-role flow — Curator syncs `sample_source/`
     into a freshly cleaned `sample_bundle/`, Consumer answers a question grounded in the synced
     content, Producer writes a concept the Consumer reads back; skipped when no OpenAI key is present.
- **Verify:** `uv run pytest` — offline tests pass with no key; smoke test runs/passes with a key set.

## Iteration 8: AWS provisioning (`deploy/` Terraform)

- **Goal:** The S3 run path's buckets and IAM policies are provisioned by Terraform, not left as
  manual prerequisites; the app code still assumes buckets exist.
- **Files:** `examples/cli/okf/openai/deploy/{main.tf, variables.tf, outputs.tf, backend.tf (optional)}`.
- **Steps:**
  1. `variables.tf`: `region`, bundle bucket name/prefix, source bucket name/prefix (design → Deployment).
  2. `main.tf`: create the **bundle (OKF wiki) bucket** with a **read-write** access policy
     (`s3:GetObject`/`s3:PutObject`/`s3:ListBucket`) and the **source bucket** with a **read-only**
     access policy (`s3:GetObject`/`s3:ListBucket`, never `Put`/`Delete`) — the AWS-boundary form of
     the tool-subset permission split (design → Deployment, Agents).
  3. `outputs.tf`: emit created bucket names/prefixes for the demo's `S3Storage` constructor params.
     Optional `backend.tf` for remote state, deletable for local state (as in `agent/deploy/`).
  4. Confirm `S3Storage` never provisions — buckets are assumed to exist (design → Deployment).
- **Verify:** `terraform -chdir=deploy init && terraform -chdir=deploy validate` (and `plan` against a
  real account) succeed; outputs feed the demo's `S3Storage` params.

## Iteration 9: README, docs, and skills sync

- **Goal:** Documentation matches the shipped example.
- **Files:** `examples/cli/okf/openai/README.md`; `docs/docs/examples/overview.md`.
- **Steps:**
  1. Write the README: OKF format primer, local-filesystem no-AWS run path, S3 setup via the
     `deploy/` Terraform (**read-write** bundle grant vs **read-only** source grant), source-seeding,
     and the three-flow walkthrough (sync → ask → update) (design → Example package, Deployment).
  2. Add the example to `docs/docs/examples/overview.md` alongside the other CLI examples.
  3. Verify no `.agents/skills/` dev-skill needs changes — this adds an example, not a library
     capability, so `ak-dev-*` skills should be unaffected; confirm via the
     `ak-dev-sync-docs-from-branch` / `ak-dev-sync-skills-from-branch` flows before merge.
- **Verify:** README run paths execute as written; docs example index lists the new example.
