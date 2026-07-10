"""F40 PM-depth models: per-member sprint capacity, estimation scales, and the
append-only estimate/status history logs that back cycle/lead time + CFD.

Four tables, all additive alongside F26's sprint-velocity trio
(``sprint_velocity.py``) and F01's ``sprint``/``task``:

* ``sprint_member_capacity`` — a member's declared point/day capacity for one
  sprint (mutable planning input; the capacity report is computed live from
  this + current task assignments, never persisted).
* ``estimation_scale`` — a named, ordered set of allowed estimate values,
  scoped to a project or (``project_id is None``) the whole workspace.
* ``task_estimate_event`` — append-only estimate-change history, recorded on
  *every* estimate edit (unlike the F26 ``ESTIMATE_CHANGED`` scope event, which
  only fires while the task's sprint is active).
* ``task_status_event`` — append-only task status-transition log powering the
  portfolio Cumulative Flow Diagram and cycle/lead-time rollups.

None of these four tables are read by the F26 velocity/burndown rollups, so
they cannot alter — let alone break — the ``sprint_scope_event`` log those
depend on. The two event logs are hardened DB-side via the same Postgres
immutability trigger as ``sprint_scope_event`` / ``automation_execution``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type


class SprintMemberCapacity(WorkspaceScopedModel):
    """A member's declared point/day capacity for one sprint."""

    __tablename__ = "sprint_member_capacity"
    __table_args__ = (
        UniqueConstraint("sprint_id", "member_id", name="uq_sprint_member_capacity_sprint_member"),
    )

    sprint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sprint.id", ondelete="CASCADE"), nullable=False
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    capacity_points: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class EstimationScale(WorkspaceScopedModel):
    """A configurable estimation scale (Fibonacci points, ideal days, ...).

    ``project_id is None`` is the workspace-wide default scale.
    """

    __tablename__ = "estimation_scale"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "project_id", "name", name="uq_estimation_scale_ws_project_name"
        ),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), default="points", nullable=False)
    values: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TaskEstimateEvent(WorkspaceScopedModel):
    """Append-only estimate-change history for a task (any sprint state)."""

    __tablename__ = "task_estimate_event"
    __table_args__ = (Index("ix_task_estimate_event_task_changed", "task_id", "changed_at"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="CASCADE"), nullable=False
    )
    sprint_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sprint.id", ondelete="SET NULL"), nullable=True
    )
    points_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskStatusEvent(WorkspaceScopedModel):
    """Append-only task status-transition log (portfolio CFD + cycle/lead time)."""

    __tablename__ = "task_status_event"
    __table_args__ = (Index("ix_task_status_event_task_changed", "task_id", "changed_at"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    sprint_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sprint.id", ondelete="SET NULL"), nullable=True
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Harden the two append-only logs DB-side (Postgres BEFORE UPDATE/DELETE block;
# no-op on SQLite, where the service exposes no update/delete path).
attach_immutability_trigger(TaskEstimateEvent.__table__)
attach_immutability_trigger(TaskStatusEvent.__table__)


__all__ = [
    "EstimationScale",
    "SprintMemberCapacity",
    "TaskEstimateEvent",
    "TaskStatusEvent",
]
