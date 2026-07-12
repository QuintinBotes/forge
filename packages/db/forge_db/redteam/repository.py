"""Workspace-scoped, insert-only reads/writes over ``red_team_record``.

Deliberately exposes **no update/delete path**: the only write is
:meth:`RedTeamRepository.insert`, and the ORM-level table opts into the shared
Postgres :func:`~forge_db.base.attach_immutability_trigger` (BEFORE UPDATE/DELETE
-> raise), mirroring ``AttestationRepository`` / ``policy_rule_evaluation``'s
append-only treatment.

:func:`record_red_team_verdict` is the one-call recorder the Red-Team Gate wires
into the workflow: it appends the verdict and, when the change ``survived``,
emits the chained ``redteam.survived`` audit event whose ``detail_ref`` points
back at the freshly-inserted row — the tamper-evident "survived adversarial
review" fact the Phase-1 :class:`AttestationService` later reads. The insert and
the audit row commit together on the caller's transaction (fail-closed), exactly
like :class:`~forge_api.services.attestation_service.AttestationService`.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_contracts.audit import AuditEvent
from forge_db.audit.writer import SqlAuditWriter
from forge_db.models.audit import AuditLog
from forge_db.models.red_team import VERDICT_SURVIVED, RedTeamRecord

__all__ = ["REDTEAM_SURVIVED_ACTION", "RedTeamRepository", "record_red_team_verdict"]

#: Audit action emitted when a candidate survives adversarial review.
REDTEAM_SURVIVED_ACTION = "redteam.survived"


class RedTeamRepository:
    """Insert-only repository over the append-only ``red_team_record`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(
        self,
        workspace_id: UUID,
        *,
        id: UUID | None = None,
        verdict: str,
        kind: str,
        evidence: dict[str, Any] | None = None,
        adversary_model: str | None = None,
        coder_model: str | None = None,
        workflow_run_id: UUID | None = None,
    ) -> RedTeamRecord:
        """Append one red-team verdict row and flush it (visible within the txn).

        ``id`` may be supplied to pre-generate the primary key so an audit event
        emitted *before* the insert can reference the row (the append-only table
        forbids writing anything via a later UPDATE), mirroring
        :class:`AttestationRepository`.
        """
        row = RedTeamRecord(
            **({"id": id} if id is not None else {}),
            workspace_id=workspace_id,
            verdict=verdict,
            kind=kind,
            evidence=evidence if evidence is not None else {},
            adversary_model=adversary_model,
            coder_model=coder_model,
            workflow_run_id=workflow_run_id,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def survived_for_run(self, workspace_id: UUID, workflow_run_id: UUID) -> list[RedTeamRecord]:
        """The ``survived`` adversarial-review records for a run, newest first.

        The Phase-1 attestation reads this as its "survived adversarial review"
        input: a non-empty list means the change earned a survived record.
        """
        query = (
            select(RedTeamRecord)
            .where(
                RedTeamRecord.workspace_id == workspace_id,
                RedTeamRecord.workflow_run_id == workflow_run_id,
                RedTeamRecord.verdict == VERDICT_SURVIVED,
            )
            .order_by(RedTeamRecord.created_at.desc(), RedTeamRecord.id.desc())
        )
        return list(self._session.scalars(query).all())

    def get_by_run(self, workspace_id: UUID, workflow_run_id: UUID) -> list[RedTeamRecord]:
        """All red-team records (any verdict) for a run, newest first."""
        query = (
            select(RedTeamRecord)
            .where(
                RedTeamRecord.workspace_id == workspace_id,
                RedTeamRecord.workflow_run_id == workflow_run_id,
            )
            .order_by(RedTeamRecord.created_at.desc(), RedTeamRecord.id.desc())
        )
        return list(self._session.scalars(query).all())


def record_red_team_verdict(
    session: Session,
    workspace_id: UUID,
    *,
    verdict: str,
    kind: str,
    evidence: dict[str, Any] | None = None,
    adversary_model: str | None = None,
    coder_model: str | None = None,
    workflow_run_id: UUID | None = None,
    actor_id: UUID | None = None,
    redactor: object | None = None,
) -> tuple[RedTeamRecord, AuditLog | None]:
    """Append one verdict row; on ``survived`` also chain a ``redteam.survived``
    audit event whose ``detail_ref`` points back at the row.

    Returns ``(row, audit_log)`` — ``audit_log`` is ``None`` for a ``blocked``
    verdict (nothing survived to attest). Participates in the caller's
    transaction (the caller commits), so the verdict and its audit entry commit
    atomically (fail-closed), mirroring
    :meth:`AttestationService.attest_changeset`.
    """
    record_id = uuid.uuid4()
    audit_log: AuditLog | None = None
    if verdict == VERDICT_SURVIVED:
        audit_log = SqlAuditWriter(session, redactor=redactor).emit(
            AuditEvent(
                workspace_id=workspace_id,
                action=REDTEAM_SURVIVED_ACTION,
                actor_id=actor_id,
                actor_type="system" if actor_id is None else "user",
                target_type="workflow_run",
                scope_type="workflow_run",
                scope_id=workflow_run_id,
                severity="notice",
                detail_ref={"table": "red_team_record", "id": str(record_id)},
                details={
                    "kind": kind,
                    "adversary_model": adversary_model,
                    "coder_model": coder_model,
                    "workflow_run_id": str(workflow_run_id) if workflow_run_id else None,
                },
            )
        )
    row = RedTeamRepository(session).insert(
        workspace_id,
        id=record_id,
        verdict=verdict,
        kind=kind,
        evidence=evidence,
        adversary_model=adversary_model,
        coder_model=coder_model,
        workflow_run_id=workflow_run_id,
    )
    return row, audit_log
