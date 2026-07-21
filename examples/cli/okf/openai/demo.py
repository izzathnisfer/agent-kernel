"""OKF (Open Knowledge Format) demo — OpenAI Agents SDK + Agent Kernel CLI.

Three role agents share a single :class:`OKFBundle`; they differ only in system
prompt and the subset of OKF tools each is bound — the tool subset is the
permission model:

- **consumer** — read-only Q&A over the bundle.
- **producer** — read + write; applies user-requested updates to the bundle.
- **curator** — producer tools + read-only source tools + ``sync_source``;
  runs the source→bundle sync on demand (timestamp-based, into ``synced/``).

Storage backend selection is the only difference between running locally and
against S3, chosen with the ``OKF_BACKEND`` environment variable:

- ``filesystem`` (default) — ``OKF_BUNDLE_DIR`` (default ``./sample_bundle``) and
  ``OKF_SOURCE_DIR`` (default ``./sample_source``).
- ``s3`` — ``OKF_BUNDLE_BUCKET`` / ``OKF_BUNDLE_PREFIX`` (read-write) and
  ``OKF_SOURCE_BUCKET`` / ``OKF_SOURCE_PREFIX`` (read-only), in ``AWS_REGION``.
  The buckets are provisioned by the ``deploy/`` Terraform module; ``S3Storage``
  assumes they already exist.

Note: running the producer or curator flows mutates the bundle directory. When
using the committed ``sample_bundle/``, reset it with ``git checkout sample_bundle``.
"""

import logging
import os

from agentkernel.cli import CLI
from agentkernel.openai import OpenAIModule
from agents import Agent

from okf.bundle import OKFBundle
from okf.storage import FileSystemStorage, OKFStorage, S3Storage
from okf.tools import OKFTools

MODEL = os.getenv("OKF_MODEL", "gpt-4.1")


def _configure_logging() -> None:
    """Route the ``okf`` package logs to the console at ``OKF_LOG_LEVEL``.

    Defaults to ``INFO``; set ``OKF_LOG_LEVEL=DEBUG`` to trace every storage,
    cache, and tool call, or ``WARNING`` to see only rejected writes. Only the
    ``okf`` logger namespace is configured, so it does not touch the OpenAI
    Agents SDK or Agent Kernel loggers.
    """
    level = getattr(logging, os.getenv("OKF_LOG_LEVEL", "INFO").upper(), logging.INFO)
    okf_logger = logging.getLogger("okf")
    okf_logger.setLevel(level)
    if not okf_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        okf_logger.addHandler(handler)
    okf_logger.propagate = False


def _build_bundle() -> OKFBundle:
    """Construct the shared bundle from environment configuration."""
    backend = os.getenv("OKF_BACKEND", "filesystem").lower()
    if backend == "s3":
        region = os.getenv("AWS_REGION")
        bundle_storage: OKFStorage = S3Storage(
            bucket=os.environ["OKF_BUNDLE_BUCKET"],
            prefix=os.getenv("OKF_BUNDLE_PREFIX", ""),
            region=region,
        )
        source_storage: OKFStorage = S3Storage(
            bucket=os.environ["OKF_SOURCE_BUCKET"],
            prefix=os.getenv("OKF_SOURCE_PREFIX", ""),
            region=region,
        )
    else:
        bundle_storage = FileSystemStorage(os.getenv("OKF_BUNDLE_DIR", "./sample_bundle"))
        source_storage = FileSystemStorage(os.getenv("OKF_SOURCE_DIR", "./sample_source"))
    return OKFBundle(bundle_storage, source_storage)


_configure_logging()
bundle = _build_bundle()

consumer = Agent(
    name="consumer",
    model=MODEL,
    handoff_description="Read-only assistant that answers questions from the OKF bundle.",
    instructions=(
        "You answer questions using an Open Knowledge Format (OKF) bundle: a tree of markdown "
        "concept documents you navigate like a file system. You are READ-ONLY.\n\n"
        'Always begin discovery at the root by calling list_concept(""), then walk into '
        "subdirectories with list_concept and open documents with read_concept. Use "
        "search_concept to find concepts by keyword and get_related to follow links between "
        "documents. When a document has a 'resource' field, cite that link in your answer. "
        "Ground every answer in what the bundle actually contains; if it isn't there, say so."
    ),
    tools=OKFTools.select_tools(bundle, OKFTools.CONSUMER_TOOLS),
)

producer = Agent(
    name="producer",
    model=MODEL,
    handoff_description="Author that creates and updates concept documents in the OKF bundle.",
    instructions=(
        "You maintain an Open Knowledge Format (OKF) bundle of markdown concept documents. "
        "You can read AND write the bundle.\n\n"
        "Workflow for every change:\n"
        "1. Validate by reading first: use list_concept / read_concept to inspect the current "
        "state before writing.\n"
        "2. Write with write_concept(path, content). Content must be a full OKF document: YAML "
        "frontmatter with at least a 'type' field (always also fill title, description, and an "
        "ISO-8601 timestamp), followed by a markdown body. Use absolute-from-root links like "
        "/sales/tables/orders.md between documents.\n"
        "3. After every successful write, call append_log with a short description of the change.\n"
        "You do NOT need to update index.md — write_concept regenerates it automatically. "
        "If a write is rejected, read the reason, fix the document, and retry."
    ),
    tools=OKFTools.select_tools(bundle, OKFTools.PRODUCER_TOOLS),
)

curator = Agent(
    name="curator",
    model=MODEL,
    handoff_description="Manages the bundle and syncs markdown from the read-only source folder.",
    instructions=(
        "You are the curator of an Open Knowledge Format (OKF) bundle. You have the producer's "
        "read/write tools plus READ-ONLY access to a source folder.\n\n"
        "When the user asks to sync (e.g. 'sync the source folder'), call sync_source(): it reads "
        "every markdown file from the source, transforms each into an OKF document under the "
        "synced/ subtree, and writes new or changed documents. Freshness is timestamp-based — a "
        "file whose source last-modified time is unchanged since the previous sync is skipped — and "
        "it logs a per-run summary. Use list_source_files / read_source_file to inspect the source "
        "before or after a sync. You can never write to the source — only read it. For direct "
        "bundle edits, follow the same write_concept + append_log workflow as the producer."
    ),
    tools=OKFTools.select_tools(bundle, OKFTools.CURATOR_TOOLS),
)

OpenAIModule([consumer, producer, curator])

if __name__ == "__main__":
    CLI.main()
