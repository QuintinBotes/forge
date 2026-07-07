"""F38 cost ledger models: ``cost_event`` (durable token/cost log) + ``model_price``.

Conformance notes (foundation vs the F38 slice doc):

* Singular table names and the shared string-backed ``enum_type`` convention
  (``kind`` is a :class:`CostEventKind`, not free text).
* FK targets are the REAL tables — ``workspace``/``project``/``task``/
  ``workflow_run``/``agent_run`` (the doc's plural names don't exist in-tree).
* There is no ``agent_steps`` table in the foundation (F06 stores steps as JSON
  on ``agent_run.steps``), so ``step_id`` is a plain nullable UUID correlation
  column (the step's id inside that JSON), not a DB-level FK.
* Non-workspace FKs use ``SET NULL`` — the ledger is a billing record and must
  survive project/task/run deletion; only workspace deletion cascades.

Append-only: rows are inserted once (idempotent on the unique
``(workspace_id, request_id)`` index); the only mutation path is the audited
reprice, which updates ``cost_usd``/``price_id`` alone. Enforced in the
repository layer (``forge_obs.cost``), not by trigger, so reprice stays a plain
UPDATE.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import ForgeModel, WorkspaceScopedModel, enum_type
from forge_db.models.enums import CostEventKind

__all__ = ["CostEvent", "ModelPrice"]


class ModelPrice(ForgeModel):
    """BYOK price book: global defaults (``workspace_id IS NULL``) + overrides.

    Effective-dated: resolution picks the newest ``effective_from <= occurred_at``,
    preferring a workspace override over the global row (F38 AC4).
    """

    __tablename__ = "model_price"
    __table_args__ = (
        Index(
            "ix_model_price_lookup",
            "provider",
            "model",
            "kind",
            "effective_from",
        ),
        Index(
            "ix_model_price_workspace_lookup",
            "workspace_id",
            "provider",
            "model",
            "kind",
            "effective_from",
        ),
    )

    # Nullable tenant column (NULL = global default), so not WorkspaceScopedModel.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[CostEventKind] = mapped_column(
        enum_type(CostEventKind), default=CostEventKind.COMPLETION, nullable=False
    )
    prompt_usd_per_1k: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), default=Decimal(0), server_default=text("0"), nullable=False
    )
    completion_usd_per_1k: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), default=Decimal(0), server_default=text("0"), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(8), default="USD", server_default=text("'USD'"), nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )


class CostEvent(WorkspaceScopedModel):
    """Durable system of record for token cost (the spec's "token/cost logs")."""

    __tablename__ = "cost_event"
    __table_args__ = (
        # Idempotent emission: a retried record is a no-op (no double-billing).
        Index("uq_cost_event_request", "workspace_id", "request_id", unique=True),
        Index("ix_cost_event_workspace_time", "workspace_id", "occurred_at"),
        Index("ix_cost_event_task", "task_id"),
        Index("ix_cost_event_workflow_run", "workflow_run_id"),
        Index(
            "ix_cost_event_provider_time",
            "workspace_id",
            "provider",
            "model",
            "occurred_at",
        ),
        Index("ix_cost_event_phase_time", "workspace_id", "phase", "occurred_at"),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_run.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )
    # Correlates to the step id inside agent_run.steps JSON (no step table exists).
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[CostEventKind] = mapped_column(enum_type(CostEventKind), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        Computed("prompt_tokens + completion_tokens", persisted=True),
        nullable=True,
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), nullable=False)
    price_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_price.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
