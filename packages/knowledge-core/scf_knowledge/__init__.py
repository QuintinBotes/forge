"""Chunking, embeddings, hybrid retrieval, RRF fusion, and reranking."""

from __future__ import annotations

from scf_knowledge.chunking import (
    DEFAULT_MAX_CHARS,
    TREE_SITTER_LANGUAGES,
    chunk_code,
    chunk_file,
    chunk_markdown,
    classify_path,
    treesitter_available,
    weight_for,
)
from scf_knowledge.embeddings import (
    DEFAULT_SENTENCE_TRANSFORMERS_MODEL,
    DeterministicEmbeddingClient,
    HttpEmbeddingClient,
    SentenceTransformerEmbeddingClient,
    build_embedding_client,
    build_embedding_client_from_settings,
)
from scf_knowledge.fusion import fuse
from scf_knowledge.mcp_chunking import (
    McpResourceChunker,
    McpResourceSnapshot,
    provenance_uri,
)
from scf_knowledge.mcp_indexer import (
    LedgerRow,
    McpResourceFetcher,
    McpSyncIndexer,
    ResourceLedger,
    ResourceRef,
    SyncDirection,
    SyncReport,
    SyncRunRecorder,
)
from scf_knowledge.mcp_ledger import (
    SqlResourceLedger,
    SqlSyncRunRecorder,
    index_status_counts,
    latest_run,
    purge_index,
)
from scf_knowledge.mcp_retrieval import McpRetrievalRouter, retrieve_with_mcp
from scf_knowledge.redaction import redact_secrets
from scf_knowledge.reranker import (
    DEFAULT_RERANK_TIMEOUT_MS,
    FixtureRerankerClient,
    GracefulReranker,
    JinaRerankerClient,
    RerankerUnavailableError,
    RerankTelemetry,
    build_reranker,
    build_reranker_from_settings,
)
from scf_knowledge.retriever import HybridRetriever, RerankDebug
from scf_knowledge.service import KnowledgeService
from scf_knowledge.stores import (
    Bm25Store,
    KnowledgeSourceNotFoundError,
    PgVectorStore,
    VectorLegUnconfiguredError,
    bm25_score,
    rebuild_bm25_stats,
)
from scf_knowledge.sync import (
    ChangeSet,
    full_sync,
    git_changed_files,
    git_log,
    incremental_sync,
    iter_source_files,
    read_repo_files,
    sync_source,
    sync_tracker,
)
from scf_knowledge.text import ENGLISH_STOPWORDS, query_terms, tokenize

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_RERANK_TIMEOUT_MS",
    "DEFAULT_SENTENCE_TRANSFORMERS_MODEL",
    "ENGLISH_STOPWORDS",
    "TREE_SITTER_LANGUAGES",
    "Bm25Store",
    "ChangeSet",
    "DeterministicEmbeddingClient",
    "FixtureRerankerClient",
    "GracefulReranker",
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
    "RerankDebug",
    "RerankTelemetry",
    "RerankerUnavailableError",
    "ResourceLedger",
    "ResourceRef",
    "SentenceTransformerEmbeddingClient",
    "SqlResourceLedger",
    "SqlSyncRunRecorder",
    "SyncDirection",
    "SyncReport",
    "SyncRunRecorder",
    "VectorLegUnconfiguredError",
    "bm25_score",
    "build_embedding_client",
    "build_embedding_client_from_settings",
    "build_reranker",
    "build_reranker_from_settings",
    "chunk_code",
    "chunk_file",
    "chunk_markdown",
    "classify_path",
    "full_sync",
    "fuse",
    "git_changed_files",
    "git_log",
    "incremental_sync",
    "index_status_counts",
    "iter_source_files",
    "latest_run",
    "provenance_uri",
    "purge_index",
    "query_terms",
    "read_repo_files",
    "rebuild_bm25_stats",
    "redact_secrets",
    "retrieve_with_mcp",
    "sync_source",
    "sync_tracker",
    "tokenize",
    "treesitter_available",
    "weight_for",
]
