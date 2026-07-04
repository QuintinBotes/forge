"""F26 sprint-velocity models.

Three tables extend F01's ``sprint``:

* ``sprint_scope_event`` — the append-only log that is the source of truth for
  all velocity/burndown reconstruction. Hardened DB-side on Postgres via
  :func:`attach_immutability_trigger` (BEFORE UPDATE/DELETE block), mirroring
  ``automation_execution``.
* ``sprint_burndown_snapshot`` — derived per-sprint-per-day time series; idempotent
  daily upsert keyed by ``(sprint_id, snapshot_date)``.
* ``sprint_velocity`` — derived per-sprint rollup powering the dashboard in one scan.

``sprint_burndown_snapshot`` + ``sprint_velocity`` are **derived state**: droppable
and fully rebuildable from ``sprint_scope_event`` + current task rows by the
``sprint.reconcile_sprint`` task. Nothing reads them as a source of truth.

Foundation deviation (noted in the slice report): the idealized slice gave
``sprint_velocity`` a ``sprint_id`` PK. The foundation convention (enforced by
``test_models.py``) is a UUID ``id`` PK on every model, so velocity uses an ``id``
PK plus a UNIQUE on ``sprint_id`` (one rollup row per sprint, same guarantee).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, enum_type
from forge_db.models.enums import ScopeActorKind, SprintScopeEventType


class SprintScopeEvent(WorkspaceScopedModel):
    """Append-only record of a single sprint scope change."""

    __tablename__ = "sprint_scope_event"
    __table_args__ = (Index("ix_sprint_scope_event_sprint_occurred", "sprint_id", "occurred_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    sprint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sprint.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[SprintScopeEventType] = mapped_column(
        enum_type(SprintScopeEventType), nullable=False
    )
    points_delta: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    points_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope_points_after: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_points_after: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_kind: Mapped[ScopeActorKind] = mapped_column(
        enum_type(ScopeActorKind), default=ScopeActorKind.SYSTEM, nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SprintBurndownSnapshot(WorkspaceScopedModel):
    """One derived burndown point per sprint per calendar day."""

    __tablename__ = "sprint_burndown_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "sprint_id",
            "snapshot_date",
            name="uq_sprint_burndown_snapshot_sprint_id_snapshot_date",
        ),
        Index("ix_sprint_burndown_snapshot_sprint_date", "sprint_id", "snapshot_date"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    sprint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sprint.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    scope_points: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_points: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_points: Mapped[int] = mapped_column(Integer, nullable=False)
    ideal_points: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    completed_task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_task_count: Mapped[int] = mapped_column(Integer, nullable=False)


class SprintVelocity(WorkspaceScopedModel):
    """Derived per-sprint velocity rollup (one row per sprint)."""

    __tablename__ = "sprint_velocity"
    __table_args__ = (
        UniqueConstraint("sprint_id", name="uq_sprint_velocity_sprint_id"),
        Index("ix_sprint_velocity_project", "project_id"),
    )

    sprint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sprint.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    committed_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    added_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    removed_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    carryover_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    committed_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    carryover_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    predictability: Mapped[float] = mapped_column(Numeric(5, 4), default=0, nullable=False)
    scope_change_ratio: Mapped[float] = mapped_column(Numeric(5, 4), default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Harden the scope-event log DB-side (Postgres BEFORE UPDATE/DELETE block; no-op
# on SQLite, where the service exposes no update/delete path).
attach_immutability_trigger(SprintScopeEvent.__table__)


__all__ = [
    "SprintBurndownSnapshot",
    "SprintScopeEvent",
    "SprintVelocity",
]
