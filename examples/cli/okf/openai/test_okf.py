"""Offline unit tests for the OKF example.

These run against :class:`FileSystemStorage` over a temp directory — no AWS and
no model key required.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from okf.bundle import OKFBundle
from okf.cache import KnowledgeCache
from okf.format import OKFFormat
from okf.storage import FileSystemStorage, NotFoundError
from okf.sync import OKFSync
from okf.tools import OKFTools

VALID_DOC = """---
type: BigQuery Table
title: Orders
description: One row per order.
resource: https://example.com/orders
tags: [sales, orders]
timestamp: 2026-05-28T09:00:00Z
---

# Orders

The orders table. See [Customers](/tables/customers.md).
"""


@pytest.fixture
def bundle(tmp_path):
    return OKFBundle(FileSystemStorage(str(tmp_path / "bundle")))


@pytest.fixture
def bundle_with_source(tmp_path):
    return OKFBundle(
        FileSystemStorage(str(tmp_path / "bundle")),
        FileSystemStorage(str(tmp_path / "source")),
    )


# --------------------------------------------------------------------------- #
# Format round-trip
# --------------------------------------------------------------------------- #


def test_format_round_trip():
    doc = OKFFormat.parse_document(VALID_DOC)
    assert doc.type == "BigQuery Table"
    assert doc.metadata["tags"] == ["sales", "orders"]
    assert "The orders table" in doc.body
    # Serialize then re-parse yields identical metadata + body.
    reparsed = OKFFormat.parse_document(OKFFormat.serialize_document(doc))
    assert reparsed.metadata == doc.metadata
    assert reparsed.body.strip() == doc.body.strip()


def test_write_then_read_preserves_content(bundle):
    msg = OKFTools.op_write_concept(bundle, "tables/orders.md", VALID_DOC)
    assert "Wrote" in msg
    read_back = bundle.read("tables/orders.md")
    assert read_back == VALID_DOC
    assert OKFFormat.parse_document(read_back).metadata["title"] == "Orders"


# --------------------------------------------------------------------------- #
# Write guardrails
# --------------------------------------------------------------------------- #


def test_reject_missing_frontmatter(bundle):
    result = bundle.validate("x.md", "# no frontmatter here")
    assert not result.valid
    assert "frontmatter" in result.reason.lower()


def test_reject_missing_type(bundle):
    result = bundle.validate("x.md", "---\ntitle: X\n---\n\nbody")
    assert not result.valid
    assert "type" in result.reason


def test_reject_out_of_bundle_absolute_link(bundle):
    content = "---\ntype: X\n---\n\n[bad](/../../secrets.md)"
    result = bundle.validate("x.md", content)
    assert not result.valid
    assert "outside the bundle" in result.reason


def test_reject_out_of_bundle_relative_link(bundle):
    content = "---\ntype: X\n---\n\n[bad](../../../etc/passwd.md)"
    result = bundle.validate("a/b/x.md", content)
    assert not result.valid
    assert "outside the bundle" in result.reason


def test_reject_non_md_link(bundle):
    content = "---\ntype: X\n---\n\n[bad](/tables/orders.txt)"
    result = bundle.validate("x.md", content)
    assert not result.valid
    assert ".md" in result.reason


def test_warn_missing_optional_fields(bundle):
    result = bundle.validate("x.md", "---\ntype: X\n---\n\nbody")
    assert result.valid
    assert any("title" in w for w in result.warnings)


def test_external_links_allowed(bundle):
    content = "---\ntype: X\n---\n\n[docs](https://example.com/page)"
    result = bundle.validate("x.md", content)
    assert result.valid


# --------------------------------------------------------------------------- #
# index.md invariant
# --------------------------------------------------------------------------- #


def test_write_regenerates_directory_index(bundle):
    OKFTools.op_write_concept(bundle, "tables/orders.md", VALID_DOC)
    index = bundle.read("tables/index.md")
    assert "/tables/orders.md" in index
    assert OKFFormat.parse_document(index).type == "Index"


def test_write_cascades_index_to_root(bundle):
    OKFTools.op_write_concept(bundle, "tables/orders.md", VALID_DOC)
    # The root index links down to the new subdirectory, so it is reachable.
    root_index = bundle.read("index.md")
    assert "/tables/index.md" in root_index


def test_list_concept_generates_listing_without_index(bundle):
    # Write straight to storage (bypassing write_concept) so no index exists.
    bundle.write("notes/a.md", "---\ntype: Note\ntitle: A\n---\n\nbody")
    listing = OKFTools.op_list_concept(bundle, "notes")
    assert "no index.md" in listing
    assert "/notes/a.md" in listing


# --------------------------------------------------------------------------- #
# Cache behavior
# --------------------------------------------------------------------------- #


def test_cache_second_read_skips_storage(tmp_path):
    storage = FileSystemStorage(str(tmp_path))
    storage.write("a.md", "hello")
    cache = KnowledgeCache(storage)
    assert cache.read("a.md") == "hello"
    assert cache.storage_reads == 1
    assert cache.read("a.md") == "hello"
    assert cache.storage_reads == 1  # served from cache, no second storage hit


def test_cache_write_refreshes_entry(tmp_path):
    storage = FileSystemStorage(str(tmp_path))
    cache = KnowledgeCache(storage)
    cache.write("a.md", "v1")
    assert cache.read("a.md") == "v1"
    cache.write("a.md", "v2")
    assert cache.read("a.md") == "v2"
    assert cache.storage_reads == 0  # every read served from the write-through cache


def test_cache_miss_raises(tmp_path):
    cache = KnowledgeCache(FileSystemStorage(str(tmp_path)))
    with pytest.raises(NotFoundError):
        cache.read("missing.md")


# --------------------------------------------------------------------------- #
# Storage last-modified (drives timestamp-based sync freshness)
# --------------------------------------------------------------------------- #


def test_filesystem_last_modified(tmp_path):
    storage = FileSystemStorage(str(tmp_path))
    assert storage.last_modified("missing.md") is None
    storage.write("a.md", "hi")
    os.utime(tmp_path / "a.md", (1_700_000_000, 1_700_000_000))
    assert storage.last_modified("a.md") == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_source_tools_are_read_only(bundle_with_source):
    bundle_with_source._source.write("tables/refunds.md", "# Refunds\n\nOne row per refund.\n")
    assert "tables/refunds.md" in OKFTools.op_list_source_files(bundle_with_source)
    assert "One row per refund" in OKFTools.op_read_source_file(bundle_with_source, "tables/refunds.md")
    # Reading the source never creates a bundle document, and no tool mutates the
    # source (only sync_source writes, and only into the bundle's synced/ subtree).
    assert bundle_with_source.list() == []
    names = set(OKFTools.build_tools(bundle_with_source))
    assert "write_source_file" not in names and "delete_source_file" not in names


def test_source_tools_error_without_source(bundle):
    assert "no sync source" in OKFTools.op_list_source_files(bundle)
    assert "no sync source" in OKFTools.op_read_source_file(bundle, "x.md")


# --------------------------------------------------------------------------- #
# Sync flow (deterministic, timestamp-based, into synced/)
# --------------------------------------------------------------------------- #


def _seed_source(bundle_with_source, tmp_path, mtime_epoch):
    """Seed two source files with a pinned last-modified time (reproducible freshness)."""
    bundle_with_source._source.write("tables/refunds.md", "# Refunds\n\nOne row per refund.\n")
    bundle_with_source._source.write("readme.md", "---\ntype: Doc\ntitle: Readme\n---\n\nhello")
    for rel in ("tables/refunds.md", "readme.md"):
        os.utime(tmp_path / "source" / rel, (mtime_epoch, mtime_epoch))


def test_sync_creates_docs_index_and_log(bundle_with_source, tmp_path):
    _seed_source(bundle_with_source, tmp_path, 1_700_000_000)
    summary = OKFSync.sync_source(bundle_with_source)
    assert "2 created" in summary

    # Documents land under synced/ mirroring the source layout, as valid OKF.
    doc = OKFFormat.parse_document(bundle_with_source.read("synced/tables/refunds.md"))
    assert doc.type == "Document"  # defaulted (source had no frontmatter)
    assert doc.title == "Refunds"  # derived from the first heading
    assert "timestamp" in doc.metadata  # wall-clock write time
    # The source's last-modified time is recorded for freshness comparison.
    assert doc.metadata["source_timestamp"] == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).isoformat()

    # index.md regenerated and log.md summarized.
    assert "/synced/tables/refunds.md" in bundle_with_source.read("synced/tables/index.md")
    assert "**Sync**" in bundle_with_source.read("log.md")


def test_resync_skips_when_source_mtime_unchanged(bundle_with_source, tmp_path):
    _seed_source(bundle_with_source, tmp_path, 1_700_000_000)
    OKFSync.sync_source(bundle_with_source)
    # A second run with no change to the source mtimes writes nothing new.
    summary = OKFSync.sync_source(bundle_with_source)
    assert "0 created" in summary
    assert "2 skipped (unchanged)" in summary


def test_resync_rewrites_when_source_mtime_moves(bundle_with_source, tmp_path):
    _seed_source(bundle_with_source, tmp_path, 1_700_000_000)
    OKFSync.sync_source(bundle_with_source)
    # Edit a source file and bump only its mtime → that file re-syncs; the other is skipped.
    bundle_with_source._source.write("readme.md", "---\ntype: Doc\ntitle: Readme\ntags: [new]\n---\n\nhello")
    os.utime(tmp_path / "source" / "readme.md", (1_700_000_100, 1_700_000_100))
    summary = OKFSync.sync_source(bundle_with_source)
    assert "1 updated" in summary
    assert "1 skipped (unchanged)" in summary
    synced = OKFFormat.parse_document(bundle_with_source.read("synced/readme.md"))
    assert synced.metadata["tags"] == ["new"]


def test_sync_only_touches_synced_subtree(bundle_with_source, tmp_path):
    # A hand-authored document outside synced/ is untouched by sync.
    OKFTools.op_write_concept(bundle_with_source, "handmade/note.md", "---\ntype: Note\ntitle: N\n---\n\nkeep me")
    _seed_source(bundle_with_source, tmp_path, 1_700_000_000)
    OKFSync.sync_source(bundle_with_source)
    assert "keep me" in bundle_with_source.read("handmade/note.md")
