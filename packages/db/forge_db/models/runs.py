"""Execution models: WorkflowRun, AgentRun, ApprovalRequest, SubAgentRun."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import (
    ApprovalGate,
    ApprovalStatus,
    ExecutionMode,
    RunStatus,
    SandboxKind,
    WorkflowState,
)

if TYPE_CHECKING:
    from forge_db.models.planning import Task
    from forge_db.models.sandbox import SandboxInstance


class WorkflowRun(WorkspaceScopedModel):
    """A durable FSM run for a task (spec: Default Feature Workflow States)."""

    __tablename__ = "workflow_run"

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
        back_populates="parent_agent_run", cascade="all, delete-orphan"
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
    """A sub-agent execution spawned by a primary agent (multi-agent mode)."""

    __tablename__ = "sub_agent_run"

    parent_agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus), default=RunStatus.PENDING, nullable=False
    )
    inputs: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    steps: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    parent_agent_run: Mapped[AgentRun] = relationship(back_populates="sub_agent_runs")
