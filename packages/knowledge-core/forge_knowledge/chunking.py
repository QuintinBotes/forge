"""Source chunking for the Forge Knowledge/RAG pipeline (plan Task 1.1, spine).

This module turns raw source into :class:`forge_contracts.dtos.Chunk` objects —
the un-indexed unit that the embedding + hybrid-search layers (Tasks 1.2-1.4)
consume. Two strategies, mirroring the spec's *Chunk Types and Priority Weights*
table:

* :func:`chunk_code` — function/class-level code chunking. The primary backend
  is :mod:`forge_knowledge.treesitter_chunking` (multi-language: Python,
  JavaScript/JSX, TypeScript/TSX, Go); the stdlib ``ast`` chunker is the Python
  fallback when tree-sitter is unavailable. Either way, each top-level
  ``def``/``class``/func/type becomes one chunk (decorators / ``export``
  included in the span); the remaining top-level statements (module docstring,
  imports, constants, ``__main__`` guard) are grouped into contiguous
  ``<module>`` chunks. Malformed input degrades to a single whole-file chunk —
  chunking never raises on bad input.
* :func:`chunk_markdown` — section chunking (spec J §2.4.1, D-a). A heading and
  its body up to the next heading of the same or a higher level is one chunk
  when it fits ``max_chars``; otherwise it is packed at paragraph boundaries,
  keeping lists and fenced code blocks whole. Every chunk starts with a
  breadcrumb line: the path and headings joined by `` \u203a `` (single
  right angle quote), then ``(status: …)``. It doubles as the generic text
  chunker for non-Python files.

Chunk-type weights come from the frozen ``CHUNK_TYPE_WEIGHTS`` table in
``forge_contracts.constants`` (single source of truth): README 1.3,
policy/AGENTS.md 1.5, spec/plan/validation 1.4, summary 1.2, default 1.0.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field

from forge_contracts.constants import CHUNK_TYPE_WEIGHTS
from forge_contracts.dtos import Chunk
from forge_contracts.enums import ChunkType
from forge_knowledge.treesitter_chunking import (
    TREE_SITTER_LANGUAGES,
    chunk_with_treesitter,
    language_for_path,
    treesitter_available,
)

__all__ = [
    "CHUNKER_VERSION",
    "DEFAULT_MAX_CHARS",
    "TREE_SITTER_LANGUAGES",
    "chunk_code",
    "chunk_file",
    "chunk_markdown",
    "chunk_paragraphs",
    "classify_path",
    "treesitter_available",
    "weight_for",
]

#: Default soft ceiling (characters) for a single markdown/text chunk. Over-long
#: paragraphs are split on line boundaries so no single chunk dwarfs the
#: embedding context window. Code chunks follow AST units and are never split.
DEFAULT_MAX_CHARS: int = 1200

# Greedy capture to end-of-line (no lazy `.*?` + trailing `\s*` overlap, which
# CodeQL flags as polynomial/ReDoS); the closing ATX `#`s + spaces are stripped
# from group(2) in code below, which is linear.
_HEADING_RE = re.compile(r"^[ ]{0,3}(#{1,6})[ \t]+(\S.*)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# A top-level front-matter key (no indentation), and a body line that starts
# exactly with `Status:` or `**Status:**` (never a list item or a quote).
_FRONT_STATUS_RE = re.compile(r"^status[ \t]*:[ \t]*(\S.*)$")
_STATUS_LINE_RE = re.compile(r"^(?:Status:|\*\*Status:\*\*)[ \t]*(\S.*)$")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

#: Joins the path and headings in a chunk's breadcrumb line (U+203A, spec J §2.4.1).
_CRUMB_SEP = " \u203a "

#: Longest document status carried in every chunk's breadcrumb line.
_MAX_STATUS_CHARS = 120

#: Bumped whenever chunk output changes, so syncs that skip unchanged
#: resources (the MCP ledger) re-chunk each of them once.
CHUNKER_VERSION = 2

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
    """Chunk source into one chunk per top-level definition.

    The tree-sitter backend (:mod:`forge_knowledge.treesitter_chunking`) is tried
    first for any supported ``language`` (Python, JavaScript/JSX, TypeScript/TSX,
    Go). When tree-sitter or the grammar is unavailable, or the parse errors,
    Python falls back to the stdlib ``ast`` chunker and other languages to a
    single whole-file chunk.

    Top-level statements that are not functions/classes (module docstring,
    imports, constants, ``__main__`` guard) are grouped into contiguous
    ``<module>`` chunks so identifier-bearing preamble survives for keyword
    (BM25) retrieval. Decorators / ``export`` wrappers are included in a
    definition's line span.

    Never raises on malformed input: a ``SyntaxError`` (or empty AST with
    content present) falls back to a single whole-file chunk.
    """
    if not src.strip():
        return []

    ts_chunks = chunk_with_treesitter(path, src, language, weight=weight_for(ChunkType.CODE))
    if ts_chunks is not None:
        return ts_chunks

    if language != "python":
        return [_whole_file_code_chunk(path, src, language)]

    return _chunk_python_ast(path, src, language)


def _chunk_python_ast(path: str, src: str, language: str) -> list[Chunk]:
    """Stdlib ``ast`` Python chunker — the fallback when tree-sitter is absent."""
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


def chunk_paragraphs(
    path: str,
    src: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Chunk]:
    """Chunk markdown/text into heading and paragraph chunks (no breadcrumb).

    The pre-section chunker, kept for callers that need each chunk to be a
    verbatim slice of ``src`` (world-model evidence offsets). Each chunk's
    ``chunk_type`` (and weight) is derived from ``path`` via :func:`classify_path`.
    Paragraph chunks carry the active heading breadcrumb in
    ``metadata['heading']`` / ``metadata['heading_path']`` for retrieval
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
            # Strip the optional closing ATX run of `#`s and surrounding spaces.
            text = heading_match.group(2).strip().rstrip("#").strip()
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


