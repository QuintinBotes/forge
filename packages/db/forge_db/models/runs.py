"""Execution models: WorkflowRun, AgentRun, ApprovalRequest, SubAgentRun."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
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
    from forge_db.models.approval import ApprovalDecision
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
    """A human approval gate (spec: Human Approval System).

    F36 generalizes the baseline pr-era shape into the canonical gate frame:
    a polymorphic subject (``subject_type``/``subject_id``), inbox grouping
    (``project_id``), risk + SLA (``risk_level``/``expires_at``), and
    multi-approver forward-compat (``required_approvals``). Foundation
    deviations from the F36 slice doc (conform-to-foundation): the gate-type
    column keeps its baseline name ``gate`` (already the six-value
    ``ApprovalGate`` enum — no ``kind``→``gate_type`` rename is needed), and
    the resolver columns keep their baseline names (``decided_by_id`` =
    resolver_user_id, ``decided_at`` = resolved_at, ``decision_reason`` =
    decision_note, ``payload`` = gate_payload, ``summary`` = title).
    """

    __tablename__ = "approval_request"
    __table_args__ = (
        Index("ix_approval_request_workspace_status", "workspace_id", "status"),
        Index("ix_approval_request_project_status", "project_id", "status"),
        # At most ONE open gate of a type per subject (generalizes F08's
        # one-pending-pr-per-run); partial so history rows never collide.
        Index(
            "uq_pending_gate",
            "subject_type",
            "subject_id",
            "gate",
            unique=True,
            postgresql_where=text("status = 'pending' AND subject_id IS NOT NULL"),
            sqlite_where=text("status = 'pending' AND subject_id IS NOT NULL"),
        ),
    )

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
    # F36 — inbox grouping; plain UUID (no DB-level FK) so the column is
    # cross-dialect addable/droppable in the 0019 migration (mirrors
    # ``WorkflowRun.definition_revision_id``); integrity is app-enforced.
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # F36 — polymorphic subject key (workflow_run|agent_run|step|incident|deployment).
    subject_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    gate: Mapped[ApprovalGate] = mapped_column(enum_type(ApprovalGate), nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_type(ApprovalStatus), default=ApprovalStatus.PENDING, nullable=False
    )
    # F36 — from review_rules.min_approvals; V1 resolves on a single decisive vote.
    required_approvals: Mapped[int] = mapped_column(
        Integer(), default=1, server_default=text("1"), nullable=False
    )
    # F36 — info|warning|critical; drives inbox sort + UI emphasis.
    risk_level: Mapped[str] = mapped_column(
        String(16), default="info", server_default=text("'info'"), nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    # F36 — optional object-store key for a large pre-rendered context snapshot.
    context_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # F36 — optional SLA; the worker sweeper marks overdue pending gates expired.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_run: Mapped[AgentRun | None] = relationship(back_populates="approval_requests")
    decisions: Mapped[list[ApprovalDecision]] = relationship(
        back_populates="approval_request", cascade="all, delete-orphan"
    )


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
