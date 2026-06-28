"""Immutable platform audit log: ``audit_log`` (the F39 sliver F30 needs).

A shared append-only record of security-relevant changes (F30 writes every
grant/team/membership/visibility change here). Hardened DB-side on Postgres via
:func:`attach_immutability_trigger` (BEFORE UPDATE/DELETE -> raise), mirroring
``automation_execution`` / ``policy_rule_evaluation``. The permanent
authorization history lives here, not in the mutable ``role_grant`` table.

Foundation note: neither the F39 ``audit_log`` table nor the ``AuditEvent`` /
``AuditSink`` contract exist in-tree, so F30 introduces the minimal general
pieces it needs (a full query/retention surface is out of scope).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type


class AuditLog(WorkspaceScopedModel):
    """A single immutable audit record."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_workspace_created", "workspace_id", "created_at"),
        Index("ix_audit_log_action", "workspace_id", "action"),
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


# Make the table append-only on Postgres (no-op on SQLite unit tests).
attach_immutability_trigger(AuditLog.__table__)
