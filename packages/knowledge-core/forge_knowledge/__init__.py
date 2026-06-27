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
from forge_knowledge.embeddings import (
    DeterministicEmbeddingClient,
    HttpEmbeddingClient,
)
from forge_knowledge.fusion import fuse
from forge_knowledge.reranker import (
    FixtureRerankerClient,
    JinaRerankerClient,
)
from forge_knowledge.retriever import HybridRetriever
from forge_knowledge.service import KnowledgeService
from forge_knowledge.stores import (
    Bm25Store,
    KnowledgeSourceNotFoundError,
    PgVectorStore,
)
from forge_knowledge.sync import (
    ChangeSet,
    full_sync,
    git_changed_files,
    incremental_sync,
    iter_source_files,
    read_repo_files,
    sync_source,
)
from forge_knowledge.text import tokenize

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_MAX_CHARS",
    "Bm25Store",
    "ChangeSet",
    "DeterministicEmbeddingClient",
    "FixtureRerankerClient",
    "HttpEmbeddingClient",
    "HybridRetriever",
    "JinaRerankerClient",
    "KnowledgeService",
    "KnowledgeSourceNotFoundError",
    "PgVectorStore",
    "chunk_code",
    "chunk_file",
    "chunk_markdown",
    "classify_path",
    "full_sync",
    "fuse",
    "git_changed_files",
    "incremental_sync",
    "iter_source_files",
    "read_repo_files",
    "sync_source",
    "tokenize",
    "weight_for",
]
