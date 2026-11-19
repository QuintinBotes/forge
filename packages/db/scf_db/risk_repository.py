"""Workspace-scoped, insert-only reads and writes over the risk tables (#193).

No update or delete path: both tables are append-only (the Postgres immutability
trigger rejects UPDATE and DELETE). Every read is scoped to one workspace.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from scf_contracts import RiskEnforcement, RiskEvaluation
from scf_db.models.risk import RiskClassification, RiskGateObservation

__all__ = ["RiskRepository"]


class RiskRepository:
    """Append and read risk classifications and gate observations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record_classification(
        self,
        workspace_id: UUID,
        evaluation: RiskEvaluation,
        *,
        commit_sha: str,
        signals: dict[str, Any],
        enforcement_mode: RiskEnforcement,
        pr_numbers: list[int] | None = None,
        agent_run_id: UUID | None = None,
        workflow_run_id: UUID | None = None,
        spec_key: str | None = None,
        spec_version: int | None = None,
    ) -> RiskClassification:
        """Append one classification and flush it (visible within the transaction)."""
        row = RiskClassification(
            workspace_id=workspace_id,
            commit_sha=commit_sha,
            pr_numbers=pr_numbers or [],
            agent_run_id=agent_run_id,
            workflow_run_id=workflow_run_id,
            spec_key=spec_key,
            spec_version=spec_version,
            derived_class=evaluation.derived_class,
            declared_class=evaluation.declared_class,
            final_class=evaluation.final_class,
            matched_rule_ids=list(evaluation.matched_rule_ids),
            signals=signals,
            override_approver=evaluation.override_approver,
            override_reason=evaluation.override_reason,
            enforcement_mode=enforcement_mode,
            fallback_applied=evaluation.fallback_applied,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def record_gate_observation(
        self,
        workspace_id: UUID,
        *,
        classification_id: UUID,
        gate_name: str,
        actual_decision: str,
        risk_decision: str,
        agreed: bool,
    ) -> RiskGateObservation:
        """Append one gate observation of a classification in the same workspace.

        Raises:
            ValueError: the classification does not exist in ``workspace_id``.
        """
        classification = self._session.get(RiskClassification, classification_id)
        if classification is None or classification.workspace_id != workspace_id:
            raise ValueError(f"no risk classification {classification_id} in this workspace")
        row = RiskGateObservation(
            workspace_id=workspace_id,
            classification_id=classification_id,
            gate_name=gate_name,
            actual_decision=actual_decision,
            risk_decision=risk_decision,
            agreed=agreed,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_classifications(
        self,
        workspace_id: UUID,
        *,
        commit_sha: str | None = None,
        agent_run_id: UUID | None = None,
        workflow_run_id: UUID | None = None,
        spec_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RiskClassification]:
        """A filtered page of the workspace's classifications, newest first."""
        query = select(RiskClassification).where(RiskClassification.workspace_id == workspace_id)
        if commit_sha is not None:
            query = query.where(RiskClassification.commit_sha == commit_sha)
        if agent_run_id is not None:
            query = query.where(RiskClassification.agent_run_id == agent_run_id)
        if workflow_run_id is not None:
            query = query.where(RiskClassification.workflow_run_id == workflow_run_id)
        if spec_key is not None:
            query = query.where(RiskClassification.spec_key == spec_key)
        query = query.order_by(RiskClassification.created_at.desc(), RiskClassification.id.desc())
        return list(self._session.scalars(query.limit(limit).offset(offset)))

    def agreement(self, workspace_id: UUID, *, since: datetime) -> list[tuple[str, int, int]]:
        """``(gate_name, observations, agreed)`` per gate observed since ``since``, by name."""
        obs = RiskGateObservation
        query = (
            select(obs.gate_name, func.count(), func.sum(obs.agreed.cast(Integer)))
            .where(obs.workspace_id == workspace_id, obs.created_at >= since)
            .group_by(obs.gate_name)
            .order_by(obs.gate_name)
        )
        rows = self._session.execute(query)
        return [(gate, total, int(agreed or 0)) for gate, total, agreed in rows]
