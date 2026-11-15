"""Code-symbol index (spec J §2.3, D-c): symbol -> file, as searchable chunks.

:func:`symbol_chunks` turns the tree-sitter (or ``ast``) code chunks of a file
into one ``ChunkType.SUMMARY`` chunk per named top-level symbol, with content
``"{path}: {symbol} — {signature}"`` (or the first doc line when no signature
line is found) and ``metadata["symbol"]``. Pure; idempotent by content hash
like every chunk, and stored under the code file's path so a sync that
replaces or prunes the file replaces or prunes its symbols with it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from forge_contracts.dtos import Chunk
from forge_contracts.enums import ChunkType
from forge_knowledge.chunking import weight_for

__all__ = ["symbol_chunks"]

#: Lines a multi-line signature may span before it is cut.
_MAX_SIGNATURE_LINES = 5

#: Longest signature text kept (characters).
_MAX_SIGNATURE_CHARS = 300

_DOC_PREFIXES = ('"""', "'''", "//", "/*", "*", "#")


def _signature(content: str, symbol: str) -> str:
    """The definition line(s) naming ``symbol``, whitespace-collapsed."""
    lines = content.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if symbol in line and not line.lstrip().startswith("@")),
        None,
    )
    if start is None:
        return ""
    parts: list[str] = []
    for line in lines[start : start + _MAX_SIGNATURE_LINES]:
        parts.append(line.strip())
        text = " ".join(parts)
        if text.count("(") <= text.count(")"):
            break
    text = re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", " ".join(" ".join(parts).split())))
    return text.rstrip("{").strip()[:_MAX_SIGNATURE_CHARS]


def _first_doc_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(_DOC_PREFIXES):
            text = stripped.strip("\"'/*# ").strip()
            if text:
                return text[:_MAX_SIGNATURE_CHARS]
    return ""


def symbol_chunks(code_chunks: Sequence[Chunk]) -> list[Chunk]:
    """One SUMMARY chunk per named symbol among ``code_chunks`` (pure)."""
    out: list[Chunk] = []
    for chunk in code_chunks:
        symbol = chunk.symbol
        if chunk.chunk_type is not ChunkType.CODE or not symbol or symbol == "<module>":
            continue
        detail = _signature(chunk.content, symbol) or _first_doc_line(chunk.content)
        content = f"{chunk.path}: {symbol} — {detail}" if detail else f"{chunk.path}: {symbol}"
        out.append(
            Chunk(
                content=content,
                chunk_type=ChunkType.SUMMARY,
                path=chunk.path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                language=chunk.language,
                symbol=symbol,
                weight=weight_for(ChunkType.SUMMARY),
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                metadata={"symbol": symbol},
            )
        )
    return out
