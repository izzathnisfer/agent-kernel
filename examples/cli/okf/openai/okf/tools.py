"""Agent-facing OKF tools over one shared :class:`OKFBundle`.

Every tool is a thin closure over a single ``OKFBundle`` (see
:meth:`OKFTools.build_tools`). The Consumer, Producer, and Curator agents differ
only in *which subset* of these tools each is bound — the tool subset is the
permission model (:data:`OKFTools.CONSUMER_TOOLS` / ``PRODUCER_TOOLS`` /
``CURATOR_TOOLS``). Each closure is wrapped with the OpenAI Agents SDK's
``function_tool`` so it can be handed straight to an ``Agent``'s ``tools=``.

Two invariants live here rather than in an agent prompt:

- **``index.md`` is tool-enforced.** ``write_concept`` regenerates the touched
  directory's ``index.md`` (and its ancestors up to the bundle root, so a freshly
  written subtree is reachable from the root) as a *flat mechanical listing* on
  every create/replace. This deliberately overwrites any hand-authored ordering
  or prose in a directory's index — navigation correctness is chosen over
  curation (see design Non-goals).
- Tools **return descriptive error strings, never raise**, so the agent can
  self-correct.

The lower-level helpers :meth:`OKFTools.apply_write` and
:meth:`OKFTools.append_log` are reused by the sync flow in :mod:`okf.sync`.
"""

from __future__ import annotations

import logging
import posixpath
from datetime import datetime
from typing import Any

from agents import function_tool

from okf.bundle import OKFBundle
from okf.format import OKFDocument, OKFFormat
from okf.storage import NotFoundError
from okf.validation import ValidationResult

logger = logging.getLogger(__name__)


