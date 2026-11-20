"""Workspace-scoped, insert-only reads and writes over the risk tables (#193).

No update or delete path: both tables are append-only (the Postgres immutability
trigger rejects UPDATE and DELETE). Every read is scoped to one workspace.
:class:`DbRiskReader` is the shipping gates' ``RiskReader`` over them (#194).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, func, or_, select
from sqlalchemy.orm import Session

from scf_contracts import (
    ClassifiedChange,
    GatedChange,
    GateRequirements,
    RiskClass,
    RiskEnforcement,
    RiskEvaluation,
    RiskGate,
)
from scf_db.models.factory_metrics import ChangeOutcome
from scf_db.models.risk import RiskClassification, RiskGateObservation
from scf_db.models.runs import AgentRun

__all__ = ["DbRiskReader", "RiskRepository"]


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
        class_requirements: dict[str, Any] | None = None,
    ) -> RiskClassification:
        """Append one classification and flush it (visible within the transaction).

        ``class_requirements`` maps each class name to its ``GateRequirements``.
        """
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
            class_requirements=class_requirements,
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

    def latest_for(self, change: GatedChange) -> RiskClassification | None:
        """The newest classification of ``change`` by its most specific identity that has one.

        A commit names a change by its head or, once its pull request landed, by the
        merge commit recorded against its run (#335).
        """
        rc = RiskClassification
        merged_runs = select(ChangeOutcome.agent_run_id).where(
            ChangeOutcome.workspace_id == change.workspace_id,
            ChangeOutcome.merge_commit_sha == change.commit_sha,
        )
        identities: list[tuple[object, Any]] = [
            (
                change.commit_sha,
                or_(rc.commit_sha == change.commit_sha, rc.agent_run_id.in_(merged_runs)),
            ),
            (change.agent_run_id, rc.agent_run_id == change.agent_run_id),
            (change.workflow_run_id, rc.workflow_run_id == change.workflow_run_id),
        ]
        for value, matches in identities:
            if value is None:
                continue
            query = (
                select(rc)
                .where(rc.workspace_id == change.workspace_id, matches)
                .order_by(rc.created_at.desc(), rc.id.desc())
                .limit(1)
            )
            row = self._session.scalars(query).first()
            if row is not None:
                return row
        return None

    def enforcement_of(self, workspace_id: UUID, repo: str) -> str | None:
        """The mode recorded with the newest classified change of ``repo``; ``None`` if none (#335).

        A change's repository is its agent run's ``owner/name``. ``repo`` may carry a
        host or URL around it, and matches case-insensitively, as GitHub names do.
        """
        rc = RiskClassification
        run_repo = AgentRun.inputs["context"]["repo_full_name"].as_string()
        owner_name = "/".join(
            repo.strip().strip("/").removesuffix(".git").replace(":", "/").split("/")[-2:]
        )
        query = (
            select(rc.enforcement_mode)
            .join(AgentRun, AgentRun.id == rc.agent_run_id)
            .where(
                rc.workspace_id == workspace_id,
                AgentRun.workspace_id == workspace_id,
                func.lower(run_repo) == owner_name.lower(),
            )
            .order_by(rc.created_at.desc(), rc.id.desc())
            .limit(1)
        )
        return self._session.scalar(query)

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


class DbRiskReader:
    """The shipping gates' ``RiskReader`` over the stored classifications (#194).

    Given a ``Session`` it works in the caller's transaction: an observation is
    flushed and commits with the gate's own decision. Given a session factory, each
    call opens a session and commits its observation. With ``record=False`` it
    observes nothing: for a preview of a gate that decides nothing.
    """

    def __init__(self, sessions: Session | Callable[[], Session], *, record: bool = True) -> None:
        self._sessions = sessions
        self._record = record

    @contextmanager
    def _session(self) -> Iterator[Session]:
        if isinstance(self._sessions, Session):
            # A SAVEPOINT: a failed read or observation rolls back only itself and
            # leaves the gate's own transaction usable (FR-013).
            with self._sessions.begin_nested():
                yield self._sessions
            return
        with self._sessions() as session:
            yield session
            session.commit()

    def risk_for(self, change: GatedChange) -> ClassifiedChange | None:
        with self._session() as session:
            row = RiskRepository(session).latest_for(change)
        if row is None:
            return None
        named = bool((row.override_approver or "").strip() and (row.override_reason or "").strip())
        # Only a named override ships a change below its derived class (spec A §3.1).
        ships_as = row.final_class if named else max(row.derived_class, row.final_class)
        raw = (row.class_requirements or {}).get(RiskClass(ships_as).value)
        if raw is None:
            return None  # recorded before the requirements were: nothing to tighten with
        return ClassifiedChange(
            classification_id=row.id,
            workspace_id=row.workspace_id,
            enforcement=row.enforcement_mode,  # type: ignore[arg-type]  # validated
            requirements=GateRequirements.model_validate(raw),
        )

    def enforcement(self, workspace_id: UUID, repo_id: str) -> RiskEnforcement | None:
        """The mode of ``repo_id``'s risk rules, as its newest classified change recorded it."""
        with self._session() as session:
            mode = RiskRepository(session).enforcement_of(workspace_id, repo_id)
        return mode  # type: ignore[return-value]  # written from a validated policy

    def observe(
        self, risk: ClassifiedChange, *, gate: RiskGate, actual: str, risk_decision: str
    ) -> None:
        if not self._record:
            return
        with self._session() as session:
            RiskRepository(session).record_gate_observation(
                risk.workspace_id,
                classification_id=risk.classification_id,
                gate_name=gate,
                actual_decision=actual,
                risk_decision=risk_decision,
                agreed=actual == risk_decision,
            )