@dataclass
class _Section:
    """A heading (``level`` 0: the preamble) and its own units, then subsections."""

    level: int
    title: str | None
    units: list[tuple[int, int]] = field(default_factory=list)
    children: list[_Section] = field(default_factory=list)

    def span(self) -> tuple[int, int] | None:
        """Inclusive line span of the section with every subsection."""
        spans = [s for s in (*self.units, *(c.span() for c in self.children)) if s is not None]
        return (spans[0][0], spans[-1][1]) if spans else None


def _subtree_empty(section: _Section) -> bool:
    """Only a heading here and in every subsection: no body to chunk."""
    return len(section.units) == 1 and all(_subtree_empty(c) for c in section.children)


def _units(lines: list[str]) -> list[tuple[int, int, re.Match[str] | None]]:
    """Blocks, with a loose list (blank lines between items) merged into one unit."""
    units: list[tuple[int, int, re.Match[str] | None]] = []
    for start, end in _split_markdown_blocks(lines):
        heading = _HEADING_RE.match(lines[start]) if start == end else None
        if heading is None and units and units[-1][2] is None:
            prev_start, _, _ = units[-1]
            continues = _LIST_ITEM_RE.match(lines[start]) or lines[start][:1] in (" ", "\t")
            if _LIST_ITEM_RE.match(lines[prev_start]) and continues:
                units[-1] = (prev_start, end, None)
                continue
        units.append((start, end, heading))
    return units


def _clean_status(value: str) -> str | None:
    value = " ".join(value.strip("*_'\" \t").split())
    return value[:_MAX_STATUS_CHARS] or None


