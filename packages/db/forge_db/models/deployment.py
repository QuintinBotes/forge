"""Deployment-gates models (F31 — deployment gates & environment promotion).

Six tables drive the environment-promotion control plane:

* ``environment_pipeline`` — one ordered pipeline per (project, repo).
* ``environment`` — an ordered stage (``dev``/``staging``/``production``) with
  per-stage gate/provider/health config; ``is_restricted`` is derived from repo
  ``deploy_rules`` and can never be relaxed to ``false``.
* ``deployment`` — a single promotion attempt (the FSM "run").
* ``deployment_transition`` — append-only FSM audit (mirrors ``workflow_transition``).
* ``deployment_check_result`` — per-deployment automated gate-check outcomes.
* ``deployment_approval`` — append-only deploy-approval decisions (distinct
  approver counting + no-self-approval). Forge's approval handling is in-memory
  and the ``ApprovalRequest`` contract is frozen, so deploy approvals are
  recorded here within the deployment domain rather than mutating the shared
  primitive (see slice notes — conforms to the foundation).

All append-only tables opt into the shared Postgres immutability trigger and the
repository layer exposes no update/delete path (cross-dialect enforcement).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import (
    WorkspaceScopedModel,
    attach_immutability_trigger,
    enum_type,
    json_type,
)
from forge_db.models.enums import (
    DeploymentKind,
    DeploymentState,
    DeploymentTrigger,
    GateCheckName,
    GateCheckStatus,
    HealthStatus,
)

#: States in which a deployment still holds the environment's single active slot.
#: Rollback deployments are excluded: a ``kind=rollback`` runs concurrently with
#: its parent (which holds the slot while in ``rolling_back``), so it must not
#: trip the at-most-one-active-per-environment guard.
_ACTIVE_DEPLOYMENT_SQL = (
    "state NOT IN ('succeeded','failed','gate_rejected','rolled_back','cancelled') "
    "AND kind <> 'rollback'"
)


class EnvironmentPipeline(WorkspaceScopedModel):
    """An ordered environment pipeline for a (project, repo)."""

    __tablename__ = "environment_pipeline"
    __table_args__ = (
        Index(
            "uq_environment_pipeline_project_repo",
            "project_id",
            "repo_id",
            unique=True,
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_id: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    environments: Mapped[list[Environment]] = relationship(
        back_populates="pipeline",
        cascade="all, delete-orphan",
        order_by="Environment.rank",
    )


class Environment(WorkspaceScopedModel):
    """An ordered stage within a pipeline."""

    __tablename__ = "environment"
    __table_args__ = (
        Index("uq_environment_pipeline_name", "pipeline_id", "name", unique=True),
        Index("uq_environment_pipeline_rank", "pipeline_id", "rank", unique=True),
        CheckConstraint("rank >= 0", name="rank_non_negative"),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("environment_pipeline.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    is_restricted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    gate_config: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    provider_config: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    health_check: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )

    pipeline: Mapped[EnvironmentPipeline] = relationship(back_populates="environments")


class Deployment(WorkspaceScopedModel):
    """A single promotion attempt — the durable run of the deployment FSM."""

    __tablename__ = "deployment"
    __table_args__ = (
        Index("ix_deployment_env_state", "environment_id", "state"),
        Index(
            "uq_deployment_active_env",
            "environment_id",
            unique=True,
            postgresql_where=text(_ACTIVE_DEPLOYMENT_SQL),
            sqlite_where=text(_ACTIVE_DEPLOYMENT_SQL),
        ),
        Index(
            "ix_deployment_currently_deployed",
            "repo_id",
            "environment_name",
            "state",
            "finished_at",
        ),
        Index(
            "uq_deployment_idempotency",
            "environment_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("environment_pipeline.id", ondelete="CASCADE"),
        nullable=False,
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("environment.id", ondelete="CASCADE"),
        nullable=False,
    )
    environment_name: Mapped[str] = mapped_column(String(128), nullable=False)
    repo_id: Mapped[str] = mapped_column(String(512), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    from_environment_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[DeploymentKind] = mapped_column(
        enum_type(DeploymentKind),
        default=DeploymentKind.PROMOTION,
        server_default=DeploymentKind.PROMOTION.value,
        nullable=False,
    )
    rollback_of: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deployment.id", ondelete="SET NULL"),
        nullable=True,
    )
    state: Mapped[DeploymentState] = mapped_column(
        enum_type(DeploymentState),
        default=DeploymentState.REQUESTED,
        server_default=DeploymentState.REQUESTED.value,
        nullable=False,
        index=True,
    )
    trigger: Mapped[DeploymentTrigger] = mapped_column(
        enum_type(DeploymentTrigger),
        default=DeploymentTrigger.MANUAL,
        server_default=DeploymentTrigger.MANUAL.value,
        nullable=False,
    )
    initiated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    provider_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    health_status: Mapped[HealthStatus | None] = mapped_column(
        enum_type(HealthStatus), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    freeze_override_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    transitions: Mapped[list[DeploymentTransition]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        order_by="DeploymentTransition.sequence",
    )
    checks: Mapped[list[DeploymentCheckResult]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )
    approvals: Mapped[list[DeploymentApproval]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
    )


class DeploymentTransition(WorkspaceScopedModel):
    """Append-only FSM transition audit row."""

    __tablename__ = "deployment_transition"
    __table_args__ = (
        Index(
            "uq_deployment_transition_seq", "deployment_id", "sequence", unique=True
        ),
        Index(
            "uq_deployment_transition_idem",
            "deployment_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deployment.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    guard_results: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    effects_dispatched: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    deployment: Mapped[Deployment] = relationship(back_populates="transitions")


class DeploymentCheckResult(WorkspaceScopedModel):
    """Per-deployment automated gate-check outcome."""

    __tablename__ = "deployment_check_result"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deployment.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[GateCheckName] = mapped_column(enum_type(GateCheckName), nullable=False)
    status: Mapped[GateCheckStatus] = mapped_column(
        enum_type(GateCheckStatus), nullable=False
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metrics: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )

    deployment: Mapped[Deployment] = relationship(back_populates="checks")


class DeploymentApproval(WorkspaceScopedModel):
    """Append-only deploy-approval decision (distinct-approver counting)."""

    __tablename__ = "deployment_approval"
    __table_args__ = (
        Index(
            "uq_deployment_approval_approver",
            "deployment_id",
            "approver_user_id",
            unique=True,
        ),
    )

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deployment.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approver_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    deployment: Mapped[Deployment] = relationship(back_populates="approvals")


# Append-only enforcement on Postgres (no-op on SQLite; repo enforces there).
attach_immutability_trigger(DeploymentTransition.__table__)
attach_immutability_trigger(DeploymentCheckResult.__table__)
attach_immutability_trigger(DeploymentApproval.__table__)
