"""Knowledge / retrieval models: KnowledgeSource, RetrievalChunk.

``RetrievalChunk`` carries the hybrid-retrieval columns:
- ``embedding`` — pgvector ``Vector(EMBEDDING_DIM)`` (degrades to JSON on SQLite),
- ``tsv`` — Postgres ``tsvector`` for BM25/full-text (degrades to TEXT on SQLite).

The Postgres-only types are guarded via ``with_variant`` so the full metadata
still creates on SQLite for fast unit tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import ChunkType, KnowledgeSourceKind, SyncMode

if TYPE_CHECKING:
    from forge_db.models.workspace import Workspace

# Embedding dimensionality. 1536 is a common default (e.g. OpenAI
# text-embedding-3-small); the knowledge-core tasks consume this constant.
EMBEDDING_DIM = 1536

# Chunk-type priority weights (spec: Chunk Types and Priority Weights table).
CHUNK_TYPE_WEIGHTS: dict[ChunkType, float] = {
    ChunkType.MARKDOWN: 1.0,
    ChunkType.CODE: 1.0,
    ChunkType.SUMMARY: 1.2,
    ChunkType.README: 1.3,
    ChunkType.SPEC: 1.4,
    ChunkType.POLICY: 1.5,
    ChunkType.MCP_RESOURCE: 1.0,
}


class KnowledgeSource(WorkspaceScopedModel):
    """An indexed knowledge source (repo, MCP server, document, URL)."""

    __tablename__ = "knowledge_source"

    kind: Mapped[KnowledgeSourceKind] = mapped_column(
        enum_type(KnowledgeSourceKind), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    repository_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("repository_connection.id", ondelete="CASCADE"),
        nullable=True,
    )
    mcp_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("mcp_connection.id", ondelete="CASCADE"),
        nullable=True,
    )
    sync_mode: Mapped[SyncMode] = mapped_column(
        enum_type(SyncMode), default=SyncMode.FULL, nullable=False
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    freshness_sla_minutes: Mapped[int | None] = mapped_column(nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="knowledge_sources")
    chunks: Mapped[list[RetrievalChunk]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class RetrievalChunk(WorkspaceScopedModel):
    """An indexed, embedded, attributed chunk for hybrid retrieval."""

    __tablename__ = "retrieval_chunk"
    __table_args__ = (
        Index("ix_retrieval_chunk_source", "knowledge_source_id"),
        # GIN over the tsvector on Postgres (a plain index elsewhere).
        Index("ix_retrieval_chunk_tsv", "tsv", postgresql_using="gin"),
    )

    knowledge_source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_type: Mapped[ChunkType] = mapped_column(enum_type(ChunkType), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    start_line: Mapped[int | None] = mapped_column(nullable=True)
    end_line: Mapped[int | None] = mapped_column(nullable=True)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", json_type(), default=dict, nullable=False
    )
    # ``none_as_null`` on SQLite: without it Python ``None`` is stored as the
    # JSON literal ``'null'``, which passes ``isnot(None)`` and resurrected
    # unembedded rows as score-0.0 vector hits (dialect disagreement). No DDL
    # difference — the column type is unchanged.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM).with_variant(JSON(none_as_null=True), "sqlite"),
        nullable=True,
    )
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR().with_variant(Text(), "sqlite"), nullable=True
    )
    # Token length under ``forge_knowledge.text.tokenize``, maintained by the
    # store in the same transaction as the chunk write (spec J §2.5, ADR-0017).
    # Feeds exact Okapi BM25 document-length normalisation.
    token_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    source: Mapped[KnowledgeSource] = relationship(back_populates="chunks")


class RetrievalTermStat(WorkspaceScopedModel):
    """Per-workspace document frequency for one term (BM25 IDF input)."""

    __tablename__ = "retrieval_term_stats"
    __table_args__ = (
        UniqueConstraint("workspace_id", "term", name="uq_retrieval_term_stats_workspace_term"),
    )

    term: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    df: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RetrievalWorkspaceStat(WorkspaceScopedModel):
    """Per-workspace corpus totals for BM25 (``N`` and average document length)."""

    __tablename__ = "retrieval_workspace_stats"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_retrieval_workspace_stats_workspace"),
    )

    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