class OKFTools:
    """Stateless OKF tool operations over an :class:`OKFBundle`.

    All methods are static and take the bundle explicitly; :meth:`build_tools`
    closes over one bundle to produce the agent-facing ``function_tool`` set.
    """

    LOG_PATH = "log.md"
    DEFAULT_SEARCH_LIMIT = 20

    CONSUMER_TOOLS = ["list_concept", "read_concept", "search_concept", "get_related"]
    PRODUCER_TOOLS = CONSUMER_TOOLS + ["write_concept", "append_log"]
    CURATOR_TOOLS = PRODUCER_TOOLS + ["sync_source", "list_source_files", "read_source_file"]

    # ----------------------------------------------------------------------- #
    # Path helpers
    # ----------------------------------------------------------------------- #

    @staticmethod
    def _norm(path: str) -> str:
        """Normalize a bundle-relative path (POSIX, no leading slash, ``""`` = root)."""
        normalized = posixpath.normpath(path.strip().lstrip("/"))
        return "" if normalized in (".", "") else normalized

    @staticmethod
    def _escape_error(path: str) -> str | None:
        """Return an error string if ``path`` escapes the bundle root, else ``None``.

        Agent-supplied paths are free-form (they originate in user prompts), so a
        ``..`` segment that normalizes to a path outside the bundle root is
        rejected here before it ever reaches storage — the tool's own ``path``
        argument gets the same containment the write guardrails apply to links.
        """
        if OKFFormat.escapes_bundle(OKFTools._norm(path)):
            logger.warning("path %r escapes the bundle root — rejected", path)
            return f"Error: path '{path}' escapes the bundle root."
        return None

    @staticmethod
    def _dir_of(path: str) -> str:
        """Return the bundle-relative directory containing ``path`` (``""`` = root)."""
        return posixpath.dirname(OKFTools._norm(path))

    @staticmethod
    def _index_path(directory: str) -> str:
        return f"{directory}/index.md" if directory else "index.md"

    @staticmethod
    def _ancestors(directory: str) -> list[str]:
        """Return ``[directory, parent, ..., ""]`` — every dir up to the root."""
        result = [directory]
        while directory:
            directory = posixpath.dirname(directory)
            result.append(directory)
        return result

    @staticmethod
    def _title_and_type(bundle: OKFBundle, path: str) -> tuple[str, str]:
        """Best-effort ``(title, type)`` for a document, for index listings."""
        fallback_title = posixpath.basename(path)
        try:
            doc = OKFFormat.parse_document(bundle.read(path))
        except (NotFoundError, ValueError):
            return fallback_title, "Document"
        title = str(doc.metadata.get("title") or fallback_title)
        doc_type = str(doc.metadata.get("type") or "Document")
        return title, doc_type

    # ----------------------------------------------------------------------- #
    # index.md generation
    # ----------------------------------------------------------------------- #

    @staticmethod
    def _direct_children(bundle: OKFBundle, directory: str) -> tuple[list[str], list[str]]:
        """Return ``(document_paths, subdirectory_names)`` directly under ``directory``."""
        prefix = f"{directory}/" if directory else ""
        docs: list[str] = []
        subdirs: set[str] = set()
        index_name = OKFTools._index_path(directory)
        for path in bundle.list_bundle(directory):
            if prefix and not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            if "/" in rest:
                subdirs.add(rest.split("/", 1)[0])
            elif path != index_name:  # never list a directory's own index in itself
                docs.append(path)
        return sorted(docs), sorted(subdirs)

    @staticmethod
    def _generate_listing(bundle: OKFBundle, directory: str) -> str | None:
        """Build the markdown body of a directory listing, or ``None`` if empty."""
        docs, subdirs = OKFTools._direct_children(bundle, directory)
        if not docs and not subdirs:
            return None
        lines: list[str] = []
        if docs:
            lines.append("## Documents")
            lines.append("")
            for doc_path in docs:
                title, doc_type = OKFTools._title_and_type(bundle, doc_path)
                lines.append(f"- [{title}](/{doc_path}) — {doc_type}")
            lines.append("")
        if subdirs:
            lines.append("## Subdirectories")
            lines.append("")
            for name in subdirs:
                child = f"{directory}/{name}" if directory else name
                lines.append(f"- [{name}/](/{OKFTools._index_path(child)})")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _regenerate_index(bundle: OKFBundle, directory: str) -> None:
        """Regenerate ``index.md`` for ``directory`` as a flat mechanical listing.

        Written straight to storage (not via :meth:`apply_write`) so it neither
        re-validates nor recurses.
        """
        listing = OKFTools._generate_listing(bundle, directory)
        if listing is None:
            return
        logger.debug("regenerating index for %s", directory or "(root)")
        label = f"/{directory}/" if directory else "the bundle root"
        heading = directory or "Bundle root"
        doc = OKFDocument(
            metadata={
                "type": "Index",
                "title": heading,
                "description": f"Auto-generated index for {label} (regenerated on every write; do not hand-edit).",
                "timestamp": datetime.now().astimezone().isoformat(),
            },
            body=f"# {heading}\n\n{listing}",
        )
        bundle.write(OKFTools._index_path(directory), OKFFormat.serialize_document(doc))

    # ----------------------------------------------------------------------- #
    # Reusable write / log helpers (also used by okf.sync)
    # ----------------------------------------------------------------------- #

    @staticmethod
    def apply_write(bundle: OKFBundle, path: str, content: str) -> ValidationResult:
        """Validate ``content`` for ``path``; on success persist it and regenerate indexes.

        Returns the :class:`ValidationResult`. On rejection nothing is written. On
        success the document is stored, the cache refreshed, and the touched
        directory's ``index.md`` (plus every ancestor index up to the root) is
        regenerated so the new document is reachable by navigation.
        """
        target = OKFTools._norm(path)
        if OKFFormat.escapes_bundle(target):
            logger.warning("write rejected: path %r escapes the bundle root", path)
            return ValidationResult(valid=False, errors=[f"path '{path}' escapes the bundle root"])
        if not target.endswith(".md"):
            logger.warning("write rejected: path %r is not a '.md' document", path)
            return ValidationResult(valid=False, errors=[f"path '{path}' must be a '.md' document"])
        result = bundle.validate(target, content)
        if not result.valid:
            return result
        bundle.write(target, content)
        ancestors = OKFTools._ancestors(OKFTools._dir_of(target))
        for directory in ancestors:
            OKFTools._regenerate_index(bundle, directory)
        logger.info("wrote %s (%d chars); regenerated %d index.md file(s)", target, len(content), len(ancestors))
        return result

    @staticmethod
    def append_log(bundle: OKFBundle, log_details: str, when: str | None = None) -> None:
        """Append ``log_details`` under today's date section in the root ``log.md``.

        New date sections are inserted newest-first, just under the log's heading.
        ``when`` overrides the date (ISO ``YYYY-MM-DD``); defaults to today.
        """
        day = when or datetime.now().astimezone().date().isoformat()
        entry = f"* {log_details.strip()}"
        header = f"## {day}"

        if bundle.exists(OKFTools.LOG_PATH):
            doc = OKFFormat.parse_document(bundle.read(OKFTools.LOG_PATH))
        else:
            doc = OKFDocument(
                metadata={
                    "type": "Change Log",
                    "title": "Change Log",
                    "description": "Chronological, date-sectioned history of bundle changes.",
                },
                body="# Change Log\n",
            )

        lines = doc.body.splitlines()
        if header in lines:
            idx = lines.index(header)
            lines.insert(idx + 1, entry)
        else:
            # Insert a new section right after the first heading line (newest-first).
            insert_at = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("#")), 0)
            section = ["", header, "", entry]
            lines[insert_at:insert_at] = section
        doc.body = "\n".join(lines).rstrip() + "\n"
        bundle.write(OKFTools.LOG_PATH, OKFFormat.serialize_document(doc))
        logger.info("appended log entry under %s", header)

    # ----------------------------------------------------------------------- #
    # Read operations
    # ----------------------------------------------------------------------- #

    @staticmethod
    def op_list_concept(bundle: OKFBundle, path: str) -> str:
        logger.debug("tool list_concept(%r)", path)
        if (err := OKFTools._escape_error(path)) is not None:
            return err
        directory = OKFTools._norm(path)
        index_path = OKFTools._index_path(directory)
        if bundle.exists(index_path):
            return bundle.read(index_path)
        listing = OKFTools._generate_listing(bundle, directory)
        if listing is None:
            return f"Error: no concept or directory found at '{path}'."
        location = f"/{directory}/" if directory else "the bundle root"
        return f"# {directory or 'Bundle root'} (no index.md; generated listing for {location})\n\n{listing}"

    @staticmethod
    def op_read_concept(bundle: OKFBundle, path: str) -> str:
        logger.debug("tool read_concept(%r)", path)
        if (err := OKFTools._escape_error(path)) is not None:
            return err
        target = OKFTools._norm(path)
        try:
            content = bundle.read(target)
        except NotFoundError:
            return f"Error: no document found at '{path}'."
        try:
            doc = OKFFormat.parse_document(content)
        except ValueError as exc:
            return f"Error: document '{path}' could not be parsed: {exc}"
        meta_lines = [f"{key}: {value}" for key, value in doc.metadata.items()]
        parsed = "\n".join(meta_lines) if meta_lines else "(none)"
        return f"# {target}\n\nParsed metadata:\n{parsed}\n\n---\n\n{doc.body.strip()}"

    @staticmethod
    def op_search_concept(bundle: OKFBundle, path: str, keyword: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
        logger.debug("tool search_concept(path=%r, keyword=%r)", path, keyword)
        if not keyword.strip():
            return "Error: search keyword must not be empty."
        if (err := OKFTools._escape_error(path)) is not None:
            return err
        target = OKFTools._norm(path)
        if bundle.exists(target) and target.endswith(".md"):
            candidates = [target]
        else:
            candidates = [p for p in bundle.list_bundle(target) if p.endswith(".md")]
        needle = keyword.lower()

        matches: list[tuple[str, list[str]]] = []
        truncated = False
        for doc_path in candidates:
            try:
                text = bundle.read(doc_path)
            except NotFoundError:
                continue
            hit_lines = [ln.strip() for ln in text.splitlines() if needle in ln.lower()]
            if not hit_lines:
                continue
            if len(matches) >= limit:
                truncated = True
                break
            matches.append((doc_path, hit_lines[:3]))

        if not matches:
            return f"No documents under '{path or 'the bundle root'}' matched '{keyword}'."
        out = [f"Found {len(matches)} document(s) matching '{keyword}':", ""]
        for doc_path, hit_lines in matches:
            out.append(f"- /{doc_path}")
            for line in hit_lines:
                out.append(f"    …{line}")
        if truncated:
            out.append("")
            out.append(f"(results truncated at {limit} documents; refine the keyword or narrow the path)")
        return "\n".join(out)

    @staticmethod
    def op_get_related(bundle: OKFBundle, path: str) -> str:
        logger.debug("tool get_related(%r)", path)
        if (err := OKFTools._escape_error(path)) is not None:
            return err
        target = OKFTools._norm(path)
        try:
            content = bundle.read(target)
        except NotFoundError:
            return f"Error: no document found at '{path}'."
        try:
            doc = OKFFormat.parse_document(content)
        except ValueError as exc:
            return f"Error: document '{path}' could not be parsed: {exc}"
        related: list[str] = []
        seen: set[str] = set()
        for raw in OKFFormat.extract_links(doc.body):
            if not OKFFormat.is_internal_link(raw):
                continue
            resolved = OKFFormat.resolve_link(raw, target)
            if resolved in seen:
                continue
            seen.add(resolved)
            marker = "" if bundle.exists(resolved) else " (not found)"
            related.append(f"- /{resolved}{marker}")
        if not related:
            return f"'{target}' links to no other bundle documents."
        return f"'{target}' links to:\n" + "\n".join(related)

    @staticmethod
    def op_write_concept(bundle: OKFBundle, path: str, content: str) -> str:
        result = OKFTools.apply_write(bundle, path, content)
        if not result.valid:
            return f"Rejected write to '{path}': {result.reason}"
        msg = f"Wrote '{OKFTools._norm(path)}' and regenerated the affected index.md."
        if result.warnings:
            msg += " Warnings: " + "; ".join(result.warnings)
        return msg

    @staticmethod
    def op_append_log(bundle: OKFBundle, log_details: str) -> str:
        if not log_details.strip():
            return "Error: log entry must not be empty."
        try:
            OKFTools.append_log(bundle, log_details)
        except ValueError as exc:
            return f"Error: existing log.md could not be parsed: {exc}"
        return "Appended an entry to log.md under today's date."

    @staticmethod
    def op_list_source_files(bundle: OKFBundle) -> str:
        logger.debug("tool list_source_files()")
        if not bundle.has_source:
            return "Error: no sync source is configured for this bundle."
        files = [p for p in bundle.list_source() if p.endswith(".md")]
        if not files:
            return "The source folder contains no markdown files."
        return "Source markdown files:\n" + "\n".join(f"- {p}" for p in files)

    @staticmethod
    def op_read_source_file(bundle: OKFBundle, path: str) -> str:
        logger.debug("tool read_source_file(%r)", path)
        if not bundle.has_source:
            return "Error: no sync source is configured for this bundle."
        if (err := OKFTools._escape_error(path)) is not None:
            return err
        try:
            return bundle.read_source(OKFTools._norm(path))
        except NotFoundError:
            return f"Error: no source file found at '{path}'."

    # ----------------------------------------------------------------------- #
    # Tool closures
    # ----------------------------------------------------------------------- #

    @staticmethod
    def build_tools(bundle: OKFBundle) -> dict[str, Any]:
        """Build the OKF ``function_tool`` set over ``bundle``, keyed by tool name.

        Each value is a callable wrapped with the OpenAI Agents SDK's
        ``function_tool``. Select a subset (:data:`OKFTools.CONSUMER_TOOLS`, etc.)
        with :meth:`select_tools` and pass the result to an ``Agent``'s ``tools=``.
        """

        def list_concept(path: str) -> str:
            """List a directory's concepts.

            Returns the directory's index.md when present, otherwise a generated
            listing of the documents directly under it. Use "" or "/" for the
            bundle root. Start discovery here.

            :param path: bundle-relative directory path (e.g. "sales" or "").
            """
            return OKFTools.op_list_concept(bundle, path)

        def read_concept(path: str) -> str:
            """Read a single concept document: its parsed metadata and full body.

            :param path: bundle-relative document path (e.g. "sales/tables/orders.md").
            """
            return OKFTools.op_read_concept(bundle, path)

        def search_concept(path: str, keyword: str) -> str:
            """Keyword-search concept documents, scoped by path.

            When path is a document, only that document is searched; when it is a
            directory (or ""), every document under it is searched recursively.
            Matching is a case-insensitive substring over the raw document text.

            :param path: bundle-relative file or directory to scope the search.
            :param keyword: substring to search for.
            """
            return OKFTools.op_search_concept(bundle, path, keyword)

        def get_related(path: str) -> str:
            """List the bundle documents a concept links to (its relationships).

            :param path: bundle-relative document path.
            """
            return OKFTools.op_get_related(bundle, path)

        def write_concept(path: str, content: str) -> str:
            """Create or replace a concept document (validated on write).

            The content must be a full OKF document: YAML frontmatter (at least a
            'type' field) followed by a markdown body. On success the document is
            stored and the affected index.md is regenerated automatically. On a
            validation failure the reason is returned so you can revise and retry.

            :param path: bundle-relative document path ending in ".md".
            :param content: the full markdown document (frontmatter + body).
            """
            return OKFTools.op_write_concept(bundle, path, content)

        def append_log(log_details: str) -> str:
            """Append a change-history entry under today's date in the root log.md.

            Call this after every successful write to record what changed.

            :param log_details: a short description of the change (may include links).
            """
            return OKFTools.op_append_log(bundle, log_details)

        def sync_source() -> str:
            """Sync the source folder into the bundle's synced/ subtree.

            Reads every markdown file in the read-only source folder, transforms it
            into an OKF document, and writes new/changed documents under synced/. A
            file is skipped when its source last-modified time is unchanged since the
            previous sync. Returns a summary of the run.
            """
            from okf.sync import OKFSync

            return OKFSync.sync_source(bundle)

        def list_source_files() -> str:
            """List the markdown files in the read-only sync source folder."""
            return OKFTools.op_list_source_files(bundle)

        def read_source_file(path: str) -> str:
            """Read one file from the read-only sync source folder.

            :param path: source-relative file path.
            """
            return OKFTools.op_read_source_file(bundle, path)

        return {
            "list_concept": function_tool(list_concept),
            "read_concept": function_tool(read_concept),
            "search_concept": function_tool(search_concept),
            "get_related": function_tool(get_related),
            "write_concept": function_tool(write_concept),
            "append_log": function_tool(append_log),
            "sync_source": function_tool(sync_source),
            "list_source_files": function_tool(list_source_files),
            "read_source_file": function_tool(read_source_file),
        }

    @staticmethod
    def select_tools(bundle: OKFBundle, names: list[str]) -> list[Any]:
        """Return the ``function_tool`` objects for ``names``, in order.

        Pass the result straight to an ``Agent``'s ``tools=`` argument.
        """
        tools = OKFTools.build_tools(bundle)
        return [tools[name] for name in names]
