"""F36 — Human Approval System child tables.

``approval_decision`` is the tamper-evident per-approver decision trail
(append-only via the reusable F39 :func:`attach_immutability_trigger`, exactly
like ``policy_rule_evaluation`` / ``deployment_approval``); the gate's derived
``approval_request.status`` remains on the parent row. ``policy_override_grant``
backs the ``policy_override`` resolution hook: a single-use, short-TTL
permission bound to one exact action fingerprint — it never broadens scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger

if TYPE_CHECKING:
    from forge_db.models.runs import ApprovalRequest


class ApprovalDecision(WorkspaceScopedModel):
    """One immutable reviewer vote on an approval gate (F36).

    Unique per ``(approval_request_id, approver_user_id)`` — one vote per
    approver; structurally supports ``required_approvals > 1`` aggregation
    (a V2 fast-follow; V1 resolves on a single decisive vote).
    """

    __tablename__ = "approval_decision"
    __table_args__ = (
        Index(
            "uq_approval_decision_approver",
            "approval_request_id",
            "approver_user_id",
            unique=True,
        ),
    )

    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("approval_request.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approver_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    approval_request: Mapped[ApprovalRequest] = relationship(back_populates="decisions")


class PolicyOverrideGrant(WorkspaceScopedModel):
    """A single-use policy-override permission for one exact tool call (F36 J5)."""

    __tablename__ = "policy_override_grant"
    __table_args__ = (
        # At most one ACTIVE (unconsumed) grant per (agent_run, fingerprint).
        Index(
            "uq_active_override",
            "agent_run_id",
            "action_fingerprint",
            unique=True,
            postgresql_where=text("consumed = false"),
            sqlite_where=text("consumed = 0"),
        ),
    )

    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("approval_request.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    consumed: Mapped[bool] = mapped_column(
        Boolean(), default=False, server_default=text("false"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Harden the decision trail DB-side (Postgres BEFORE UPDATE/DELETE block; no-op
# on SQLite, where the repository exposes no update/delete path).
attach_immutability_trigger(ApprovalDecision.__table__)


__all__ = ["ApprovalDecision", "PolicyOverrideGrant"]
