"""OKF document format: frontmatter parsing/serialization and link resolution.

An OKF concept document is a markdown file in two parts:

- a **metadata block** — YAML frontmatter delimited by ``---`` lines. Only
  ``type`` is required; standard optional fields are ``title``, ``description``,
  ``resource``, ``tags`` (list), ``timestamp`` (ISO-8601).
- **document details** — the free markdown body.

Relationships are ordinary markdown links between concept documents. The
:class:`OKFFormat` helper also owns the shared link-resolution rule used by both
``get_related`` and the write guardrail's link check.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

import yaml


class FrontmatterError(ValueError):
    """Raised when a document's frontmatter is missing or not valid YAML."""


@dataclass
class OKFDocument:
    """A parsed OKF concept document: metadata block + markdown body."""

    metadata: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def type(self) -> Any:
        """The document ``type`` (the spec's only required field), or ``None``."""
        return self.metadata.get("type")

    @property
    def title(self) -> Any:
        """The document ``title`` if present, else ``None``."""
        return self.metadata.get("title")


class OKFFormat:
    """Stateless helpers for parsing, serializing, and linking OKF documents."""

    _FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
    # Markdown inline links: [text](target). Bare autolinks and reference-style
    # links are out of scope for the example.
    _LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    # A target we treat as an in-bundle path (vs an external URL, anchor, or mailto).
    _EXTERNAL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://|^mailto:|^#", re.IGNORECASE)

    @staticmethod
    def parse_document(content: str) -> OKFDocument:
        """Parse raw markdown into an :class:`OKFDocument`.

        :raises FrontmatterError: if the frontmatter block is absent or not a valid
            YAML mapping.
        """
        match = OKFFormat._FRONTMATTER_RE.match(content.lstrip("\ufeff"))
        if not match:
            raise FrontmatterError("document is missing a YAML frontmatter block delimited by '---' lines")
        raw_meta, body = match.group(1), match.group(2)
        try:
            meta = yaml.safe_load(raw_meta)
        except yaml.YAMLError as exc:
            raise FrontmatterError(f"frontmatter is not valid YAML: {exc}") from exc
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise FrontmatterError("frontmatter must be a YAML mapping of fields")
        return OKFDocument(metadata=meta, body=body)

    @staticmethod
    def serialize_document(doc: OKFDocument) -> str:
        """Serialize an :class:`OKFDocument` back to frontmatter + body markdown."""
        yaml_text = yaml.safe_dump(doc.metadata, sort_keys=False, allow_unicode=True).rstrip("\n")
        body = doc.body.lstrip("\n")
        return f"---\n{yaml_text}\n---\n\n{body}"

    @staticmethod
    def extract_links(body: str) -> list[str]:
        """Return the raw link targets from markdown inline links in ``body``."""
        return OKFFormat._LINK_RE.findall(body)

    @staticmethod
    def is_internal_link(target: str) -> bool:
        """Return ``True`` if ``target`` is an in-bundle link (not a URL/anchor)."""
        return not OKFFormat._EXTERNAL_RE.match(target.strip())

    @staticmethod
    def resolve_link(target: str, current_path: str) -> str:
        """Resolve an in-bundle link to a normalized bundle-relative POSIX path.

        - **absolute** links (``/tables/orders.md``) resolve from the bundle root;
        - **relative** links (``../orders.md``) resolve against the directory of
          ``current_path``.

        A leading ``..`` that escapes the bundle root is preserved in the result
        (as a ``..`` segment) so the validator can detect and reject it.

        :param target: the raw link target.
        :param current_path: bundle-relative path of the document containing the link.
        :return: the normalized bundle-relative target path.
        """
        target = target.split("#", 1)[0].strip()  # drop any anchor fragment
        if target.startswith("/"):
            return posixpath.normpath(target.lstrip("/"))
        current_dir = posixpath.dirname(current_path)
        return posixpath.normpath(posixpath.join(current_dir, target))

    @staticmethod
    def escapes_bundle(resolved_path: str) -> bool:
        """Return ``True`` if a resolved link path points outside the bundle root."""
        return resolved_path == ".." or resolved_path.startswith("../")
