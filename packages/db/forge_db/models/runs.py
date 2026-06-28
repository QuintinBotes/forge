"""Execution models: WorkflowRun, AgentRun, ApprovalRequest, SubAgentRun."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import (
    ApprovalGate,
    ApprovalStatus,
    EngineBackend,
    ExecutionMode,
    RunStatus,
    SandboxKind,
    WorkflowState,
)

if TYPE_CHECKING:
    from forge_db.models.planning import Task
    from forge_db.models.sandbox import SandboxInstance


class WorkflowRun(WorkspaceScopedModel):
    """A durable FSM run for a task (spec: Default Feature Workflow States).

    F25 adds three engine-attribution columns so a run can be driven either by
    the V1 Postgres FSM or the V2 Temporal durable workflow engine. The Temporal
    engine writes ``temporal_workflow_id = wf-<run_id>``; a partial-unique index
    on it (where not null) gives the same single-active-run guarantee the FSM
    enforces, complementing Temporal's ``WorkflowIdReusePolicy.REJECT_DUPLICATE``.
    """

    __tablename__ = "workflow_run"
    __table_args__ = (
        Index("ix_workflow_run_temporal_wfid", "temporal_workflow_id"),
        Index(
            "uq_workflow_run_temporal_workflow_id",
            "temporal_workflow_id",
            unique=True,
            postgresql_where=text("temporal_workflow_id IS NOT NULL"),
            sqlite_where=text("temporal_workflow_id IS NOT NULL"),
        ),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="CASCADE"), nullable=False
    )
    workflow_name: Mapped[str] = mapped_column(
        String(128), default="default_feature", nullable=False
    )
    current_state: Mapped[WorkflowState] = mapped_column(
        enum_type(WorkflowState), default=WorkflowState.CREATED, nullable=False
    )
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        enum_type(ExecutionMode), default=ExecutionMode.SINGLE_AGENT, nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus), default=RunStatus.PENDING, nullable=False
    )
    context: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    # F25 — engine attribution. ``enum_type`` renders VARCHAR + CHECK in
    # (postgres_fsm, temporal); defaults keep every pre-F25 / FSM run unchanged.
    engine_backend: Mapped[EngineBackend] = mapped_column(
        enum_type(EngineBackend),
        default=EngineBackend.POSTGRES_FSM,
        server_default=EngineBackend.POSTGRES_FSM.value,
        nullable=False,
    )
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    temporal_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # F28 — the DB definition revision a run resolved to (NULL = bundled file was
    # used). Pinned at start so in-flight runs never drift to a newer publish.
    # Plain UUID column (no DB-level FK) to keep workflow_run independent of the
    # editor tables' create/drop ordering; integrity is app-enforced.
    definition_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    task: Mapped[Task] = relationship(back_populates="workflow_runs")
    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="workflow_run", cascade="all, delete-orphan"
    )


class AgentRun(WorkspaceScopedModel):
    """A single-agent execution within a workflow run."""

    __tablename__ = "agent_run"

    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_run.id", ondelete="CASCADE"),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(64), default="primary", nullable=False)
    skill_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus), default=RunStatus.PENDING, nullable=False
    )
    # F27 — supervised multi-agent coordinator columns (null/false for single-agent).
    # ``is_supervisor`` marks the parent coordinator run; ``pattern`` records the
    # chosen ``CoordinationPattern``; ``supervision`` holds the resolved plan +
    # merge summary (redacted; large blobs offloaded to object store).
    is_supervisor: Mapped[bool] = mapped_column(
        Boolean(), default=False, server_default=text("false"), nullable=False
    )
    pattern: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supervision: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, server_default=text("'{}'"), nullable=False
    )
    inputs: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    steps: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # F19 — container sandboxing: which provider ran this run + container handle.
    sandbox_kind: Mapped[SandboxKind] = mapped_column(
        enum_type(SandboxKind), default=SandboxKind.WORKTREE, nullable=False
    )
    sandbox_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sandbox_container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    workflow_run: Mapped[WorkflowRun | None] = relationship(back_populates="agent_runs")
    approval_requests: Mapped[list[ApprovalRequest]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )
    sub_agent_runs: Mapped[list[SubAgentRun]] = relationship(
        back_populates="parent_agent_run",
        cascade="all, delete-orphan",
        foreign_keys="SubAgentRun.parent_agent_run_id",
    )
    sandbox_instances: Mapped[list[SandboxInstance]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )


class ApprovalRequest(WorkspaceScopedModel):
    """A human approval gate (spec: Human Approval System)."""

    __tablename__ = "approval_request"

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=True,
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_run.id", ondelete="CASCADE"),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
    )
    gate: Mapped[ApprovalGate] = mapped_column(enum_type(ApprovalGate), nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_type(ApprovalStatus), default=ApprovalStatus.PENDING, nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    agent_run: Mapped[AgentRun | None] = relationship(back_populates="approval_requests")


class SubAgentRun(WorkspaceScopedModel):
    """A sub-agent execution spawned by the F27 Supervisor (multi-agent mode).

    One row per spawned specialist (``SubAgentRun[]`` of the Core Data Model).
    Conforms to the foundation: ``role`` is the :class:`SubAgentRole` value as a
    string and ``status`` reuses the shared :class:`RunStatus` enum (rather than
    introducing parallel ``sub_agent_role`` / ``sub_agent_status`` enums).
    ``completed_at`` doubles as the spec's ``finished_at``.
    """

    __tablename__ = "sub_agent_run"
    __table_args__ = (
        # Unique as an INDEX (not a named constraint) so the F27 migration's
        # downgrade is cross-dialect reversible (SQLite cannot ALTER-DROP a named
        # constraint, but DROP INDEX works).
        Index(
            "uq_sub_agent_run_assignment",
            "parent_agent_run_id",
            "assignment_id",
            unique=True,
        ),
        Index("ix_sub_agent_run_parent", "parent_agent_run_id", "ordinal"),
        Index("ix_sub_agent_run_child", "agent_run_id"),
    )

    parent_agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The child F06 ExecutionAgent run (its steps live in ``agent_run``); null
    # until the subagent is spawned. The DB-level FK (ON DELETE SET NULL) is added
    # on Postgres by the F27 migration — it is intentionally NOT declared inline so
    # the column stays cross-dialect droppable on the migration's SQLite downgrade.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    assignment_id: Mapped[str] = mapped_column(
        String(128), default="", server_default=text("''"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    pattern: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ordinal: Mapped[int] = mapped_column(
        Integer(), default=0, server_default=text("0"), nullable=False
    )
    depends_on: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, server_default=text("'[]'"), nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus), default=RunStatus.PENDING, nullable=False
    )
    optional: Mapped[bool] = mapped_column(
        Boolean(), default=False, server_default=text("false"), nullable=False
    )
    objective: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, server_default=text("'{}'"), nullable=False
    )
    artifact: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, server_default=text("'{}'"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merged: Mapped[bool] = mapped_column(
        Boolean(), default=False, server_default=text("false"), nullable=False
    )
    token_usage: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, server_default=text("'{}'"), nullable=False
    )
    inputs: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    steps: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    parent_agent_run: Mapped[AgentRun] = relationship(
        back_populates="sub_agent_runs", foreign_keys=[parent_agent_run_id]
    )
