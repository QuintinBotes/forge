"""Durable backing table for the observability audit store (audit-store persist).

The API's :class:`forge_api.observability.audit.InMemoryAuditStore` keeps a
*global*, append-only, tamper-evident hash chain of :class:`AuditEntry` records
(``category`` / ``actor`` / ``run_id`` / ``connection_id`` / ``workspace_id`` /
``seq`` / ``prev_hash`` / ``entry_hash``). This module is the Postgres backing
for the *db* variant of that store: one row per appended entry plus a single
global cursor row that serializes appends and hands out the next ``seq`` /
``prev_hash``.

Why a **new** table rather than reusing F39's ``audit_log``: the two are
genuinely different sinks. F39's ``audit_log`` is a *per-workspace* chain of
``AuditEvent`` rows (``workspace_id`` NOT NULL + FK, ``actor_id`` a real
``app_user`` FK, 1-based per-workspace ``seq``, and an ``entry_hash`` computed
over the F39 field tuple). The observability ``AuditEntry`` is a *global* chain
(0-based ``seq`` spanning every workspace), its ``workspace_id`` is optional and
carries no FK, its ``actor`` is a free-form label string, and its ``entry_hash``
is the SHA-256 of the whole redacted entry model. Forcing one onto the other
would either weaken F39's constraints or change the observability store's
behaviour — so the observability chain gets its own table and keeps byte-for-byte
parity with the in-memory store (the repository re-uses that store's own
``_hash_entry`` helper).

The entry's logical ``timestamp`` is stored in ``occurred_at`` (timezone-aware);
its ``metadata`` dict lands in JSONB. ``category`` / ``actor`` / ``target`` /
``connection_id`` / ``status`` / ``detail`` are stored in unbounded ``Text`` so
no value is ever truncated (a truncated value would break the hash chain). The
mapping to/from :class:`AuditCategory` lives in the API repository — ``forge_db``
never imports ``forge_api``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import ForgeModel, json_type

#: ``prev_hash`` seed of the global chain (mirrors the in-memory store's genesis).
GENESIS_HASH = "0" * 64

#: Fixed singleton id of the single global chain-cursor row.
CHAIN_HEAD_ID = uuid.UUID("00000000-0000-0000-0000-0000000a0d17")


class ObservabilityAuditEntry(ForgeModel):
    """One persisted row of the global observability audit hash chain.

    Not workspace-scoped: ``workspace_id`` is an optional, un-constrained tag
    (the in-memory store holds free-floating UUIDs), so this uses the plain
    :class:`ForgeModel` (surrogate UUID PK + timestamps) rather than the
    tenant-scoped base. ``created_at`` / ``updated_at`` are the DB insert stamps;
    ``occurred_at`` is the entry's own logical timestamp.
    """

    __tablename__ = "observability_audit_entry"
    __table_args__ = (
        # Global chain integrity: gap-free, unique, monotonic position.
        Index("uq_observability_audit_entry_seq", "seq", unique=True),
        Index("ix_observability_audit_entry_category", "category"),
        Index("ix_observability_audit_entry_actor", "actor"),
        Index("ix_observability_audit_entry_run_id", "run_id"),
        Index("ix_observability_audit_entry_connection_id", "connection_id"),
        Index("ix_observability_audit_entry_workspace_ref", "workspace_ref"),
    )

    #: The :class:`AuditEntry.id` (its own UUID; distinct from the surrogate PK).
    entry_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: Global 0-based chain position (unique).
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The entry's logical timestamp (aware; the in-memory ``AuditEntry.timestamp``).
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: ``AuditCategory`` value (stored as text; mapped in the API repository).
    category: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Optional, un-constrained tenant tag — a free-floating UUID like the
    #: in-memory store holds (accepts any workspace id, existent or not). Named
    #: ``workspace_ref`` rather than ``workspace_id`` precisely because it is NOT
    #: the tenant FK the house ``workspace_id`` invariant mandates; the repository
    #: maps it to/from :attr:`AuditEntry.workspace_id`.
    workspace_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    connection_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Redacted metadata dict (JSONB); ``metadata`` is reserved on the declarative
    #: base, so the attribute + column are named ``entry_metadata``.
    entry_metadata: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    redacted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: ``entry_hash`` of the predecessor (``GENESIS_HASH`` for ``seq`` 0).
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 hex over the canonical redacted entry (in-memory ``_hash_entry``).
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ObservabilityAuditChainHead(ForgeModel):
    """The single global chain cursor — the only *mutable* observability-audit row.

    Locked ``FOR UPDATE`` by the repository to serialize appends and hand out the
    next ``seq`` / ``prev_hash``. Exactly one row exists, keyed by the fixed
    :data:`CHAIN_HEAD_ID` sentinel; ``last_seq`` starts at ``-1`` so the first
    append is ``seq`` 0 (parity with the in-memory ``len(entries)`` scheme).
    """

    __tablename__ = "observability_audit_chain_head"

    last_seq: Mapped[int] = mapped_column(BigInteger, default=-1, nullable=False)
    last_hash: Mapped[str] = mapped_column(String(64), default=GENESIS_HASH, nullable=False)


__all__ = [
    "CHAIN_HEAD_ID",
    "GENESIS_HASH",
    "ObservabilityAuditChainHead",
    "ObservabilityAuditEntry",
]
