"""Workspace-scoped, insert-only reads/writes over ``attestation``.

Deliberately exposes **no update/delete path**: the only write is
:meth:`AttestationRepository.insert`, and the ORM-level table opts into the
shared Postgres :func:`~forge_db.base.attach_immutability_trigger` (BEFORE
UPDATE/DELETE -> raise), mirroring ``AuditQueryRepository`` /
``policy_rule_evaluation``'s append-only treatment.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_db.models.attestation import Attestation

__all__ = ["AttestationRepository"]


class AttestationRepository:
    """Insert-only repository over the append-only ``attestation`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(
        self,
        workspace_id: UUID,
        *,
        subject_digest: str,
        predicate_type: str,
        envelope: dict[str, Any],
        payload_hash: str,
        keyid: str,
        workflow_run_id: UUID | None = None,
        agent_run_id: UUID | None = None,
        pr_numbers: list[Any] | None = None,
        spec_key: str | None = None,
        spec_version: int | None = None,
        audit_seq: int | None = None,
        merkle_leaf_hash: str | None = None,
    ) -> Attestation:
        """Append one attestation row and flush it (visible within the txn)."""
        row = Attestation(
            workspace_id=workspace_id,
            subject_digest=subject_digest,
            predicate_type=predicate_type,
            envelope=envelope,
            payload_hash=payload_hash,
            keyid=keyid,
            workflow_run_id=workflow_run_id,
            agent_run_id=agent_run_id,
            pr_numbers=pr_numbers if pr_numbers is not None else [],
            spec_key=spec_key,
            spec_version=spec_version,
            audit_seq=audit_seq,
            merkle_leaf_hash=merkle_leaf_hash,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_by_run(
        self,
        workspace_id: UUID,
        *,
        workflow_run_id: UUID | None = None,
        agent_run_id: UUID | None = None,
    ) -> Attestation | None:
        """The most recent attestation for a workflow/agent run, or ``None``.

        Exactly one of ``workflow_run_id``/``agent_run_id`` is expected; when a
        run carries several attestations (re-signed / re-attempted), the newest
        (``created_at`` desc, ``id`` desc tiebreak) wins.
        """
        if workflow_run_id is None and agent_run_id is None:
            raise ValueError("get_by_run requires workflow_run_id or agent_run_id")

        query = select(Attestation).where(Attestation.workspace_id == workspace_id)
        if workflow_run_id is not None:
            query = query.where(Attestation.workflow_run_id == workflow_run_id)
        if agent_run_id is not None:
            query = query.where(Attestation.agent_run_id == agent_run_id)
        query = query.order_by(Attestation.created_at.desc(), Attestation.id.desc())
        return self._session.scalars(query).first()

    def list(
        self,
        workspace_id: UUID,
        *,
        subject_digest: str | None = None,
        predicate_type: str | None = None,
        workflow_run_id: UUID | None = None,
        agent_run_id: UUID | None = None,
        spec_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Attestation]:
        """Filtered page of attestations, newest first."""
        query = select(Attestation).where(Attestation.workspace_id == workspace_id)
        if subject_digest is not None:
            query = query.where(Attestation.subject_digest == subject_digest)
        if predicate_type is not None:
            query = query.where(Attestation.predicate_type == predicate_type)
        if workflow_run_id is not None:
            query = query.where(Attestation.workflow_run_id == workflow_run_id)
        if agent_run_id is not None:
            query = query.where(Attestation.agent_run_id == agent_run_id)
        if spec_key is not None:
            query = query.where(Attestation.spec_key == spec_key)
        query = (
            query.order_by(Attestation.created_at.desc(), Attestation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(query).all())
