# 499-OKF-exploration: Open Knowledge Format (OKF) example on Agent Kernel

A self-contained example that implements the Open Knowledge Format (OKF) — an open, vendor-neutral
specification for markdown knowledge bundles that agents navigate like a file system — on top of
Agent Kernel, with an abstract storage layer (S3 first) and three agents (Consumer, Producer,
Curator). The example use case: sync markdown files from a given S3 source folder into an OKF
bundle, then ask questions against the bundle and update it through agents.

Sources:

- OKF concept/spec: [How the Open Knowledge Format can improve data sharing](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)
  (Google Cloud, OKF v0.1)
- Architecture sketch: `docs/specs/499-OKF-exploration/Open Knowledge Format.drawio.xml`
  (pages: Concept, Design, Consumer Flow, Producer Flow, Curator Flow)

## Motivation

- OKF formalizes the "LLM-wiki" pattern: a directory tree of markdown **concept** documents an
  agent can browse, search, and edit with a small tool surface — no vector store or retrieval
  pipeline required. It is "just markdown, just files, just YAML frontmatter" and explicitly
  framework-neutral, which makes it a natural fit to demonstrate on Agent Kernel.
- OKF's three design principles constrain this design: **minimally opinionated** (only the
  `type` frontmatter field is required), **producer/consumer independence** (authoring and
  consumption are cleanly separated — mirrored here by the agent/tool split), and
  **format, not platform** (no proprietary SDK; the bundle stays portable plain files on S3).
- Agent Kernel's existing `KnowledgeBase` ABC (`ak-py/src/agentkernel/knowledgebase/base.py:7`)
  is query-shaped (`read(query, limit)`, `write(records)`); OKF's tool surface is path- and
  navigation-shaped (`list_concept(path)`, `get_related(path)`, `append_log(...)`) and does not
  map onto it — hence an exploration as an example first, not a new library backend.
- Examples in this repo are self-contained `uv` projects whose top-level directory is the
  **interface/target** (`cli`, `api`, `aws-serverless`, …), not the runtime; a framework example
  is `examples/<interface>/<runtime>/` (e.g. `examples/cli/openai/`), and concept examples nest
  a runtime beneath the concept (e.g. `examples/cli/knowledgebase/openai/<backend>/`). Each is a
  self-contained project with `demo.py`, `demo_test.py`, `README.md`, `pyproject.toml`, `build.sh`,
  `uv.lock`, depending only on published `agentkernel[...]` extras — this example follows that shape.

## The OKF format (per the OKF v0.1 spec and the Concept page)

- A **bundle** is a directory tree of markdown files, each representing a **concept** — anything
  worth capturing (tables, datasets, metrics, playbooks, runbooks, APIs), e.g.
  `sales/{index.md, log.md, datasets/, tables/, metrics/}`.
- **File path = concept identity** — there is no separate ID field.
- Every concept document has two parts:
  - **Metadata block**: YAML frontmatter. Only **`type`** is required (e.g. `BigQuery Table`);
    standard optional fields are `title`, `description`, `resource` (URL to the real underlying
    asset), `tags` (list), `timestamp` (ISO-8601 last-modified).
  - **Document details**: free markdown body — the spec is minimally opinionated; producers
    define their own sections (schema tables, join paths, usage notes, etc.).
- **Relationships** are ordinary markdown links between concept documents
  (e.g. `[orders](/tables/orders.md)`), forming a graph richer than the directory hierarchy.
- **Reserved filenames** (optional but recommended, and used throughout this example):
  - **`index.md`** per directory: a curated listing of that directory's concepts — enables
    **progressive disclosure**, letting agents navigate the hierarchy incrementally.
  - **`log.md`** at the bundle root: a chronological, date-sectioned history of changes
    (`## 2026-05-28` → `* **Update**: ...` entries linking to touched documents).

## Requirements

### Example package

- Lives at `examples/cli/okf/openai/` (concept → runtime, mirroring the
  `examples/cli/knowledgebase/openai/<backend>/` precedent and leaving room for other runtimes),
  following the existing CLI example layout: `demo.py`, `demo_test.py`, `okf/` (the OKF
  implementation package), `README.md`, `pyproject.toml`, `build.sh`, `uv.lock`.
- Depends on `agentkernel[cli,openai]` plus `boto3` and `pyyaml`; **no changes to the ak-py
  library** (no new config sections, extras, factories, or exports).
- README documents: the OKF format, required AWS credentials/bucket setup, how to seed the
  source folder, and a scripted walkthrough of the three flows (sync → ask → update).

### Storage abstraction

- `OKFStorage` ABC with a minimal blob-store surface:
  - `read(path) -> str` — raises a not-found error for missing paths
  - `write(path, content) -> None`
  - `list(prefix) -> list[str]` — recursive listing of document paths under a prefix
  - `exists(path) -> bool`
