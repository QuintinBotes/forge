"""Planning models: Epic, SpecDocument, Task, Sprint, Milestone, Incident."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import (
    ExecutionMode,
    IncidentSeverity,
    IncidentState,
    Priority,
    SpecStatus,
    TaskKind,
    TaskStatus,
)
from forge_db.models.incidents import open_incident_dedup_index

if TYPE_CHECKING:
    from forge_db.models.project import Project
    from forge_db.models.runs import WorkflowRun


class Epic(WorkspaceScopedModel):
    """A unit of work grouping a spec document and its tasks."""

    __tablename__ = "epic"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    priority: Mapped[Priority] = mapped_column(
        enum_type(Priority), default=Priority.MEDIUM, nullable=False
    )
    # F01 board persistence: carries the ``EpicDTO`` free-form fields the domain
    # service round-trips but the base Epic has no dedicated column for. ``spec_id``
    # is a plain UUID mirror of the DTO value (the referential 1:1 link lives on
    # ``spec_document.epic_id``); ``labels`` is the DTO's saved-filter label set.
    spec_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    labels: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)

    project: Mapped[Project] = relationship(back_populates="epics")
    spec_document: Mapped[SpecDocument | None] = relationship(
        back_populates="epic", cascade="all, delete-orphan", uselist=False
    )
    tasks: Mapped[list[Task]] = relationship(back_populates="epic")


class SpecDocument(WorkspaceScopedModel):
    """A spec-driven-development document (spec: Spec Manifest Schema)."""

    __tablename__ = "spec_document"
    __table_args__ = (UniqueConstraint("workspace_id", "spec_key"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    epic_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("epic.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    spec_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[SpecStatus] = mapped_column(
        enum_type(SpecStatus), default=SpecStatus.DRAFT, nullable=False
    )
    requirements: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    acceptance_criteria: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    open_questions: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    constraints: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    constitution_refs: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    repos: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    decisions: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    plan_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tasks_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    validation_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        enum_type(ExecutionMode), default=ExecutionMode.SINGLE_AGENT, nullable=False
    )
    skill_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)

    epic: Mapped[Epic | None] = relationship(back_populates="spec_document")
    tasks: Mapped[list[Task]] = relationship(back_populates="spec")


class Sprint(WorkspaceScopedModel):
    """A time-boxed iteration.

    F26 (sprint-velocity) extends F01's basic sprint with lifecycle/velocity
    fields: the ``committed_*`` snapshot frozen at start, lifecycle timestamps,
    ``capacity_points`` (planning only), a lexorank ``position``, a monotonic
    ``velocity_version`` bumped on each rollup refresh, and ``committed_task_ids``
    (the per-task committed-scope snapshot used to reconstruct velocity). The
    partial unique index ``uq_active_sprint_per_project`` enforces at most one
    ``active`` sprint per project (fail-closed at the DB, not only the service).
    """

    __tablename__ = "sprint"
    __table_args__ = (
        Index(
            "uq_active_sprint_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    # F26 lifecycle + velocity columns (additive; existing rows stay valid).
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capacity_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    committed_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    committed_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    committed_task_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    position: Mapped[str | None] = mapped_column(Text, nullable=True)
    velocity_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # F01 board persistence: the ``SprintDTO.task_ids`` membership list the domain
    # service round-trips verbatim (distinct from the F26 ``committed_task_ids``
    # velocity snapshot frozen at sprint start).
    task_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    # F40 PM-depth: the working-day/holiday calendar the burndown ideal-line
    # reads (``forge_board.velocity.WorkCalendar``). ``calendar_weekend_days``
    # is a denylist of ISO weekdays (Monday=0..Sunday=6) treated as non-working;
    # ``calendar_holidays`` is a list of ISO date strings. Both default to an
    # empty list, which is byte-identical to the pre-F40 calendar-free ideal
    # line (every day counts as a working day).
    calendar_weekend_days: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    calendar_holidays: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)

    project: Mapped[Project] = relationship(back_populates="sprints")
    tasks: Mapped[list[Task]] = relationship(back_populates="sprint")


class Milestone(WorkspaceScopedModel):
    """A delivery milestone."""

    __tablename__ = "milestone"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)

    project: Mapped[Project] = relationship(back_populates="milestones")
    tasks: Mapped[list[Task]] = relationship(back_populates="milestone")


class Task(WorkspaceScopedModel):
    """The central unit of agent work (spec: Task Schema)."""

    __tablename__ = "task"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    epic_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("epic.id", ondelete="SET NULL"), nullable=True
    )
    spec_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("spec_document.id", ondelete="SET NULL"),
        nullable=True,
    )
    sprint_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sprint.id", ondelete="SET NULL"), nullable=True
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("milestone.id", ondelete="SET NULL"),
        nullable=True,
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[TaskKind] = mapped_column(
        enum_type(TaskKind), default=TaskKind.FEATURE, nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        enum_type(TaskStatus), default=TaskStatus.BACKLOG, nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        enum_type(Priority), default=Priority.MEDIUM, nullable=False
    )
    estimate: Mapped[int | None] = mapped_column(nullable=True)
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        enum_type(ExecutionMode), default=ExecutionMode.SINGLE_AGENT, nullable=False
    )
    repo_targets: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    instructions_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    skill_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    allowed_actions: Mapped[list[str]] = mapped_column(json_type(), default=list, nullable=False)
    restricted_actions: Mapped[list[str]] = mapped_column(json_type(), default=list, nullable=False)
    requires_approval: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    # ``None`` round-trips faithfully: JSON columns store Python ``None`` as the
    # JSON literal ``null`` (``none_as_null`` defaults to False), a non-SQL-NULL
    # value that satisfies ``nullable=False`` and reads back as ``None`` — distinct
    # from an all-defaults empty object. The Mapped type therefore includes None.
    knowledge_scope: Mapped[dict[str, Any] | None] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    subagent_policy: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    handoff_rules: Mapped[dict[str, Any] | None] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    acceptance_criteria: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    # F01 board persistence: the ``TaskDTO.labels`` saved-filter set (the DTO's
    # ``depends_on`` edges live in the ``task_dependency`` adjacency table below).
    labels: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)

    project: Mapped[Project] = relationship(back_populates="tasks")
    epic: Mapped[Epic | None] = relationship(back_populates="tasks")
    spec: Mapped[SpecDocument | None] = relationship(back_populates="tasks")
    sprint: Mapped[Sprint | None] = relationship(back_populates="tasks")
    milestone: Mapped[Milestone | None] = relationship(back_populates="tasks")
    workflow_runs: Mapped[list[WorkflowRun]] = relationship(back_populates="task")


class Incident(WorkspaceScopedModel):
    """An operational incident (spec: Incident Workflow States).

    F17 extends this with the incident-workflow lifecycle fields: ``source`` /
    ``dedup_key`` (alert intake + dedup), ``lifecycle_state`` (the FSM-state
    mirror spanning the forward + error/terminal states), ``commander_id``,
    ``impact_summary``, ``acknowledged_at``, and ``postmortem_id``.
    """

    __tablename__ = "incident"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key"),
        open_incident_dedup_index(),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[IncidentSeverity] = mapped_column(
        enum_type(IncidentSeverity), default=IncidentSeverity.MEDIUM, nullable=False
    )
    state: Mapped[IncidentState] = mapped_column(
        enum_type(IncidentState), default=IncidentState.ALERT_RECEIVED, nullable=False
    )
    # FSM-state mirror (free string: spans forward + error/terminal states).
    lifecycle_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    commander_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    blast_radius: Mapped[str | None] = mapped_column(String(32), nullable=True)
    impact_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    runbook: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    postmortem: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FK to postmortem.id is intentionally omitted (would create an
    # incident<->postmortem table cycle that SQLite create_all cannot resolve).
    postmortem_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    detected_at: Mapped[datetime | None] = mapped_column(nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    project: Mapped[Project] = relationship(back_populates="incidents")


class TaskDependency(WorkspaceScopedModel):
    """A directed task-dependency edge (``TaskDTO.depends_on``).

    An edge ``(task_id -> depends_on_id)`` means *task ``task_id`` depends on /
    is blocked-by task ``depends_on_id``* — the same orientation the in-memory
    board's cycle detector uses. This adjacency table is what the DB-backed
    :class:`~forge_board.sql_service.SqlAlchemyBoardService` persists the
    dependency graph in (the base ``task`` table has no ``depends_on`` column).
    Both endpoints ``ON DELETE CASCADE`` so deleting a task removes its incident
    edges without leaving dangling references.
    """

    __tablename__ = "task_dependency"
    __table_args__ = (UniqueConstraint("task_id", "depends_on_id"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    depends_on_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
