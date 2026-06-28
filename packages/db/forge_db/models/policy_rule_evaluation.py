"""F29 — the append-only ``policy_rule_evaluation`` audit table.

One immutable row is written per conditional policy evaluation that matched at
least one rule (flat-only repos write none). It records the redacted context,
the flat F04 ``base_effect``, the composed ``final_effect``, and the ordered
``matched_rule_ids`` — the rich, queryable per-decision audit feeding the F36
approval UI's "Risks flagged" panel and the F10 run trace.

Append-only at two layers: the service exposes no UPDATE/DELETE path, and the
table opts into the reusable F39 :func:`attach_immutability_trigger` Postgres
trigger (DB-level rejection of UPDATE/DELETE; a no-op on the SQLite unit path).

Foundation deviations (conforming to the real model — see the slice notes): the
F04 ``repo_policy_snapshot`` and F07/F10 ``step`` tables do not exist in this
foundation, so ``policy_snapshot_id`` / ``step_id`` are nullable UUIDs without an
FK (mirroring ``AgentRepoWorkspace.policy_snapshot_id``); ``agent_run_id`` keeps
its real FK to ``agent_run``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type


class PolicyRuleEvaluation(WorkspaceScopedModel):
    """An immutable record of one conditional policy evaluation (F29)."""

    __tablename__ = "policy_rule_evaluation"
    __table_args__ = (
        Index("ix_policy_rule_evaluation_agent_run_id", "agent_run_id"),
        Index("ix_policy_rule_evaluation_workspace_evaluated", "workspace_id", "evaluated_at"),
    )

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=True
    )
    # No ``step`` / ``repo_policy_snapshot`` tables in this foundation -> no FK.
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    policy_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    action: Mapped[str] = mapped_column(Text, nullable=False)
    base_effect: Mapped[str] = mapped_column(String(32), nullable=False)
    final_effect: Mapped[str] = mapped_column(String(32), nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    matched_rule_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    context_redacted: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Harden the audit table DB-side (Postgres BEFORE UPDATE/DELETE block; no-op on
# SQLite, where the repository exposes no update/delete path).
attach_immutability_trigger(PolicyRuleEvaluation.__table__)


__all__ = ["PolicyRuleEvaluation"]
