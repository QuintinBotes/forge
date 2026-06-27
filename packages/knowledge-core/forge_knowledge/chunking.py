"""Source chunking for the Forge Knowledge/RAG pipeline (plan Task 1.1, spine).

This module turns raw source into :class:`forge_contracts.dtos.Chunk` objects —
the un-indexed unit that the embedding + hybrid-search layers (Tasks 1.2-1.4)
consume. Two strategies, mirroring the spec's *Chunk Types and Priority Weights*
table:

* :func:`chunk_code` — Python function/class-level chunking via the stdlib
  ``ast``. Each top-level ``def``/``class`` becomes one chunk (decorators
  included in the span); the remaining top-level statements (module docstring,
  imports, constants, ``__main__`` guard) are grouped into contiguous
  ``<module>`` chunks. Malformed input degrades to a single whole-file chunk —
  chunking never raises on bad input.
* :func:`chunk_markdown` — semantic paragraph splitting. Splits on ATX headings
  and blank-line-separated paragraphs, keeps fenced code blocks intact, carries
  a heading breadcrumb for source attribution, and splits over-long paragraphs
  by ``max_chars``. It doubles as the generic text chunker for non-Python files.

Chunk-type weights come from the frozen ``CHUNK_TYPE_WEIGHTS`` table in
``forge_contracts.constants`` (single source of truth): README 1.3,
policy/AGENTS.md 1.5, spec/plan/validation 1.4, summary 1.2, default 1.0.

# PARKED: tree-sitter multi-language chunking is intentionally optional (plan
# Task 1.1 says "tree-sitter optional, ``ast`` fallback"). ``tree_sitter`` is not
# installed in this environment, so the active, tested path is the stdlib ``ast``
# chunker for Python plus paragraph chunking for everything else. Adding a
# tree-sitter backend later is a drop-in alternative to :func:`chunk_code`.
"""

from __future__ import annotations

import ast
import hashlib
import re

from forge_contracts.constants import CHUNK_TYPE_WEIGHTS
from forge_contracts.dtos import Chunk
from forge_contracts.enums import ChunkType

__all__ = [
    "DEFAULT_MAX_CHARS",
    "chunk_code",
    "chunk_file",
    "chunk_markdown",
    "classify_path",
    "weight_for",
]

#: Default soft ceiling (characters) for a single markdown/text chunk. Over-long
#: paragraphs are split on line boundaries so no single chunk dwarfs the
#: embedding context window. Code chunks follow AST units and are never split.
DEFAULT_MAX_CHARS: int = 1200

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# File extensions treated as source code for path classification.
_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".scala",
        ".kt",
        ".swift",
        ".sh",
        ".bash",
        ".sql",
    }
)

# Basenames that always denote spec/plan/validation artifacts (boosted 1.4).
_SPEC_BASENAMES: frozenset[str] = frozenset(
    {
        "spec.md",
        "plan.md",
        "tasks.md",
        "clarify.md",
        "validation.md",
        "decisions.md",
    }
)


# --------------------------------------------------------------------------- #
# Weights + path classification                                               #
# --------------------------------------------------------------------------- #


def weight_for(chunk_type: ChunkType) -> float:
    """Return the priority weight for ``chunk_type`` (spec weights table)."""
    return CHUNK_TYPE_WEIGHTS.get(chunk_type, 1.0)


