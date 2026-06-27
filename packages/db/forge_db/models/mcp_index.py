"""MCP sync-and-index models (F20): per-resource ledger + per-run history.

F20 turns an MCP connection's ``index_strategy = sync_and_index`` into persisted
``retrieval_chunk`` rows. Two new tables back that pipeline:

* :class:`KnowledgeSyncRun` — one row per sync run (full or incremental) recording
  the reconciliation counters (resources/chunks seen/indexed/skipped/deleted),
  whether the tombstone sweep ran, timing, and any error. It generalises F05's
  intended ``knowledge_sync_runs`` table (which the foundation never shipped) and
  is reused by the generic sync path too.
* :class:`MCPIndexedResource` — the per-``(source, resource_uri)`` ledger that
  drives incremental change detection (server ``change_token`` else ``content_hash``),
  tombstoning of upstream-deleted resources, and stable provenance.

Both conform to the foundation conventions: singular ``snake_case`` table names,
``WorkspaceScopedModel`` (UUID PK + timestamps + ``workspace_id`` FK), and the
string-backed enums in :mod:`forge_db.models.enums`.

Deviations from the F20 slice doc (idealised schema vs. the real foundation),
noted deliberately:

* The slice named the FK columns ``source_id`` / ``connection_id``; the foundation
  consistently names FK columns ``<referent>_id``, so we use
  ``knowledge_source_id`` / ``mcp_connection_id`` to match ``retrieval_chunk`` etc.
* MCP connections live in an in-memory manager keyed by *slug* (the DB
  ``mcp_connection`` table is not populated by the control plane), so
  ``mcp_connection_id`` is a *nullable* FK and ``connection_slug`` is the stable,
  always-present link used everywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type
from forge_db.models.enums import RunStatus, SyncMode


class KnowledgeSyncRun(WorkspaceScopedModel):
    """One sync run over a knowledge source (F20 + generic sync history)."""

    __tablename__ = "knowledge_sync_run"
    __table_args__ = (Index("ix_knowledge_sync_run_source", "knowledge_source_id"),)

    knowledge_source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    mode: Mapped[SyncMode] = mapped_column(
        enum_type(SyncMode), default=SyncMode.INCREMENTAL, nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus), default=RunStatus.RUNNING, nullable=False
    )
    resources_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resources_indexed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resources_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resources_deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_indexed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sweep_skipped: Mapped[bool] = mapped_column(default=False, nullable=False)
    cap_hit: Mapped[bool] = mapped_column(default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPIndexedResource(WorkspaceScopedModel):
    """Per-resource sync ledger: change detection, tombstoning, provenance (F20)."""

    __tablename__ = "mcp_indexed_resource"
    __table_args__ = (
        Index(
            "ux_mcp_idx_resource",
            "knowledge_source_id",
            "resource_uri",
            unique=True,
        ),
        Index("ix_mcp_idx_tenant_source", "workspace_id", "knowledge_source_id"),
        Index("ix_mcp_idx_seen", "knowledge_source_id", "last_seen_sync_run_id"),
        CheckConstraint(
            "deleted_at IS NULL OR chunk_count = 0",
            name="tombstone_no_chunks",
        ),
    )

    knowledge_source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable FK: the foundation's MCP control plane keys connections by slug in
    # an in-memory manager, so a DB ``mcp_connection`` row may not exist.
    mcp_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("mcp_connection.id", ondelete="CASCADE"),
        nullable=True,
    )
    connection_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    change_token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_sync_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_sync_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Re-export for callers that read the CHECK constraint name in tests.
TOMBSTONE_CHECK = "ck_mcp_indexed_resource_tombstone_no_chunks"

__all__ = ["TOMBSTONE_CHECK", "KnowledgeSyncRun", "MCPIndexedResource"]
