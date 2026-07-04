"""MCP-resource chunking (F20): mime-type -> chunk strategy.

:class:`McpResourceChunker` turns a fetched (already redacted, size-capped)
:class:`McpResourceSnapshot` into ``Chunk`` objects of type
``ChunkType.MCP_RESOURCE`` (weight 1.0), routed by mime-type:

* ``text/markdown`` / ``text/x-markdown`` -> the F05 markdown splitter (headings +
  paragraphs).
* ``text/html`` -> tag-stripped, then the markdown splitter.
* ``application/json`` -> structured key-path windows (one chunk per top-level key).
* ``text/plain`` / unknown text / ``None`` -> the paragraph/line-window splitter.
* non-text (binary / ``blob``) -> **skipped** (returns ``[]``; the caller records a
  ledger row with ``chunk_count=0``).

Every emitted chunk carries the full provenance ``path = mcp://{slug}/{uri}`` (so
the F05 dedup/delete-by-path upsert path applies unchanged) and ``metadata`` with
``resource_uri``, ``mcp_namespace``, ``connection_slug``, ``title``, ``url``,
``mime_type``, ``chunk_index`` and ``source_uri`` (mirrors ``path`` for the
Approval-UI provenance contract).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

from pydantic import BaseModel

from forge_contracts.constants import CHUNK_TYPE_WEIGHTS
from forge_contracts.dtos import Chunk
from forge_contracts.enums import ChunkType
from forge_knowledge.chunking import DEFAULT_MAX_CHARS, chunk_markdown

__all__ = ["McpResourceChunker", "McpResourceSnapshot", "provenance_uri"]

_MCP_WEIGHT = CHUNK_TYPE_WEIGHTS[ChunkType.MCP_RESOURCE]

# Mime types (besides ``text/*``) treated as indexable text.
_TEXT_APPLICATION_MIMES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/markdown",
        "application/javascript",
        "application/x-ndjson",
    }
)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?>.*?</\1>")
_WS_RUN_RE = re.compile(r"[ \t]+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


class McpResourceSnapshot(BaseModel):
    """A fetched MCP resource (already redacted + size-capped by the gateway)."""

    uri: str
    content: str
    connection_slug: str
    mime_type: str | None = None
    namespace: str | None = None
    title: str | None = None
    url: str | None = None
    change_token: str | None = None


def provenance_uri(connection_slug: str, resource_uri: str) -> str:
    """Stable provenance path for an MCP-indexed chunk: ``mcp://{slug}/{uri}``."""
    return f"mcp://{connection_slug}/{resource_uri}"


def _hash(path: str, index: int, content: str) -> str:
    """Per-resource-unique content hash (path + index keep identical bodies distinct)."""
    return hashlib.sha256(f"{path}\0{index}\0{content}".encode()).hexdigest()


def _normalise_mime(mime_type: str | None) -> str:
    return (mime_type or "").split(";", 1)[0].strip().lower()


def _is_text(mime: str) -> bool:
    if mime == "":
        return True  # no mime -> assume text (gateway content is a str)
    if mime.startswith("text/"):
        return True
    return mime in _TEXT_APPLICATION_MIMES


def _strip_html(raw: str) -> str:
    without_blocks = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", without_blocks)
    text = html.unescape(text)
    text = _WS_RUN_RE.sub(" ", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _json_blocks(raw: str) -> list[str]:
    """Pretty-print JSON into one block per top-level key (recursive key-paths)."""
    try:
        data: Any = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        return [
            f"{key}:\n{json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)}"
            for key, value in data.items()
        ] or [json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)]
    return [json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)]


class McpResourceChunker:
    """Mime-type-routed chunker emitting ``ChunkType.MCP_RESOURCE`` chunks."""

    def __init__(self, *, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self._max_chars = max_chars

    def chunk(self, snapshot: McpResourceSnapshot) -> list[Chunk]:
        mime = _normalise_mime(snapshot.mime_type)
        if not _is_text(mime):
            return []  # binary / blob — indexing is out of scope (F20 §12)

        path = provenance_uri(snapshot.connection_slug, snapshot.uri)
        contents = self._split(snapshot.content, mime, path)

        base_metadata = {
            "resource_uri": snapshot.uri,
            "mcp_namespace": snapshot.namespace,
            "connection_slug": snapshot.connection_slug,
            "title": snapshot.title,
            "url": snapshot.url,
            "mime_type": snapshot.mime_type,
            "source_uri": path,
            "file_path": path,
        }

        chunks: list[Chunk] = []
        for index, (content, extra) in enumerate(contents):
            metadata = {**base_metadata, "chunk_index": index}
            metadata.update(extra)
            chunks.append(
                Chunk(
                    content=content,
                    chunk_type=ChunkType.MCP_RESOURCE,
                    path=path,
                    language=mime or None,
                    weight=_MCP_WEIGHT,
                    content_hash=_hash(path, index, content),
                    metadata=metadata,
                )
            )
        return chunks

    def _split(self, content: str, mime: str, path: str) -> list[tuple[str, dict[str, Any]]]:
        if not content.strip():
            return []
        if mime == "application/json":
            return [(block, {}) for block in _json_blocks(content)] or [(content, {})]
        if mime == "text/html":
            content = _strip_html(content)
            if not content.strip():
                return []
        # markdown, text/plain, and unknown text all use the markdown/paragraph
        # splitter (it carries heading breadcrumbs and splits over-long blocks).
        md_chunks = chunk_markdown(path, content, max_chars=self._max_chars)
        if not md_chunks:
            return [(content, {})]
        out: list[tuple[str, dict[str, Any]]] = []
        for c in md_chunks:
            extra = {k: v for k, v in c.metadata.items() if k in ("heading", "heading_path")}
            out.append((c.content, extra))
        return out
