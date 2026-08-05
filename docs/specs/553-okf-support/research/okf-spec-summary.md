# OKF v0.2 — spec summary (research notes for #553)

Status: summarized 2026-08-05 from the published spec and announcement. Not normative — the
spec at `GoogleCloudPlatform/knowledge-catalog/okf/SPEC.md` is the source of truth.

Sources:

- Spec (v0.2): https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf
  (raw: https://raw.githubusercontent.com/GoogleCloudPlatform/knowledge-catalog/main/okf/SPEC.md)
- Announcement: https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing

## What OKF is

The Open Knowledge Format is Google's open specification (introduced June 2026, currently v0.2)
for packaging curated knowledge as a **bundle**: a directory of markdown files, one **concept**
per file, cross-linked into a graph. Vendor-neutral — "just markdown, just files, just YAML
frontmatter"; no SDK, runtime, or account required. Bundles live in git, on disk, in object
storage, or as tarballs.

## Bundle structure

```
bundle-root/
  index.md (optional)         # directory listing; only file that may carry okf_version
  log.md (optional)           # dated change history, newest first
  <concept>.md
  <subdirectory>/
    index.md (optional)       # no frontmatter allowed outside bundle root
    <concept>.md
    ...
```

- **Concept identity**: file path within the bundle with the `.md` suffix removed —
  `tables/customers.md` has concept ID `tables/customers`.
- **Reserved filenames**: `index.md` and `log.md` are never concept documents.
- `index.md` bodies are markdown sections of `* [Title](url) - description` bullets
  (progressive disclosure). Consumers **may synthesize an index** when none exists.
- `log.md` is a flat list of ISO-8601-dated entries (`**Update**`, `**Creation**`,
  `**Deprecation**` opening words are conventional, not required).

## Frontmatter

- **Required (the only universally required field)**: `type` — non-empty string classifying
  the concept (e.g. `BigQuery Table`, `Metric`, `Playbook`, `Attested Computation`).
- **Recommended**: `title`, `description`, `resource` (URI of the underlying asset),
  `tags` (list).
- **Provenance family**: `sources` (list of `{resource, id, title, author, usage_count,
  last_modified}`), `usage_window` (`{from, to}`); per-claim attribution via markdown
  footnotes keyed to `sources[].id`.
- **Trust family**: `generated: {by: <actor>, at: <ISO 8601>}`; `verified`: list of
  `{by, at}` events (a bare mapping must be normalized to a single-element list).
  Trust tiers: no `verified` → unverified; only non-`human:` actors → machine-confirmed;
  any `human:<id>` actor → human-reviewed.
- **Lifecycle family**: `status`: `draft` | `stable` (default) | `deprecated`;
  `stale_after`: date — concept is stale when `today >= stale_after`.
- **Actor convention**: `<producer>/<version>` for agents/tools
  (e.g. `reference_agent/gemini-2.5-pro`), `human:<id>` for people, `process:<id>` for
  automated processes.
- **Extensions**: arbitrary additional keys are allowed; consumers SHOULD preserve unknown
  keys on round-trip (§4.1) and MUST NOT reject documents containing them.

## Cross-linking

- Normal markdown links; two forms:
  - **Bundle-absolute** (recommended, stable under moves): starts with `/`, resolved from
    bundle root — `[customers](/tables/customers.md)`.
  - **Relative**: standard markdown relative paths.
- Path-valued frontmatter fields (`resource`, `sources[].resource`, `computation`,
  `executor.resource`, `attester.resource`) accept absolute URLs, bundle-absolute paths,
  or relative paths.
- `references/` is a naming convention (not required) for mirroring external code/run
  instructions as first-class concepts.
- Consumers **must tolerate broken links** — a missing target does not make a bundle
  malformed.

## Attested Computation (§10)

A standalone concept type carrying a definition plus its sanctioned execution method:
`runtime` (required), `parameters` (typed holes), `computation` (external file, or inline
`# Computation` fence in the body), `executor` (`resource` + `receipt`), `attester`
(`resource` — deterministic consumer-side verification). Full runtime protocol, attester
ABI/sandboxing, and attestation caching are explicitly deferred to future spec revisions.

## Conformance (§11)

A bundle is conformant iff:

1. Every non-reserved `.md` file contains parseable YAML frontmatter.
2. Every frontmatter block contains a non-empty `type`.
3. Reserved filenames follow their specified structures when present.

Consumers **must**: normalize bare `verified` mappings to lists; not reject concepts for
missing optional fields; tolerate unknown `type` values and unknown frontmatter keys;
tolerate broken links and missing `index.md`.

Consumers **should**: derive trust tiers and staleness only from the specified fields;
surface (not silently drop) failing attestations.

## Versioning (§12)

Current version 0.2. Bundles may declare `okf_version: "0.2"` in bundle-root `index.md`
frontmatter only. Minor bumps are backward-compatible additions; major bumps are breaking.
Consumers should attempt best-effort consumption of unknown versions rather than refusing.

## Consumption model (from the announcement)

Agents consume bundles by progressive disclosure (root index → concept files → links) and
are expected to **write** as well as read — updating concepts, keeping cross-references
consistent, appending to `log.md` — while humans curate the bundle as code in git. Google
ships a reference producer (BigQuery enrichment agent), a consumer (static HTML graph
visualizer), and sample bundles (GA4 e-commerce, Stack Overflow, Bitcoin datasets).

## Takeaways for #553

1. "OKF support" requires a **semantic layer** (frontmatter parsing, concept IDs, index
   handling, conformance-validated writes) on top of blob storage — raw path read/write is
   not OKF support.
2. The KB record contract `{"text", "metadata"}` maps cleanly: body → `text`, parsed
   frontmatter → `metadata`.
3. Writes must keep the bundle conformant (frontmatter + non-empty `type`) and can stamp
   `generated` provenance using the actor convention.
4. The MUST-tolerate rules (broken links, unknown keys/types, missing index) are hard
   requirements on our consumer/writer; round-trip key preservation is a spec SHOULD
   (§4.1) — value-level preservation satisfies it.
5. Lifecycle (`status`, `stale_after`) and trust tiers are cheap, spec-sanctioned metadata
   to surface to the agent.
6. Attested Computation execution is deferred in the spec itself — safe to exclude from
   this change's scope.
