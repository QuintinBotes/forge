"""Persistence for the deployment subsystem (workspace-scoped, append-only audit).

The repository owns every read/write of the F31 tables. Audit tables
(``deployment_transition``, ``deployment_check_result``, ``deployment_approval``)
expose insert-only methods — there is no update/delete path, which enforces
append-only semantics on SQLite too (the Postgres trigger enforces it at the DB).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from forge_db.models.deployment import (
    Deployment,
    DeploymentApproval,
    DeploymentCheckResult,
    DeploymentTransition,
    Environment,
    EnvironmentPipeline,
)
from forge_deploy.errors import DeploymentNotFoundError
from forge_deploy.states import DeploymentState, GateCheckName, GateCheckStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeploymentRepository:
    def __init__(self, session: Session, *, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    @property
    def _supports_for_update(self) -> bool:
        return self.session.bind is not None and self.session.bind.dialect.name != "sqlite"

    # ----------------------------------------------------------------- reads
    def get(self, deployment_id: uuid.UUID) -> Deployment | None:
        row = self.session.get(Deployment, deployment_id)
        if row is None or row.workspace_id != self.workspace_id:
            return None
        return row

    def get_or_404(self, deployment_id: uuid.UUID) -> Deployment:
        row = self.get(deployment_id)
        if row is None:
            raise DeploymentNotFoundError(str(deployment_id))
        return row

    def lock(self, deployment_id: uuid.UUID) -> Deployment:
        stmt = select(Deployment).where(Deployment.id == deployment_id)
        if self._supports_for_update:
            stmt = stmt.with_for_update()
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None or row.workspace_id != self.workspace_id:
            raise DeploymentNotFoundError(str(deployment_id))
        return row

    def get_pipeline_for_project(self, project_id: uuid.UUID) -> EnvironmentPipeline | None:
        stmt = (
            select(EnvironmentPipeline)
            .where(
                EnvironmentPipeline.workspace_id == self.workspace_id,
                EnvironmentPipeline.project_id == project_id,
            )
            .options(selectinload(EnvironmentPipeline.environments))
        )
        return self.session.execute(stmt).scalars().first()

    def get_environment(self, pipeline_id: uuid.UUID, name: str) -> Environment | None:
        stmt = select(Environment).where(
            Environment.pipeline_id == pipeline_id,
            Environment.name == name,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def environments(self, pipeline_id: uuid.UUID) -> list[Environment]:
        stmt = (
            select(Environment)
            .where(Environment.pipeline_id == pipeline_id)
            .order_by(Environment.rank)
        )
        return list(self.session.execute(stmt).scalars())

    def predecessor(self, environment: Environment) -> Environment | None:
        if environment.rank == 0:
            return None
        stmt = (
            select(Environment)
            .where(
                Environment.pipeline_id == environment.pipeline_id,
                Environment.rank < environment.rank,
            )
            .order_by(Environment.rank.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def currently_deployed(self, environment_id: uuid.UUID) -> Deployment | None:
        """The most recent ``succeeded`` deployment for an environment."""
        stmt = (
            select(Deployment)
            .where(
                Deployment.environment_id == environment_id,
                Deployment.state == DeploymentState.SUCCEEDED,
            )
            .order_by(Deployment.finished_at.desc(), Deployment.requested_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def last_good(
        self, environment_id: uuid.UUID, *, exclude_id: uuid.UUID | None = None
    ) -> Deployment | None:
        """The most recent ``succeeded`` deployment, excluding ``exclude_id``."""
        stmt = (
            select(Deployment)
            .where(
                Deployment.environment_id == environment_id,
                Deployment.state == DeploymentState.SUCCEEDED,
            )
            .order_by(Deployment.finished_at.desc(), Deployment.requested_at.desc())
        )
        if exclude_id is not None:
            stmt = stmt.where(Deployment.id != exclude_id)
        return self.session.execute(stmt.limit(1)).scalars().first()

    def predecessor_succeeded_for(
        self, environment: Environment, commit_sha: str
    ) -> Deployment | None:
        pred = self.predecessor(environment)
        if pred is None:
            return None
        stmt = (
            select(Deployment)
            .where(
                Deployment.environment_id == pred.id,
                Deployment.commit_sha == commit_sha,
                Deployment.state == DeploymentState.SUCCEEDED,
            )
            .order_by(Deployment.finished_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def active_for_environment(self, environment_id: uuid.UUID) -> Deployment | None:
        terminal = {s.value for s in DeploymentState} - {
            DeploymentState.SUCCEEDED.value,
            DeploymentState.FAILED.value,
            DeploymentState.GATE_REJECTED.value,
            DeploymentState.ROLLED_BACK.value,
            DeploymentState.CANCELLED.value,
        }
        stmt = select(Deployment).where(
            Deployment.environment_id == environment_id,
            Deployment.state.in_(list(terminal)),
        )
        return self.session.execute(stmt).scalars().first()

    def find_by_idempotency(
        self, environment_id: uuid.UUID, idempotency_key: str
    ) -> Deployment | None:
        stmt = select(Deployment).where(
            Deployment.environment_id == environment_id,
            Deployment.idempotency_key == idempotency_key,
        )
        return self.session.execute(stmt).scalars().first()

    def list_deployments(
        self,
        *,
        project_id: uuid.UUID,
        environment_name: str | None = None,
        state: DeploymentState | None = None,
        limit: int = 50,
    ) -> list[Deployment]:
        stmt = select(Deployment).where(
            Deployment.workspace_id == self.workspace_id,
            Deployment.project_id == project_id,
        )
        if environment_name is not None:
            stmt = stmt.where(Deployment.environment_name == environment_name)
        if state is not None:
            stmt = stmt.where(Deployment.state == state)
        stmt = stmt.order_by(Deployment.requested_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars())

    def transitions(self, deployment_id: uuid.UUID) -> list[DeploymentTransition]:
        stmt = (
            select(DeploymentTransition)
            .where(DeploymentTransition.deployment_id == deployment_id)
            .order_by(DeploymentTransition.sequence)
        )
        return list(self.session.execute(stmt).scalars())

    def checks(self, deployment_id: uuid.UUID) -> list[DeploymentCheckResult]:
        stmt = select(DeploymentCheckResult).where(
            DeploymentCheckResult.deployment_id == deployment_id
        )
        return list(self.session.execute(stmt).scalars())

    def approvals(self, deployment_id: uuid.UUID) -> list[DeploymentApproval]:
        stmt = select(DeploymentApproval).where(DeploymentApproval.deployment_id == deployment_id)
        return list(self.session.execute(stmt).scalars())

    def distinct_approver_count(self, deployment_id: uuid.UUID) -> int:
        return len(
            {a.approver_user_id for a in self.approvals(deployment_id) if a.decision == "approve"}
        )

    # ---------------------------------------------------------------- writes
    def create_deployment(self, **kwargs: Any) -> Deployment:
        kwargs.setdefault("workspace_id", self.workspace_id)
        kwargs.setdefault("requested_at", _utcnow())
        row = Deployment(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def _next_sequence(self, deployment_id: uuid.UUID) -> int:
        existing = self.session.execute(
            select(DeploymentTransition.sequence).where(
                DeploymentTransition.deployment_id == deployment_id
            )
        ).scalars()
        seqs = list(existing)
        return (max(seqs) + 1) if seqs else 1

    def append_transition(
        self,
        deployment: Deployment,
        *,
        from_state: str,
        to_state: str,
        event: str,
        guard_results: dict[str, Any] | None = None,
        effects: list[str] | None = None,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> DeploymentTransition:
        row = DeploymentTransition(
            workspace_id=self.workspace_id,
            deployment_id=deployment.id,
            sequence=self._next_sequence(deployment.id),
            from_state=from_state,
            to_state=to_state,
            event=event,
            guard_results=guard_results or {},
            effects_dispatched=list(effects or []),
            actor=actor,
            payload=payload or {},
            idempotency_key=idempotency_key,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_check_result(
        self,
        deployment_id: uuid.UUID,
        *,
        name: GateCheckName,
        status: GateCheckStatus,
        detail: str = "",
        metrics: dict[str, str] | None = None,
    ) -> DeploymentCheckResult:
        row = DeploymentCheckResult(
            workspace_id=self.workspace_id,
            deployment_id=deployment_id,
            name=name,
            status=status,
            detail=detail,
            metrics=metrics or {},
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_approval(
        self,
        deployment_id: uuid.UUID,
        *,
        approver_user_id: uuid.UUID,
        decision: str,
        reason: str | None = None,
    ) -> DeploymentApproval:
        row = DeploymentApproval(
            workspace_id=self.workspace_id,
            deployment_id=deployment_id,
            approver_user_id=approver_user_id,
            decision=decision,
            reason=reason,
        )
        self.session.add(row)
        self.session.flush()
        return row


__all__ = ["DeploymentRepository"]