def _document_status(lines: list[str]) -> str | None:
    """The document's status, from the first of these that has one.

    A top-level ``status:`` key in a leading ``---`` front-matter block; a
    line starting exactly ``Status:`` or ``**Status:**`` before the first
    H2 (outside fences); the first paragraph under a ``## Status`` heading.
    """
    body = 0
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            for line in lines[1:end]:
                if match := _FRONT_STATUS_RE.match(line):
                    return _clean_status(match.group(1))
            body = end + 1
    rest = lines[body:]
    blocks = _split_markdown_blocks(rest)
    before_h2 = True
    for i, (start, end) in enumerate(blocks):
        heading = _HEADING_RE.match(rest[start]) if start == end else None
        if heading is not None and len(heading.group(1)) == 2:
            before_h2 = False
            title = heading.group(2).strip().rstrip("#").strip()
            if title.lower() == "status" and i + 1 < len(blocks):
                n_start, n_end = blocks[i + 1]
                first = rest[n_start]
                if not _not_prose(first):
                    return _clean_status(" ".join(rest[n_start : n_end + 1]))
        elif before_h2 and not _FENCE_RE.match(rest[start]) and not _is_comment(rest[start]):
            for line in rest[start : end + 1]:
                if match := _STATUS_LINE_RE.match(line):
                    return _clean_status(match.group(1))
    return None


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("<!--")


def _not_prose(line: str) -> bool:
    """A heading, fence, list item, quote or HTML comment: never a status paragraph."""
    return bool(
        _HEADING_RE.match(line)
        or _FENCE_RE.match(line)
        or _LIST_ITEM_RE.match(line)
        or line.lstrip().startswith(">")
        or _is_comment(line)
    )


def _pack(
    lines: list[str],
    units: list[tuple[int, int]],
    budget: int,
    limit: int,
    *,
    heading_first: bool = False,
) -> list[tuple[str, int, int]]:
    """Group whole units into pieces of at most ``budget`` chars (``(text, start, end)``).

    A unit (a list, fence or paragraph) over the budget stays one piece up to
    ``limit`` chars; only a unit longer than that is cut, on line boundaries.
    With ``heading_first``, ``units[0]`` is a heading that joins the unit
    after it rather than standing alone.
    """
    pieces: list[tuple[str, int, int]] = []
    current: tuple[int, int] | None = None

    def text(start: int, end: int) -> str:
        return "\n".join(lines[start : end + 1])

    for i, (unit_start, end) in enumerate(units):
        start = unit_start
        if current is not None and len(text(current[0], end)) <= budget:
            current = (current[0], end)
            continue
        if current is not None and heading_first and i == 1:
            start = current[0]  # the lone heading travels with this unit
        elif current is not None:
            pieces.append((text(*current), *current))
        current = None
        if len(text(start, end)) <= budget:
            current = (start, end)
        elif len(text(unit_start, end)) <= limit:
            pieces.append((text(start, end), start, end))
        else:
            cut = _split_long_block(lines[unit_start : end + 1], unit_start, budget)
            first_end = cut[0][2]
            cut[0] = (text(start, first_end), start, first_end)
            pieces.extend(cut)
    if current is not None:
        pieces.append((text(*current), *current))
    return pieces


