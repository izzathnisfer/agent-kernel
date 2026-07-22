"""Blob-store abstraction for OKF bundles.

The storage layer is a mostly-blob store: ``read`` / ``write`` / ``list`` /
``exists`` over bundle-relative POSIX paths, plus one metadata probe —
``last_modified`` — that reports a path's last-modified time without reading its
content. That probe is what the Curator's sync uses to decide whether a source
file changed since the last run (see :mod:`okf.sync`); a document's ``timestamp``
frontmatter remains the *write* time, independent of the source's mtime.

Two first-class implementations ship with the example:

- :class:`FileSystemStorage` — a local directory; the no-AWS run path and the
  backend used by the offline tests.
- :class:`S3Storage` — an ``s3://<bucket>/<prefix>/`` backend (boto3).

Both take explicit constructor parameters (never global config), mirroring the
shared-driver rule in the Agent Kernel core: the code that constructs the
storage decides where the bytes live.
"""

from __future__ import annotations

import logging
import posixpath
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3.client import S3Client

logger = logging.getLogger(__name__)


class NotFoundError(KeyError):
    """Raised by :meth:`OKFStorage.read` when a path does not exist."""


class OKFStorage(ABC):
    """Minimal blob-store surface over bundle-relative POSIX paths.

    Implementations map a bundle-relative path such as ``tables/orders.md`` to
    their own addressing scheme (a filesystem path, an S3 key, ...). Paths are
    always POSIX-style and never start with ``/``.
    """

    @abstractmethod
    def read(self, path: str) -> str:
        """Return the UTF-8 content stored at ``path``.

        :raises NotFoundError: if ``path`` does not exist.
        """

    @abstractmethod
    def write(self, path: str, content: str) -> None:
        """Create or replace the document at ``path`` with ``content``."""

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]:
        """Return all document paths under ``prefix`` (recursive), sorted.

        An empty ``prefix`` lists the whole bundle. Paths are returned
        bundle-relative and POSIX-style.
        """

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return ``True`` if a document exists at ``path``."""

    @abstractmethod
    def last_modified(self, path: str) -> Optional[datetime]:
        """Return the last-modified time of ``path``, or ``None`` if it is absent.

        The returned datetime is timezone-aware. This is the only metadata the
        blob surface exposes; sync uses it to skip source files that have not
        changed since the last run.
        """


def _normalize(path: str) -> str:
    """Normalize a bundle-relative path to POSIX form with no leading slash.

    Raises :class:`ValueError` if the path escapes the bundle root via ``..``.
    The agent-facing tools already reject such paths with a descriptive error;
    this is defence in depth so a bypass never reads or writes outside the root,
    mirroring how the IAM policies back up the tool-subset permission model.
    """
    normalized = posixpath.normpath(path.strip().lstrip("/"))
    if normalized in (".", ""):
        return ""
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"path '{path}' escapes the bundle root")
    return normalized


class FileSystemStorage(OKFStorage):
    """Store a bundle as files under a local directory.

    This is the no-AWS run path and the backend the offline tests use. The root
    directory is created on construction if it does not already exist.
    """

    def __init__(self, root: str) -> None:
        """:param root: local directory that holds the bundle root."""
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _full(self, path: str) -> Path:
        rel = _normalize(path)
        return self._root / rel

    def read(self, path: str) -> str:
        full = self._full(path)
        if not full.is_file():
            logger.debug("fs read miss: %s (root=%s)", path, self._root)
            raise NotFoundError(path)
        content = full.read_text(encoding="utf-8")
        logger.debug("fs read: %s (%d chars)", path, len(content))
        return content

    def write(self, path: str, content: str) -> None:
        full = self._full(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        logger.debug("fs write: %s (%d chars)", path, len(content))

    def list(self, prefix: str = "") -> list[str]:
        base = self._full(prefix) if prefix else self._root
        if base.is_file():
            return [_normalize(prefix)]
        if not base.is_dir():
            logger.debug("fs list: %r -> 0 paths (no such directory)", prefix)
            return []
        paths = [p.relative_to(self._root).as_posix() for p in base.rglob("*") if p.is_file()]
        logger.debug("fs list: %r -> %d paths", prefix, len(paths))
        return sorted(paths)

    def exists(self, path: str) -> bool:
        return self._full(path).is_file()

    def last_modified(self, path: str) -> Optional[datetime]:
        full = self._full(path)
        if not full.is_file():
            return None
        mtime = datetime.fromtimestamp(full.stat().st_mtime, tz=timezone.utc)
        logger.debug("fs last_modified: %s -> %s", path, mtime.isoformat())
        return mtime


class S3Storage(OKFStorage):
    """Store a bundle under ``s3://<bucket>/<prefix>/`` via boto3.

    ``S3Storage`` assumes the bucket already exists — it never creates buckets.
    Provisioning (buckets + IAM policies) lives entirely in the ``deploy/``
    Terraform module. Constructor parameters are explicit; nothing is read from
    global config.

    Used read-write for the bundle bucket and read-only for the source bucket;
    the read-only contract is enforced by which tools reach a given instance and
    (at the AWS boundary) by the IAM policy attached to the running principal.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: Optional[str] = None,
        client: Optional["S3Client"] = None,
    ) -> None:
        """:param bucket: existing S3 bucket name.

        :param prefix: key prefix that acts as the bundle root (``""`` = bucket root).
        :param region: AWS region for a boto3-created client; ignored when ``client`` is given.
        :param client: optional pre-built boto3 S3 client (used by tests).
        """
        self._bucket = bucket
        self._prefix = prefix.strip().strip("/")
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client("s3", region_name=region)
        logger.info("S3Storage bound to s3://%s/%s (region=%s)", bucket, self._prefix, region or "default")

    def _key(self, path: str) -> str:
        rel = _normalize(path)
        return f"{self._prefix}/{rel}" if self._prefix else rel

    def read(self, path: str) -> str:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._key(path))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                logger.debug("s3 read miss: s3://%s/%s", self._bucket, self._key(path))
                raise NotFoundError(path) from exc
            raise
        content = resp["Body"].read().decode("utf-8")
        logger.debug("s3 read: s3://%s/%s (%d chars)", self._bucket, self._key(path), len(content))
        return content

    def write(self, path: str, content: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key(path),
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        logger.debug("s3 write: s3://%s/%s (%d chars)", self._bucket, self._key(path), len(content))

    def list(self, prefix: str = "") -> list[str]:
        key_prefix = self._key(prefix) if prefix else self._prefix
        # A non-empty key prefix that maps to a "directory" must end with "/" so
        # that "tables" does not also match "tables-archive/...".
        if key_prefix and not key_prefix.endswith("/") and not self.exists(prefix):
            key_prefix += "/"
        paginator = self._client.get_paginator("list_objects_v2")
        strip = f"{self._prefix}/" if self._prefix else ""
        paths: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=key_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if strip and key.startswith(strip):
                    key = key[len(strip) :]
                elif strip:
                    continue
                if key:
                    paths.append(key)
        logger.debug("s3 list: %r -> %d paths (bucket=%s)", prefix, len(paths), self._bucket)
        return sorted(paths)

    def exists(self, path: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(path))
            return True
        except ClientError:
            return False

    def last_modified(self, path: str) -> Optional[datetime]:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=self._key(path))
        except ClientError:
            return None
        # S3 returns a timezone-aware datetime for LastModified.
        mtime: Optional[datetime] = resp.get("LastModified")
        logger.debug("s3 last_modified: s3://%s/%s -> %s", self._bucket, self._key(path), mtime)
        return mtime
