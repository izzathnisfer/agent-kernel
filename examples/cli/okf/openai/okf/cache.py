"""In-memory knowledge cache in front of an :class:`OKFStorage`.

Per the Design page, reads are read-through and writes update the cached entry:

- ``read``  — hit → return cached; miss → fetch from storage, store, return
- ``write`` — persist to storage, then update the cached entry for that path
- ``exists`` — a cached path exists; otherwise defer to storage

The cache fronts the **bundle** storage only. The source storage is read
directly (uncached), since source reads happen once per sync.

It is process-local and unbounded: no TTL, no cross-process invalidation.
Bundles are assumed small enough to hold in memory, so an unbounded cache is
acceptable for this exploration.
"""

from __future__ import annotations

import logging

from okf.storage import NotFoundError, OKFStorage

logger = logging.getLogger(__name__)


class KnowledgeCache:
    """Read-through / write-through KV cache over one :class:`OKFStorage`."""

    def __init__(self, storage: OKFStorage) -> None:
        """:param storage: backing blob store the cache fronts."""
        self._storage = storage
        self._cache: dict[str, str] = {}
        # Instrumentation the offline tests assert against (a read served from
        # the cache must not touch storage).
        self.storage_reads = 0

    def read(self, path: str) -> str:
        """Return content for ``path``, serving from cache on a hit.

        :raises NotFoundError: if ``path`` is absent from cache and storage.
        """
        if path in self._cache:
            logger.debug("cache hit: %s", path)
            return self._cache[path]
        logger.debug("cache miss: %s (fetching from storage)", path)
        self.storage_reads += 1
        content = self._storage.read(path)  # raises NotFoundError on miss
        self._cache[path] = content
        return content

    def write(self, path: str, content: str) -> None:
        """Persist ``content`` to storage and refresh the cached entry."""
        self._storage.write(path, content)
        self._cache[path] = content
        logger.debug("cache write-through: %s", path)

    def list(self, prefix: str = "") -> list[str]:
        """List document paths under ``prefix`` (delegated, not cached)."""
        return self._storage.list(prefix)

    def exists(self, path: str) -> bool:
        """Return ``True`` if ``path`` is cached or present in storage."""
        if path in self._cache:
            return True
        return self._storage.exists(path)

    def invalidate(self, path: str) -> None:
        """Drop the cached entry for ``path`` (next read re-fetches)."""
        if self._cache.pop(path, None) is not None:
            logger.debug("cache invalidate: %s", path)