- Paths are bundle-relative POSIX paths (`tables/orders.md`); the storage maps them to its
  backend addressing.
- Storage classes take **explicit constructor parameters** (bucket, prefix, region) — they never
  read global config (mirrors the shared-driver rule in core).
- Two implementations in the example:
  - `S3Storage` (boto3) — the primary backend; bundle root = `s3://<bucket>/<prefix>/`
  - `FileSystemStorage` — local directory; used by `demo_test.py` and offline runs
- The **sync source folder** is read through the same `OKFStorage` interface (a second instance
  pointed at the source bucket/prefix) — no separate source-reader abstraction.

### Knowledge cache

- In-memory KV cache (`dict[path, content]`) in front of storage, per the Design page:
  - reads are read-through: hit → return cached; miss → fetch from storage, store, return
  - writes update/invalidate the cached entry for that path
- Process-local and unbounded for the example; no TTL, no cross-process invalidation. Bundles are
  assumed small enough to hold in memory, so an unbounded cache is acceptable for the exploration.

### Agent-facing tools

- Plain Python functions over one shared `OKFBundle` object (storage + cache + validation),
  bound per agent via `OpenAIToolBuilder.bind([...])`.
- **Read tools**:
  - `list_concept(path)` — returns the directory's `index.md` content; when the directory has
    no `index.md` (optional in a minimally-opinionated bundle), returns a generated listing of
    the document paths directly under that directory so navigation still works
  - `read_concept(path)` — returns document details + parsed metadata
  - `search_concept(path, keyword)` — keyword search scoped by `path`:
    - `path` is a file: search only that document (frontmatter + body)
    - `path` is a directory: recursively search every document under it
    - substring match (case-insensitive) against raw document text, no embeddings/ranking;
      returns matching documents' paths + the matching line(s) for context
  - `get_related(path)` — parses markdown links in the document; returns linked bundle paths
