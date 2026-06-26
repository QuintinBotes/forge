"""Planning models: Epic, SpecDocument, Task, Sprint, Milestone, Incident."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
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
    """A time-boxed iteration."""

    __tablename__ = "sprint"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)

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
    knowledge_scope: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    subagent_policy: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    handoff_rules: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    acceptance_criteria: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="tasks")
    epic: Mapped[Epic | None] = relationship(back_populates="tasks")
    spec: Mapped[SpecDocument | None] = relationship(back_populates="tasks")
    sprint: Mapped[Sprint | None] = relationship(back_populates="tasks")
    milestone: Mapped[Milestone | None] = relationship(back_populates="tasks")
    workflow_runs: Mapped[list[WorkflowRun]] = relationship(back_populates="task")


class Incident(WorkspaceScopedModel):
    """An operational incident (spec: Incident Workflow States)."""

    __tablename__ = "incident"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

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
    blast_radius: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runbook: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    postmortem: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    project: Mapped[Project] = relationship(back_populates="incidents")