def classify_path(path: str | None) -> ChunkType:
    """Infer the :class:`ChunkType` (and thus weight) from a file path.

    Priority mirrors the spec's freshness ordering: policy/AGENTS.md (1.5) wins,
    then README (1.3), then spec/plan/validation (1.4), then code, then markdown.
    """
    if not path:
        return ChunkType.MARKDOWN

    normalized = path.replace("\\", "/")
    parts = [segment.lower() for segment in normalized.split("/") if segment]
    name = parts[-1] if parts else ""
    _, _, ext = name.rpartition(".")
    ext = f".{ext}" if "." in name else ""

    # Policy files / AGENTS.md — highest freshness.
    if name == "agents.md" or name in {"policy.yaml", "policy.yml"} or ".forge" in parts:
        return ChunkType.POLICY

    # README files.
    if name.startswith("readme"):
        return ChunkType.README

    # Spec / plan / validation artifacts.
    if name in _SPEC_BASENAMES or "specs" in parts:
        return ChunkType.SPEC

    # Source code.
    if ext in _CODE_EXTENSIONS:
        return ChunkType.CODE

    return ChunkType.MARKDOWN


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _hash(text: str) -> str:
    """Stable content hash used for incremental-sync dedup (Task 1.4)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _node_start_line(node: ast.stmt) -> int:
    """Start line of a statement, including any decorator lines."""
    start = node.lineno
    for decorator in getattr(node, "decorator_list", []):
        start = min(start, decorator.lineno)
    return start


def _whole_file_code_chunk(path: str, src: str, language: str) -> Chunk:
    """Fallback chunk covering an entire file (unparseable / no AST units)."""
    lines = src.splitlines()
    return Chunk(
        content=src,
        chunk_type=ChunkType.CODE,
        path=path,
        start_line=1,
        end_line=max(len(lines), 1),
        language=language,
        symbol=None,
        weight=weight_for(ChunkType.CODE),
        content_hash=_hash(src),
    )


def _code_chunk(
    path: str,
    content: str,
    start_line: int,
    end_line: int,
    symbol: str | None,
    language: str,
) -> Chunk:
    return Chunk(
        content=content,
        chunk_type=ChunkType.CODE,
        path=path,
        start_line=start_line,
        end_line=end_line,
        language=language,
        symbol=symbol,
        weight=weight_for(ChunkType.CODE),
        content_hash=_hash(content),
    )


# --------------------------------------------------------------------------- #
# Code chunking (Python AST)                                                   #
# --------------------------------------------------------------------------- #


def chunk_code(path: str, src: str, *, language: str = "python") -> list[Chunk]:
    """Chunk Python source into one chunk per top-level ``def``/``class``.

    Top-level statements that are not functions/classes (module docstring,
    imports, constants, ``__main__`` guard) are grouped into contiguous
    ``<module>`` chunks so identifier-bearing preamble survives for keyword
    (BM25) retrieval. Decorators are included in a definition's line span.

    Never raises on malformed input: a ``SyntaxError`` (or empty AST with
    content present) falls back to a single whole-file chunk.
    """
    if not src.strip():
        return []

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [_whole_file_code_chunk(path, src, language)]

    lines = src.splitlines()
    chunks: list[Chunk] = []
    run: list[ast.stmt] = []

    def flush_run() -> None:
        if not run:
            return
        start = min(_node_start_line(node) for node in run)
        end = max(node.end_lineno or node.lineno for node in run)
        content = "\n".join(lines[start - 1 : end])
        if content.strip():
            chunks.append(_code_chunk(path, content, start, end, "<module>", language))
        run.clear()

    definition = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in tree.body:
        if isinstance(node, definition):
            flush_run()
            start = _node_start_line(node)
            end = node.end_lineno or start
            content = "\n".join(lines[start - 1 : end])
            chunks.append(_code_chunk(path, content, start, end, node.name, language))
        else:
            run.append(node)
    flush_run()

    if not chunks:
        return [_whole_file_code_chunk(path, src, language)]

    chunks.sort(key=lambda chunk: chunk.start_line or 0)
    return chunks


# --------------------------------------------------------------------------- #
# Markdown / text chunking                                                     #
# --------------------------------------------------------------------------- #


def _split_markdown_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Return inclusive 0-based ``(start, end)`` spans for each markdown block.

    A block is a heading line, a blank-line-separated paragraph, or a fenced
    code block (kept whole — blank lines and headings inside a fence do not
    split it).
    """
    blocks: list[tuple[int, int]] = []
    n = len(lines)
    cur_start: int | None = None
    i = 0

    def flush(end_exclusive: int) -> None:
        nonlocal cur_start
        if cur_start is not None:
            blocks.append((cur_start, end_exclusive - 1))
            cur_start = None

    while i < n:
        line = lines[i]
        if _FENCE_RE.match(line):
            flush(i)
            fence_start = i
            i += 1
            while i < n and not _FENCE_RE.match(lines[i]):
                i += 1
            if i < n:  # include the closing fence line
                i += 1
            blocks.append((fence_start, i - 1))
            continue
        if not line.strip():
            flush(i)
            i += 1
            continue
        if _HEADING_RE.match(line):
            flush(i)
            blocks.append((i, i))
            i += 1
            continue
        if cur_start is None:
            cur_start = i
        i += 1
    flush(n)
    return blocks


