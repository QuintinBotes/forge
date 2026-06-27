"""Chunking, embeddings, hybrid retrieval, RRF fusion, and reranking."""

from __future__ import annotations

from forge_knowledge.chunking import (
    DEFAULT_MAX_CHARS,
    TREE_SITTER_LANGUAGES,
    chunk_code,
    chunk_file,
    chunk_markdown,
    classify_path,
    treesitter_available,
    weight_for,
)
from forge_knowledge.embeddings import (
    DeterministicEmbeddingClient,
    HttpEmbeddingClient,
)
from forge_knowledge.fusion import fuse
from forge_knowledge.mcp_chunking import (
    McpResourceChunker,
    McpResourceSnapshot,
    provenance_uri,
)
from forge_knowledge.mcp_indexer import (
    LedgerRow,
    McpResourceFetcher,
    McpSyncIndexer,
    ResourceLedger,
    ResourceRef,
    SyncDirection,
    SyncReport,
    SyncRunRecorder,
)
from forge_knowledge.mcp_ledger import (
    SqlResourceLedger,
    SqlSyncRunRecorder,
    index_status_counts,
    latest_run,
    purge_index,
)
from forge_knowledge.mcp_retrieval import McpRetrievalRouter, retrieve_with_mcp
from forge_knowledge.redaction import redact_secrets
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
    "TREE_SITTER_LANGUAGES",
    "Bm25Store",
    "ChangeSet",
    "DeterministicEmbeddingClient",
    "FixtureRerankerClient",
    "HttpEmbeddingClient",
    "HybridRetriever",
    "JinaRerankerClient",
    "KnowledgeService",
    "KnowledgeSourceNotFoundError",
    "LedgerRow",
    "McpResourceChunker",
    "McpResourceFetcher",
    "McpResourceSnapshot",
    "McpRetrievalRouter",
    "McpSyncIndexer",
    "PgVectorStore",
    "ResourceLedger",
    "ResourceRef",
    "SqlResourceLedger",
    "SqlSyncRunRecorder",
    "SyncDirection",
    "SyncReport",
    "SyncRunRecorder",
    "chunk_code",
    "chunk_file",
    "chunk_markdown",
    "classify_path",
    "full_sync",
    "fuse",
    "git_changed_files",
    "incremental_sync",
    "index_status_counts",
    "iter_source_files",
    "latest_run",
    "provenance_uri",
    "purge_index",
    "read_repo_files",
    "redact_secrets",
    "retrieve_with_mcp",
    "sync_source",
    "tokenize",
    "treesitter_available",
    "weight_for",
]
