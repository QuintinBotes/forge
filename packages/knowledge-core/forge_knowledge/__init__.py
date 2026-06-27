"""Chunking, embeddings, hybrid retrieval, RRF fusion, and reranking."""

from __future__ import annotations

from forge_knowledge.chunking import (
    DEFAULT_MAX_CHARS,
    chunk_code,
    chunk_file,
    chunk_markdown,
    classify_path,
    weight_for,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_MAX_CHARS",
    "chunk_code",
    "chunk_file",
    "chunk_markdown",
    "classify_path",
    "weight_for",
]
