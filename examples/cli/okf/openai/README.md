# Open Knowledge Format (OKF) on Agent Kernel

A self-contained example that implements the **Open Knowledge Format (OKF)** — an
open, vendor-neutral format for markdown knowledge bundles that agents navigate
like a file system — on top of Agent Kernel and the OpenAI Agents SDK, with a
pluggable storage layer (local filesystem or S3) and three role agents.

> This is an **exploration example**, not a library feature. It makes **no
> changes to the `agentkernel` library** — no new config sections, extras, or
> factories. Everything lives in this project's `okf/` package.

## What is OKF?

OKF formalizes the "LLM-wiki" pattern: a directory tree of markdown **concept**
documents an agent can browse, search, and edit with a small tool surface — no
vector store or retrieval pipeline required. It's "just markdown, just files,
just YAML frontmatter."

- A **bundle** is a directory tree of markdown files, each a **concept** (a
  table, dataset, metric, playbook, …).
- **File path = concept identity** — there is no separate ID field.
- Every document has a **metadata block** (YAML frontmatter — only `type` is
  required; `title`, `description`, `resource`, `tags`, `timestamp` are standard
  optional fields) and **document details** (free markdown body).
- **Relationships** are ordinary markdown links between documents
  (`[orders](/sales/tables/orders.md)`).
- Two reserved filenames are used throughout:
  - **`index.md`** per directory — a listing of that directory's concepts, so
    agents can navigate the hierarchy incrementally (*progressive disclosure*).
  - **`log.md`** at the bundle root — a date-sectioned history of changes.

See the [OKF v0.1 announcement](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)
for the format's background.

## What this example demonstrates

| Piece | Where | Notes |
| --- | --- | --- |
| Blob storage abstraction | `okf/storage.py` | `OKFStorage` ABC; `FileSystemStorage` (local) and `S3Storage` (boto3). |
| Read-through knowledge cache | `okf/cache.py` | In-memory KV cache fronting the **bundle** storage. |
| Document format + link resolution | `okf/format.py` | Frontmatter parse/serialize; absolute + relative links. |
| Write guardrails | `okf/validation.py` | OKF v0.1 conformance validation in the write path. |
| Shared bundle object | `okf/bundle.py` | Wires bundle storage + source storage + cache + validation. |
| Agent-facing tools | `okf/tools.py` | `list/read/search/get_related`, `write_concept`, `append_log`, source tools. |
| Source→bundle sync | `okf/sync.py` | On-demand, timestamp-based idempotent sync into `synced/`. |
| Agents + CLI | `demo.py` | Consumer / Producer / Curator. |
| Sample bundle | `sample_bundle/` | A small committed OKF tree used for offline runs and tests. |
| S3 provisioning | `deploy/` | Terraform: two buckets + read-write / read-only IAM policies. |

### The three agents (the tool subset **is** the permission model)

All three agents share **one** `OKFBundle` and differ only in system prompt and
which subset of tools each is bound:

- **consumer** (read-only) — `list_concept`, `read_concept`, `search_concept`,
  `get_related`. Q&A over the bundle.
- **producer** (read + write) — consumer tools + `write_concept`, `append_log`.
  Applies user-requested updates. It never touches the source.
- **curator** (read + write + read-only source) — producer tools +
  `sync_source`, `list_source_files`, `read_source_file`. Runs the sync.

Only the Curator can reach the source, and only to **read** it — no write/delete
tool over the source is ever defined. `write_concept` regenerates the affected
`index.md` automatically, so keeping navigation correct is never left to a prompt.

## Prerequisites

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- `OPENAI_API_KEY` exported in your shell (for the interactive demo / smoke test)
- **Only for the S3 path:** AWS credentials + Terraform

```bash
export OPENAI_API_KEY="your-key-here"
```

## Setup

```bash
./build.sh          # install from the published agentkernel package
./build.sh local    # or: use the local ../../../../ak-py build
```

## Run locally (no AWS — the default)

The demo defaults to `FileSystemStorage`: the bundle is `./sample_bundle/` and
the sync source is `./sample_source/`. Choosing the backend is the **only**
difference between running locally and against S3.

```bash
python demo.py
```

You start on the **consumer** agent. Switch agents with `!select <name>` and see
them with `!list`.

### Flow 1 — sync (curator)

```
(consumer) >> !select curator
(curator)  >> Sync the source folder into the bundle.
```

The Curator reads every markdown file under `sample_source/`, transforms each
into an OKF document under `synced/` (mirroring the source layout), regenerates
the touched `index.md` files, and appends a per-run summary to `log.md`. Re-run
it — files whose source last-modified time is unchanged since the last sync are
skipped.

### Flow 2 — ask (consumer)

```
(curator)  >> !select consumer
(consumer) >> What is the grain of the orders table?
(consumer) >> Which table is Monthly Revenue derived from?
```

The Consumer starts at the root `index.md`, walks the tree, and cites a
document's `resource` link where relevant.

### Flow 3 — update (producer)

```
(consumer) >> !select producer
(producer) >> Add a "returns" table under sales/tables describing one row per return.
```

The Producer reads the current state, writes a validated OKF document (a
rejected write comes back with a reason to revise), and appends a log entry. The
directory's `index.md` is regenerated for you.

