"""``OKFBundle`` — the single shared object the agent tools close over.

An ``OKFBundle`` wires together the pieces every tool needs:

- the **bundle** storage, fronted by the in-memory :class:`KnowledgeCache`
  (all bundle reads/writes go through the cache);
- an optional **source** storage, read directly and uncached — only the Curator
  reaches it, and only through the read-only source tools; and
- document validation (delegated to :mod:`okf.validation`).

The tools in :mod:`okf.tools` and the sync flow in :mod:`okf.sync` operate over
one of these objects. The only thing that differs between the Consumer,
Producer, and Curator agents is *which subset* of tools each is bound — the tool
subset is the permission model, and the read-only source access is expressed by
the fact that no write/delete tool over the source is ever defined.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from okf.cache import KnowledgeCache
from okf.storage import OKFStorage
from okf.validation import ValidationResult, validate_document

logger = logging.getLogger(__name__)


class OKFBundle:
    """Bundle storage (cached) + optional source storage (uncached) + validation."""

    def __init__(self, bundle_storage: OKFStorage, source_storage: Optional[OKFStorage] = None) -> None:
        """:param bundle_storage: durable home of the OKF bundle (read-write).

        :param source_storage: optional read-only folder the Curator syncs from.
        """
        self.cache = KnowledgeCache(bundle_storage)
        self._source = source_storage
        logger.info(
            "OKFBundle ready (bundle=%s, source=%s)",
            type(bundle_storage).__name__,
            type(source_storage).__name__ if source_storage is not None else "none",
        )

    # -- bundle access (through the cache) --------------------------------

    def list_bundle(self, prefix: str = "") -> list[str]:
        """List bundle document paths under ``prefix`` (recursive)."""
        return self.cache.list(prefix)

    def read(self, path: str) -> str:
        """Read a bundle document (cache-first). Raises ``NotFoundError`` if absent."""
        return self.cache.read(path)

    def write(self, path: str, content: str) -> None:
        """Persist a bundle document and refresh the cache entry."""
        self.cache.write(path, content)

    def exists(self, path: str) -> bool:
        """Return ``True`` if a bundle document exists at ``path``."""
        return self.cache.exists(path)

    def validate(self, path: str, content: str) -> ValidationResult:
        """Validate ``content`` for ``path`` (links checked against the bundle)."""
        return validate_document(path, content, exists=self.exists)

    # -- source access (uncached, read-only) ------------------------------

    @property
    def has_source(self) -> bool:
        """Return ``True`` if this bundle was given a sync source."""
        return self._source is not None

    def list_source(self, prefix: str = "") -> list[str]:
        """List source document paths under ``prefix`` (uncached)."""
        if self._source is None:
            raise RuntimeError("no source storage configured for this bundle")
        return self._source.list(prefix)

    def read_source(self, path: str) -> str:
        """Read a source document (uncached). Raises ``NotFoundError`` if absent."""
        if self._source is None:
            raise RuntimeError("no source storage configured for this bundle")
        logger.debug("source read (uncached): %s", path)
        return self._source.read(path)

    def source_last_modified(self, path: str) -> Optional[datetime]:
        """Return the source document's last-modified time, or ``None`` if absent.

        Sync uses this to detect changed source files without reading content.
        """
        if self._source is None:
            raise RuntimeError("no source storage configured for this bundle")
        return self._source.last_modified(path)