def chunk_markdown(
    path: str,
    src: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Chunk]:
    """Chunk markdown/text by section (spec J §2.4.1, D-a).

    A section is a heading plus its body up to the next heading of the same
    or a higher level; it is one chunk when it fits ``max_chars``, otherwise
    its own body is packed at paragraph boundaries and each subsection is
    chunked the same way. A list (a requirement block) is never split unless
    it alone exceeds ``max_chars``. Every chunk's content starts with the
    breadcrumb line ``"{path} \u203a {heading path} (status: …, amended …)"`` —
    status from front matter or the first ``Status:`` line, plus the newest
    amendment-heading date; the parenthesis is omitted when there is
    neither. ``metadata['heading']`` / ``metadata['heading_path']`` carry the
    breadcrumb too; ``start_line``/``end_line`` span the body after it.
    """
    if not src.strip():
        return []

    lines = src.splitlines()
    chunk_type = classify_path(path)
    weight = weight_for(chunk_type)

    root = _Section(level=0, title=None)
    stack = [root]
    headings: list[str] = []
    for start, end, heading in _units(lines):
        if heading is None:
            stack[-1].units.append((start, end))
            continue
        level = len(heading.group(1))
        # Strip the optional closing ATX run of `#`s and surrounding spaces.
        title = heading.group(2).strip().rstrip("#").strip()
        headings.append(title)
        while stack[-1].level >= level:
            stack.pop()
        section = _Section(level=level, title=title, units=[(start, end)])
        stack[-1].children.append(section)
        stack.append(section)

    notes: list[str] = []
    status = _document_status(lines)
    if status:
        notes.append(f"status: {status}")
    amended = [d for h in headings if "amend" in h.lower() for d in _DATE_RE.findall(h)]
    if amended:
        notes.append(f"amended {max(amended)}")
    suffix = f" ({', '.join(notes)})" if notes else ""

    chunks: list[Chunk] = []

    def emit(
        section: _Section, crumbs: list[str], carry: int | None = None, *, last: bool = True
    ) -> int | None:
        """Chunk ``section``, starting at ``carry`` (an earlier lone heading) if given.

        Returns the start of a lone heading still waiting for a body (an empty
        section with more of the document after it) for the next section.
        """
        header = _CRUMB_SEP.join([path, *crumbs]) + suffix
        budget = max(max_chars - len(header) - 1, max_chars // 2) if max_chars else len(src) + 1
        metadata: dict[str, object] = {}
        if crumbs:
            metadata = {
                "heading": crumbs[-1],
                "heading_path": " > ".join(crumbs),
                "level": section.level,
            }
        units = list(section.units)
        span = section.span()
        if carry is not None and units and span:
            units[0] = (carry, units[0][1])
            span = (carry, span[1])
        whole = "\n".join(lines[span[0] : span[1] + 1]) if span else ""
        children = section.children
        pieces: list[tuple[str, int, int]] = []
        if section.level > 0 and _subtree_empty(section) and not last:
            # Headings with no body anywhere below: the first opens the next section.
            return units[0][0]
        # The preamble (level 0) never absorbs the sections after it.
        if section.level > 0 and span and len(whole) <= budget:
            pieces = [(whole, span[0], span[1])]
            children = []
            carry = None
        elif section.level > 0 and len(units) == 1 and children:
            carry = units[0][0]  # a heading with only subsections opens the first one
        else:
            carry = None
            pieces = _pack(
                lines, units, budget, max_chars or budget, heading_first=section.level > 0
            )
        for body, start, end in pieces:
            content = f"{header}\n{body}"
            chunks.append(
                Chunk(
                    content=content,
                    chunk_type=chunk_type,
                    path=path,
                    start_line=start + 1,
                    end_line=end + 1,
                    language="markdown",
                    symbol=section.title,
                    weight=weight,
                    content_hash=_hash(content),
                    metadata=dict(metadata),
                )
            )
        for i, child in enumerate(children):
            child_last = last and i == len(children) - 1
            carry = emit(child, [*crumbs, child.title or ""], carry, last=child_last)
        return carry

    emit(root, [])
    return chunks


# --------------------------------------------------------------------------- #
# Dispatcher                                                                   #
# --------------------------------------------------------------------------- #


def chunk_file(path: str, src: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Chunk a file by routing on its path.

    Python files always go through :func:`chunk_code` (tree-sitter primary, stdlib
    ``ast`` fallback). Other code files with a tree-sitter grammar available
    (JavaScript/JSX, TypeScript/TSX, Go) also go through :func:`chunk_code`.
    Everything else — markdown, docs, policy/spec YAML, and code files with no
    available grammar — goes through the paragraph/text chunker.
    """
    language = language_for_path(path)
    if language == "python":
        return chunk_code(path, src, language="python")
    if language is not None and treesitter_available(language):
        return chunk_code(path, src, language=language)
    return chunk_markdown(path, src, max_chars=max_chars)