def _split_long_block(
    block_lines: list[str], base_line: int, max_chars: int
) -> list[tuple[str, int, int]]:
    """Split an over-long block on line boundaries into ``(content, start, end)``."""
    pieces: list[tuple[str, int, int]] = []
    buffer: list[str] = []
    buf_start = base_line
    length = 0
    for offset, line in enumerate(block_lines):
        addition = len(line) + (1 if buffer else 0)
        if buffer and length + addition > max_chars:
            end = base_line + offset - 1
            pieces.append(("\n".join(buffer), buf_start, end))
            buffer = []
            buf_start = base_line + offset
            length = 0
            addition = len(line)
        buffer.append(line)
        length += addition
    if buffer:
        pieces.append(("\n".join(buffer), buf_start, base_line + len(block_lines) - 1))
    return pieces


def chunk_markdown(
    path: str,
    src: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Chunk]:
    """Chunk markdown/text into heading and paragraph chunks.

    Each chunk's ``chunk_type`` (and weight) is derived from ``path`` via
    :func:`classify_path`. Paragraph chunks carry the active heading breadcrumb
    in ``metadata['heading']`` / ``metadata['heading_path']`` for retrieval
    source attribution. Over-long paragraphs are split by ``max_chars``.
    """
    if not src.strip():
        return []

    lines = src.splitlines()
    chunk_type = classify_path(path)
    weight = weight_for(chunk_type)
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []

    for start, end in _split_markdown_blocks(lines):
        block_lines = lines[start : end + 1]
        content = "\n".join(block_lines)
        if not content.strip():
            continue

        heading_match = _HEADING_RE.match(block_lines[0]) if start == end else None
        symbol: str | None = None
        metadata: dict[str, object] = {}

        if heading_match is not None:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            symbol = text
            metadata["level"] = level

        if heading_stack:
            metadata["heading"] = heading_stack[-1][1]
            metadata["heading_path"] = " > ".join(text for _, text in heading_stack)

        if heading_match is None and max_chars and len(content) > max_chars:
            for piece, p_start, p_end in _split_long_block(block_lines, start, max_chars):
                chunks.append(
                    Chunk(
                        content=piece,
                        chunk_type=chunk_type,
                        path=path,
                        start_line=p_start + 1,
                        end_line=p_end + 1,
                        language="markdown",
                        symbol=symbol,
                        weight=weight,
                        content_hash=_hash(piece),
                        metadata=dict(metadata),
                    )
                )
            continue

        chunks.append(
            Chunk(
                content=content,
                chunk_type=chunk_type,
                path=path,
                start_line=start + 1,
                end_line=end + 1,
                language="markdown",
                symbol=symbol,
                weight=weight,
                content_hash=_hash(content),
                metadata=dict(metadata),
            )
        )

    return chunks


# --------------------------------------------------------------------------- #
# Dispatcher                                                                   #
# --------------------------------------------------------------------------- #


def chunk_file(path: str, src: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Chunk a file by routing on its path.

    Python files go through the AST chunker; everything else (markdown, docs,
    policy/spec YAML, and non-Python source as a graceful fallback) goes through
    the paragraph/text chunker.
    """
    if path and path.lower().endswith((".py", ".pyi")):
        return chunk_code(path, src)
    return chunk_markdown(path, src, max_chars=max_chars)
