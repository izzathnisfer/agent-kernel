"""Write guardrails: OKF document parsing/validation inside the write path.

This is the Design page's "Document Parsing" guardrail, implemented directly in
the write path rather than wired into Agent Kernel's guardrail-provider system.

Validation follows OKF v0.1 conformance — only ``type`` is mandatory. A write is
**rejected** (with a reason returned to the agent) when:

- frontmatter is missing or not valid YAML,
- the ``type`` field is absent,
- a standard optional field is present but malformed (``tags`` not a list,
  ``timestamp`` not ISO-8601, ``resource`` not a URL), or
- a body link (absolute-from-root or relative) points outside the bundle or to
  a non-``.md`` target.

Missing optional fields (``title``, ``description``, ``timestamp``) and links to
not-yet-existing bundle documents produce **warnings**, not rejections — the
format stays minimally opinionated and bundles are built incrementally.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from okf.format import FrontmatterError, OKFFormat

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\S+$")
_STANDARD_OPTIONAL = ("title", "description", "timestamp")


@dataclass
class ValidationResult:
    """Outcome of validating a document for a write.

    ``valid`` is ``False`` when any rejection reason was recorded; ``warnings``
    are advisory and never block a write.
    """

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """A single human-readable string joining all rejection reasons."""
        return "; ".join(self.errors)


def _is_iso8601(value: object) -> bool:
    if not isinstance(value, (str, datetime)):
        return False
    if isinstance(value, datetime):
        return True
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_document(
    path: str,
    content: str,
    exists: Callable[[str], bool] | None = None,
) -> ValidationResult:
    """Validate document ``content`` destined for bundle ``path``.

    :param path: bundle-relative target path (used to resolve relative links).
    :param content: the full markdown (frontmatter + body) to validate.
    :param exists: optional callable to check whether a linked bundle path
        exists; when given, links to missing documents produce a warning.
    :return: a :class:`ValidationResult`.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        doc = OKFFormat.parse_document(content)
    except FrontmatterError as exc:
        logger.warning("validation rejected %s: %s", path, exc)
        return ValidationResult(valid=False, errors=[str(exc)])

    meta = doc.metadata

    # Only mandatory field.
    if not meta.get("type"):
        errors.append("required frontmatter field 'type' is missing")

    # Standard optional fields, validated only when present.
    if "tags" in meta and not isinstance(meta["tags"], list):
        errors.append("optional field 'tags' must be a list")
    if "timestamp" in meta and not _is_iso8601(meta["timestamp"]):
        errors.append("optional field 'timestamp' must be an ISO-8601 datetime")
    if "resource" in meta and not (isinstance(meta["resource"], str) and _URL_RE.match(meta["resource"])):
        errors.append("optional field 'resource' must be a URL (scheme://...)")

    # Body links: reject out-of-bundle / non-.md targets; warn on missing docs.
    for target in OKFFormat.extract_links(doc.body):
        if not OKFFormat.is_internal_link(target):
            continue
        resolved = OKFFormat.resolve_link(target, path)
        if OKFFormat.escapes_bundle(resolved):
            errors.append(f"link '{target}' resolves outside the bundle")
            continue
        if not resolved.endswith(".md"):
            errors.append(f"link '{target}' must point to a '.md' document")
            continue
        if exists is not None and not exists(resolved):
            warnings.append(f"link '{target}' points to a document that does not exist yet")

    # Missing optional fields warn but never reject.
    for field_name in _STANDARD_OPTIONAL:
        if not meta.get(field_name):
            warnings.append(f"optional field '{field_name}' is not set")

    if errors:
        logger.warning("validation rejected %s: %s", path, "; ".join(errors))
    elif warnings:
        logger.debug("validation warnings for %s: %s", path, "; ".join(warnings))
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)
