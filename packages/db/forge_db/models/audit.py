"""Immutable platform audit log: ``audit_log`` + ``audit_chain_head`` (F39).

A shared append-only record of security-relevant changes. F30 originated the
``audit_log`` table (grant/team/membership/visibility changes); F39 extends it
in place with the tamper-evident per-workspace hash chain (``seq`` /
``payload_hash`` / ``prev_hash`` / ``entry_hash``) plus the observability
columns (``actor_label`` / ``severity`` / ``reason`` / ``detail_ref`` /
``request_id``) and adds ``audit_chain_head`` — the per-workspace chain cursor
that serializes appends (the only mutable audit table).

Immutability is defense-in-depth:

1. Postgres: :func:`~forge_db.base.attach_immutability_trigger` (BEFORE
   UPDATE/DELETE -> raise), as F30 already attached.
2. Every dialect: ORM-level guards below reject ``UPDATE``/``DELETE`` of
   ``AuditLog`` instances at flush time (SQLite unit tests included).
3. The hash chain makes any out-of-band mutation/deletion *detectable* via
   ``forge_db.audit.chain.verify_chain`` even if 1-2 are bypassed.

Chain columns are nullable: pre-F39 rows are backfilled by the
``0022_f39_audit_chain`` migration, and the chain verifier walks only chained
rows (a deviation from the slice doc's NOT NULL columns, keeping the in-place
extension of F30's populated table safe).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type

#: ``prev_hash`` seed of every per-workspace chain (mirrors forge_contracts.audit).
GENESIS_HASH = "0" * 64


class AuditLogImmutableError(RuntimeError):
    """Raised when code attempts to UPDATE or DELETE an ``audit_log`` row."""


class AuditLog(WorkspaceScopedModel):
    """A single immutable audit record (chained per workspace)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_workspace_created", "workspace_id", "created_at"),
        Index("ix_audit_log_action", "workspace_id", "action"),
        # Chain ordering integrity + idempotent appends (unique per workspace;
        # NULL seq rows — pre-backfill legacy only — are exempt on every dialect).
        Index("uq_audit_log_workspace_seq", "workspace_id", "seq", unique=True),
        Index("ix_audit_log_actor", "workspace_id", "actor_id"),
        Index("ix_audit_log_target", "workspace_id", "target_type", "target_id"),
    )

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="success", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)

    # --- F39 chain + observability columns ---------------------------------- #
    #: Monotonic per-workspace chain position (1-based; NULL = legacy/unchained).
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Durable actor snapshot, e.g. ``user:alice@acme`` — survives user deletion.
    actor_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: ``info | notice | warning | critical`` (drives alerting/fail-closed).
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    #: Short, redacted human reason (matched policy rule, error class, ...).
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: sha256 hex of the canonical redacted payload ({before, after, details}).
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ``entry_hash`` of the previous chained row (GENESIS_HASH for seq 1).
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: sha256 hex over the canonical entry tuple (see forge_contracts.audit).
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ``{"table": ..., "id": ...}`` pointer into the per-domain detail row.
    detail_ref: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    #: Correlation id (HTTP request / Celery task id).
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditChainHead(WorkspaceScopedModel):
    """Per-workspace chain cursor — the only *mutable* audit table.

    Locked ``FOR UPDATE`` by the writer to serialize appends and hand out
    ``seq``/``prev_hash``. One row per workspace (unique ``workspace_id``);
    carries a surrogate UUID ``id`` to conform to the house model substrate
    (single ``id`` PK everywhere — deviation from the slice doc's
    ``workspace_id`` PK, noted).
    """

    __tablename__ = "audit_chain_head"
    __table_args__ = (Index("uq_audit_chain_head_workspace", "workspace_id", unique=True),)

    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_hash: Mapped[str] = mapped_column(String(64), default=GENESIS_HASH, nullable=False)


# Postgres: append-only via the shared BEFORE UPDATE/DELETE trigger (F30-era).
attach_immutability_trigger(AuditLog.__table__)


# Every dialect (AC7): the ORM refuses to flush an UPDATE or DELETE of an
# ``audit_log`` row — repositories expose no mutation path, and this guard
# turns an accidental one into a hard error on SQLite too (where no trigger
# exists). Out-of-band Core/raw-SQL tampering bypasses this by design; the
# hash chain exists to *detect* exactly that.
@event.listens_for(AuditLog, "before_update")
def _audit_log_block_update(_mapper: object, _connection: object, target: AuditLog) -> None:
    raise AuditLogImmutableError(f"audit_log is append-only: refusing to update row {target.id}")


@event.listens_for(AuditLog, "before_delete")
def _audit_log_block_delete(_mapper: object, _connection: object, target: AuditLog) -> None:
    raise AuditLogImmutableError(f"audit_log is append-only: refusing to delete row {target.id}")