- **Link resolution** (shared by `get_related` and the write guardrail's link check):
  - **absolute** links (`/tables/orders.md`) resolve from the **bundle root**
  - **relative** links (`../orders.md`) resolve relative to the current document's directory
  - both normalize to a bundle-relative POSIX path; authored and synced documents use the
    **absolute-from-root** form as the canonical style so authored and validated links agree
- **Write tools**:
  - `write_concept(path, content)` — create or replace a document, gated by write guardrails
    (below); on success persists to storage and updates the cache
- **Special tools**:
  - `append_log(log_details)` — appends an entry under today's date section in `log.md`
- Tool errors (invalid path, missing document, failed validation) return descriptive error
  strings to the agent rather than raising — the agent can self-correct.

### Write guardrails

- Implemented as **document parsing/validation inside the write path** (the Design page's
  "Document Parsing" guardrail); not wired into Agent Kernel's guardrail-provider system.
- Validation follows OKF v0.1 conformance — only `type` is mandatory. `write_concept` rejects,
  with `error + reason` returned to the agent (Producer Flow `[fail]` branch), when:
  - frontmatter is missing or not valid YAML
  - the `type` field is absent (the spec's only required field)
  - standard optional fields are present but malformed: `tags` not a list, `timestamp` not
    ISO-8601, `resource` not a URL
  - relative links in the body point outside the bundle or to non-`.md` targets
- Missing optional fields (`title`, `description`, `timestamp`) produce a warning in the tool
  response, not a rejection — the agents' prompts instruct them to always populate these, but
  the format itself stays minimally opinionated.
- Links to not-yet-existing bundle documents are allowed (warn, don't reject) — bundles are
  built incrementally.
- Content-safety guardrails ("Normal Guardrails (Optional)" in the diagram) are out of scope.

### Agents

- Three OpenAI Agents SDK agents loaded via `OpenAIModule`, matching the roles OKF itself
  implies (producer authors, consumer reads, curator manages content like code) and differing
  only in system prompt and tool subset — the tool split is the permission model:
  - **Consumer** (read-only): `list_concept`, `read_concept`, `search_concept`, `get_related`.
    Q&A over the bundle; prompt instructs it to start discovery at the root `index.md`.
  - **Producer** (read + write): Consumer tools + `write_concept`, `append_log`.
    Applies user-requested updates; prompt requires it to (1) validate-by-reading first,
    (2) update the affected directory's `index.md` when adding/renaming documents,
    (3) `append_log` after every successful write.
  - **Curator** (read + write + source access): Producer tools + `list_source_files()` /
    `read_source_file(path)`, thin wrappers over a **second `OKFStorage` instance** pointed at the
    source prefix (so "no separate source-reader abstraction" holds). Executes the sync flow on demand.
- Interface: interactive `CLI.main()` (as in `examples/cli/openai/demo.py`); the user switches
  agents with the CLI's agent selection.

### Use case 1 — sync source S3 folder into the bundle (Curator)

- Triggered on demand from the CLI (e.g. "sync the source folder"), not by a scheduler.
- Flow (Curator Flow page, without the scheduler):
  - list source `.md` files; for each, read content
  - transform into an OKF document: preserve/derive frontmatter (derive `title` from filename or
    first heading, set `timestamp`, default `type: Document` when absent)
  - `write_concept` into the bundle under a `synced/`-style target path mirroring the source layout
  - update the affected `index.md` files, `append_log` a per-run summary of created/updated docs
- Idempotency: compare the **source-derived body only**, excluding the volatile derived
  `timestamp`. A document is rewritten only when its source body differs from the bundle copy; on
  an unchanged body the existing bundle `timestamp` is **preserved** (not restamped). Unchanged
  files are skipped and the log entry says "skipped (unchanged)".

### Use case 2 — ask questions (Consumer)

- Consumer Flow page: user asks → agent walks `index.md` → `read_concept` / `search_concept` /
  `get_related` → answers with the document's `resource` link cited where relevant.
- Reads hit the knowledge cache first (hit/miss behavior per the flow diagram).

### Use case 3 — update the system (Producer)

- Producer Flow page: user requests a change → agent reads the current document →
  `write_concept` (validated) → cache updated → `append_log` → success reported;
  validation failure returns the reason and the agent revises.

### Testing

- **Unit tests (offline, no external deps)** — `FileSystemStorage` against a temp-directory
  bundle; no AWS and no model key:
  - format round-trip: write a valid document → read back identical content + parsed metadata
  - guardrails: missing frontmatter / missing required field / out-of-bundle link → rejected
    with reason
  - cache: second read served without a storage call; write refreshes the cached entry
  - sync: seed a source dir → sync → bundle contains OKF docs, `index.md` and `log.md` updated;
    re-sync with no source change writes nothing new (idempotency: unchanged bodies skipped,
    `timestamp` preserved)
- **Agent smoke test (requires a live model key)** — uses the `Test("demo.py")` harness (as in
  `examples/cli/openai/demo_test.py`), which starts the real agents: Consumer answers a question
  grounded in a seeded bundle. This is the one test that is not offline — it needs an OpenAI API
  key and is skipped when none is present (matching how the other CLI examples behave in CI).

## Component overview

```mermaid
graph LR
    subgraph Agents [OpenAI agents via OpenAIModule]
        C[Consumer<br/>read-only]
        P[Producer<br/>read + write]
        K[Curator<br/>read + write + source]
    end
    subgraph Tools [OKF tools]
        R[list / read / search / get_related]
        W[write_concept / append_log]
        G[Write guardrails<br/>document parsing]
    end
    KC[Knowledge cache<br/>in-memory KV]
    S[(OKFStorage ABC)]
    S3[(S3Storage<br/>bundle)]
    FS[(FileSystemStorage<br/>tests)]
    SRC[(S3 source folder<br/>via OKFStorage)]

    C --> R
    P --> R
    P --> W
    K --> R
    K --> W
    K --> SRC
    W --> G
    R --> KC
    W --> KC
    KC --> S
    S --> S3
    S --> FS
```

## Non-goals

- No changes to the ak-py library: no `AKConfig` section, no optional-dependency extra, no
  factory registration, no public exports.
- No SQL storage implementation (the Design page lists it as a possible backend; S3 + filesystem
  suffice for the exploration).
- No scheduler/cron infrastructure for the Curator (diagram shows it; the example triggers sync
  on demand).
- No vector/semantic search — `search_concept` is a path-scoped keyword (substring) search over
  raw document text, not ranked or embedding-based.
- No content-safety guardrail provider integration for writes.
- No distributed or persistent knowledge cache (in-memory per process only).
- No REST/MCP/A2A exposure — CLI only.

## Open questions

- Placement is decided: `examples/cli/okf/openai/` (concept → runtime, per the
  `examples/cli/knowledgebase/openai/<backend>/` precedent). Framework/interface defaults still to
  confirm: OpenAI Agents SDK and interactive CLI.
- Validation strictness: the design follows OKF v0.1 (only `type` required, missing
  `title`/`description`/`timestamp` warn). Should this bundle enforce a stricter house profile
  (reject on missing optional fields) instead?
- Sync conflict policy: when a synced document was later hand-edited via the Producer and the
  source file also changed, the proposal is **source wins** (bundle copy overwritten, logged).
  Acceptable, or should edited docs be skipped/flagged?
- Sync target layout: mirror the source folder structure under a dedicated subtree
  (e.g. `synced/...`) vs. writing into the bundle root — proposal is a dedicated subtree.
- Should the Curator also do the diagram's "reconcile / enrich" duties (link fixing, index
  regeneration) in this exploration, or is sync-only enough for the first cut?
- If the exploration succeeds, the follow-up decision is whether OKF is promoted into the
  library (as a `knowledgebase` sibling package with config/extras/tests) — out of scope here,
  but the storage ABC is shaped so that promotion doesn't require redesign.
