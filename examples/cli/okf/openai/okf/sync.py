"""Use case 1 — sync a source folder into the bundle's ``synced/`` subtree.

The Curator invokes this on demand. For each markdown file in the read-only
source folder it derives an OKF document and writes it under ``synced/``,
mirroring the source layout. ``write_concept`` (via :meth:`OKFTools.apply_write`)
regenerates each touched ``index.md`` automatically, and a single per-run
summary is appended to ``log.md``.

Freshness is **timestamp-based**: each synced document stores the source file's
last-modified time (from :meth:`OKFStorage.last_modified`) in a
``source_timestamp`` frontmatter field. On a re-run a source file is skipped when
its current last-modified time equals the ``source_timestamp`` recorded on the
bundle copy; any change to that time re-syncs the document. The document's own
``timestamp`` field is the wall-clock write time, kept separate from the source
mtime that drives freshness.

Conflict policy is **source wins**: a synced document later hand-edited via the
Producer is overwritten when the source's last-modified time moves. Sync only
ever touches the ``synced/`` subtree, so hand-authored documents elsewhere are
never affected.
"""

from __future__ import annotations

import posixpath
import re
from datetime import datetime, timezone
from typing import Any, Optional

from okf.bundle import OKFBundle
from okf.format import FrontmatterError, OKFDocument, OKFFormat
from okf.storage import NotFoundError
from okf.tools import OKFTools


class OKFSync:
    """Stateless source→bundle sync into the ``synced/`` subtree."""

    SYNCED_ROOT = "synced"
    _HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

    @staticmethod
    def _derive_title(metadata: dict[str, Any], body: str, source_path: str) -> str:
        """Derive a title from frontmatter, else the first heading, else the filename."""
        if metadata.get("title"):
            return str(metadata["title"])
        heading = OKFSync._HEADING_RE.search(body)
        if heading:
            return heading.group(1).strip()
        stem = posixpath.splitext(posixpath.basename(source_path))[0]
        return stem.replace("-", " ").replace("_", " ").strip().title()

    @staticmethod
    def _derive_document(source_content: str, source_path: str, source_mtime: Optional[datetime]) -> OKFDocument:
        """Transform raw source content into an OKF document (without a write ``timestamp``)."""
        try:
            parsed = OKFFormat.parse_document(source_content)
            metadata = dict(parsed.metadata)
            body = parsed.body
        except FrontmatterError:
            metadata = {}
            body = source_content

        derived: dict[str, Any] = {}
        derived["type"] = metadata.get("type") or "Document"
        derived["title"] = OKFSync._derive_title(metadata, body, source_path)
        # Preserve any standard optional fields the source author provided.
        for key in ("description", "resource", "tags"):
            if metadata.get(key):
                derived[key] = metadata[key]
        # Record the source's last-modified time; this is what drives freshness.
        if source_mtime is not None:
            derived["source_timestamp"] = source_mtime.isoformat()
        return OKFDocument(metadata=derived, body=body)

    @staticmethod
    def _unchanged(bundle: OKFBundle, target: str, source_mtime: Optional[datetime]) -> bool:
        """Return ``True`` if the bundle copy was synced from this exact source mtime."""
        if source_mtime is None:
            return False  # no timestamp to compare against — always re-sync
        try:
            existing = OKFFormat.parse_document(bundle.read(target))
        except (NotFoundError, FrontmatterError):
            return False
        return existing.metadata.get("source_timestamp") == source_mtime.isoformat()

    @staticmethod
    def sync_source(bundle: OKFBundle) -> str:
        """Run the source→bundle sync and return a human-readable summary."""
        if not bundle.has_source:
            return "Error: no sync source is configured for this bundle."

        source_files = sorted(p for p in bundle.list_source() if p.endswith(".md"))

        created: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for source_path in source_files:
            try:
                content = bundle.read_source(source_path)
            except NotFoundError:
                failed.append(f"{source_path} (source read failed)")
                continue

            source_mtime = bundle.source_last_modified(source_path)
            target = f"{OKFSync.SYNCED_ROOT}/{source_path}"

            if OKFSync._unchanged(bundle, target, source_mtime):
                skipped.append(target)
                continue

            already = bundle.exists(target)
            derived = OKFSync._derive_document(content, source_path, source_mtime)
            derived.metadata["timestamp"] = datetime.now(timezone.utc).astimezone().isoformat()
            result = OKFTools.apply_write(bundle, target, OKFFormat.serialize_document(derived))
            if not result.valid:
                failed.append(f"{target} ({result.reason})")
            elif already:
                updated.append(target)
            else:
                created.append(target)

        summary = (
            f"Sync complete: {len(created)} created, {len(updated)} updated, "
            f"{len(skipped)} skipped (unchanged), {len(failed)} failed."
        )
        OKFSync._append_summary_log(bundle, created, updated, skipped, failed, summary)
        details = [summary]
        for label, items in (("Created", created), ("Updated", updated), ("Failed", failed)):
            if items:
                details.append(f"{label}: " + ", ".join(items))
        return "\n".join(details)

    @staticmethod
    def _append_summary_log(
        bundle: OKFBundle,
        created: list[str],
        updated: list[str],
        skipped: list[str],
        failed: list[str],
        summary: str,
    ) -> None:
        """Append one per-run sync summary entry to ``log.md``."""
        parts = [f"**Sync**: {summary}"]
        if created:
            parts.append("created " + ", ".join(f"[{p}](/{p})" for p in created))
        if updated:
            parts.append("updated " + ", ".join(f"[{p}](/{p})" for p in updated))
        if failed:
            parts.append("failed " + ", ".join(failed))
        OKFTools.append_log(bundle, " — ".join(parts))