> **Heads up:** the producer and curator flows write into `./sample_bundle/`.
> Reset the committed sample with `git checkout sample_bundle && git clean -fd sample_bundle`.

### Seeding your own source folder

Point the demo at your own directories (the source is read through the same
`OKFStorage` interface, read-only):

```bash
OKF_BUNDLE_DIR=./my_bundle OKF_SOURCE_DIR=./my_source python demo.py
```

Any `.md` files under the source folder are eligible for sync; plain markdown
(no frontmatter) is fine — the Curator derives `title`/`type`, stamps a
`timestamp`, and records the source's last-modified time as `source_timestamp`.

## Run against S3

`S3Storage` assumes the buckets already exist — the application never creates
them. Provision them (and the IAM policies) with the `deploy/` Terraform module.

### 1. Provision the buckets and policies

```bash
cd deploy
# Using local state? Delete the optional remote backend first:
#   rm backend.tf
./deploy.sh
```

On the first run `deploy.sh` copies `terraform.tfvars.example` to
`terraform.tfvars` and stops so you can edit the bucket names/region; re-run
`./deploy.sh` to `terraform init` + `apply`. Tear the buckets down with
`./deploy.sh destroy`.

`deploy/` creates **two buckets** and their access policies:

- the **bundle (OKF wiki) bucket** — the demo needs **read-write**
  (`s3:GetObject`, `s3:PutObject`, `s3:ListBucket`); and
- the **source bucket** — the demo needs **read-only** (`s3:GetObject`,
  `s3:ListBucket`, never `s3:PutObject` / `s3:DeleteObject`).

These two IAM policies are the AWS-boundary form of the read-write / read-only
split the tool subsets already enforce in-process. `deploy/` provisions buckets
and policies **only** — it creates no IAM users. Attach the emitted
`bundle_rw_policy_arn` and `source_ro_policy_arn` to whatever principal (user or
role) runs the demo, and supply that principal's credentials yourself.

### 2. Point the demo at S3

Use the Terraform outputs:

```bash
export OKF_BACKEND=s3
export AWS_REGION="$(terraform -chdir=deploy output -raw region)"
export OKF_BUNDLE_BUCKET="$(terraform -chdir=deploy output -raw bundle_bucket_name)"
export OKF_BUNDLE_PREFIX="$(terraform -chdir=deploy output -raw bundle_prefix)"
export OKF_SOURCE_BUCKET="$(terraform -chdir=deploy output -raw source_bucket_name)"
export OKF_SOURCE_PREFIX="$(terraform -chdir=deploy output -raw source_prefix)"
python demo.py
```

Seed the source bucket with markdown files (e.g. `aws s3 cp ... s3://<source>/<prefix>/`)
and run the curator sync as above. On S3 the object's `LastModified` time drives
sync freshness, exactly as file mtime does locally.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `OKF_BACKEND` | `filesystem` | `filesystem` or `s3`. |
| `OKF_BUNDLE_DIR` | `./sample_bundle` | Bundle dir (filesystem backend). |
| `OKF_SOURCE_DIR` | `./sample_source` | Source dir (filesystem backend). |
| `OKF_BUNDLE_BUCKET` / `OKF_BUNDLE_PREFIX` | — | Bundle bucket/prefix (s3 backend, read-write). |
| `OKF_SOURCE_BUCKET` / `OKF_SOURCE_PREFIX` | — | Source bucket/prefix (s3 backend, read-only). |
| `OKF_MODEL` | `gpt-4.1` | Model for all three agents. |
| `OKF_LOG_LEVEL` | `INFO` | Console log level for the `okf` package (`DEBUG` traces every storage/cache/tool call; `WARNING` shows only rejected writes). |

## Run the tests

```bash
uv run pytest              # offline unit tests (no AWS, no key) + smoke test
uv run pytest test_okf.py  # offline unit tests only
```

- `test_okf.py` — offline: format round-trip, write guardrails, the `index.md`
  invariant, cache behavior, storage `last_modified`, and timestamp-based sync
  idempotency (all on `FileSystemStorage`).
- `demo_test.py` — a live-model end-to-end test of the full three-role flow: the
  Curator syncs `sample_source/` into a freshly cleaned `sample_bundle/`, the
  Consumer answers a question grounded in the synced content, and the Producer
  writes a new concept that the Consumer then reads back. Skipped automatically
  when `OPENAI_API_KEY` is unset.

## Design notes and limitations

- **`index.md` is a regenerated, flat mechanical listing.** Every
  `write_concept` overwrites the touched directory's `index.md` (and its
  ancestors, so a new subtree is reachable from the root). Hand-authored
  ordering or prose in a directory index does not survive the next write there —
  navigation correctness is chosen over curation.
- **Sync is timestamp-based and idempotent.** Each synced document records the
  source file's last-modified time as a `source_timestamp` field; a re-sync skips
  any file whose source mtime is unchanged and rewrites the rest. The document's
  own `timestamp` is the write time, kept separate from the source mtime.
  Conflict policy is **source wins**, and sync only ever touches the `synced/`
  subtree.
- **Validation follows OKF v0.1 exactly** — only `type` is required; missing
  optional fields warn but don't reject.
- The knowledge cache is in-memory and process-local; search is a path-scoped
  case-insensitive substring match (no embeddings). See
  `docs/specs/499-OKF-exploration/design.md` for the full rationale and non-goals.
