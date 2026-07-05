"""Postgres-backed :class:`~forge_approval.repository.ApprovalRepository` (F36).

:class:`SqlAlchemyApprovalRepository` is a drop-in, durable alternative to
:class:`~forge_approval.repository.InMemoryApprovalRepository` that satisfies the
**same** async ``ApprovalRepository`` protocol (``add`` / ``get`` /
``find_pending`` / ``list`` / ``update`` / ``add_decision`` / ``decisions_for``)
— so the F36 composition root swaps it in behind ``FORGE_APPROVAL_BACKEND=db``
with no behavioural change. The default stays ``memory`` and the in-memory store
remains the unit-test default.

It lives in ``apps/api`` (not the ``forge_approval`` SDK, which is deliberately
"pure domain — no DB") exactly like the sibling
:class:`~forge_api.observability.audit_db.DbAuditStore`: the frozen SDK stays
DB-free and this adapter maps the domain :class:`ApprovalRequest` /
:class:`ApprovalDecisionRecord` onto the canonical F36 ORM rows in ``forge_db``.

Behaviour parity with the in-memory store is exact and intentional:

* the domain ``ApprovalRequest`` maps onto the baseline ``approval_request`` row
  whose F36 columns keep their baseline names (``gate`` / ``summary`` /
  ``payload`` / ``decided_by_id`` / ``decided_at`` / ``decision_reason``), with
  ``requested_at`` carried by the row's ``created_at`` timestamp and the two
  repository-only fields (``requested_actor`` / ``escalated``) added by revision
  ``0026``;
* ``add_decision`` raises :class:`DuplicateDecisionError` when the
  one-vote-per-approver ``uq_approval_decision_approver`` unique fires (the DB
  analogue of the in-memory per-approver guard), and ``update`` raises
  :class:`ApprovalNotFoundError` for an unknown id — the exact errors the
  service already handles;
* every read is workspace-scoped, so a cross-workspace id reads as absent.

The one storage-boundary divergence (shared with every DB-backed repo here) is
that the DB's ``uq_pending_gate`` partial-unique and the ``approval_decision``
foreign key are *real*: the service always calls ``find_pending`` before ``add``
and loads the parent before ``add_decision``, so these never surface in normal
flow, but a direct duplicate insert raises at the storage boundary rather than
silently succeeding.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRecord,
    ApprovalRequest,
    GateStatus,
    GateType,
    RiskLevel,
)
from forge_approval.repository import (
    ApprovalNotFoundError,
    DuplicateDecisionError,
)
from forge_db.models import ApprovalDecision as ApprovalDecisionRow
from forge_db.models import ApprovalRequest as ApprovalRequestRow
from forge_db.models.enums import ApprovalStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["SqlAlchemyApprovalRepository"]


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to timezone-aware UTC (SQLite reads naive)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _db_status(status: GateStatus) -> ApprovalStatus:
    """Domain gate status -> the DB ``ApprovalStatus`` (value-identical enums)."""
    return ApprovalStatus(status.value)


def _gate_status(value: object) -> GateStatus:
    """DB ``ApprovalStatus`` (or raw string) -> the domain :class:`GateStatus`."""
    return GateStatus(value.value if isinstance(value, ApprovalStatus) else str(value))


class SqlAlchemyApprovalRepository:
    """A Postgres-backed approval repository (implements ``ApprovalRepository``)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ #
    # Mapping                                                             #
    # ------------------------------------------------------------------ #

    def _apply(self, row: ApprovalRequestRow, request: ApprovalRequest) -> None:
        """Write every domain field onto ``row`` (used by ``add`` + ``update``)."""
        row.workspace_id = request.workspace_id
        row.project_id = request.project_id
        row.gate = request.gate_type
        row.status = _db_status(request.status)
        row.subject_type = request.subject_type
        row.subject_id = request.subject_id
        row.workflow_run_id = request.workflow_run_id
        row.agent_run_id = request.agent_run_id
        row.task_id = request.task_id
        row.required_approvals = request.required_approvals
        row.risk_level = request.risk_level
        row.summary = request.title
        row.payload = dict(request.gate_payload or {})
        row.context_ref = request.context_ref
        row.requested_by = str(request.requested_by) if request.requested_by else None
        row.requested_actor = request.requested_actor
        row.escalated = request.escalated
        row.decision_reason = request.decision_note
        row.decided_by_id = request.resolver_user_id
        row.expires_at = request.expires_at
        row.decided_at = request.resolved_at
        # ``requested_at`` is carried by the row's ``created_at`` timestamp column.
        if request.requested_at is not None:
            row.created_at = request.requested_at

    def _to_domain(self, row: ApprovalRequestRow) -> ApprovalRequest:
        """Rebuild the domain :class:`ApprovalRequest` from a persisted row."""
        return ApprovalRequest(
            id=row.id,
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            gate_type=GateType(row.gate.value if hasattr(row.gate, "value") else row.gate),
            status=_gate_status(row.status),
            subject_type=row.subject_type or "workflow_run",
            subject_id=row.subject_id,
            workflow_run_id=row.workflow_run_id,
            agent_run_id=row.agent_run_id,
            task_id=row.task_id,
            required_approvals=row.required_approvals,
            risk_level=cast(RiskLevel, row.risk_level),
            title=row.summary,
            gate_payload=dict(row.payload or {}),
            context_ref=row.context_ref,
            requested_by=uuid.UUID(row.requested_by) if row.requested_by else None,
            requested_actor=row.requested_actor,
            escalated=row.escalated,
            decision_note=row.decision_reason,
            resolver_user_id=row.decided_by_id,
            expires_at=_aware(row.expires_at),
            requested_at=_aware(row.created_at),
            resolved_at=_aware(row.decided_at),
        )

    def _decision_to_domain(self, row: ApprovalDecisionRow) -> ApprovalDecisionRecord:
        return ApprovalDecisionRecord(
            approval_request_id=row.approval_request_id,
            approver_user_id=row.approver_user_id,
            decision=ApprovalAction(row.decision),
            note=row.note,
            created_at=_aware(row.created_at),
        )

    # ------------------------------------------------------------------ #
    # ApprovalRepository protocol                                         #
    # ------------------------------------------------------------------ #

    async def add(self, request: ApprovalRequest) -> ApprovalRequest:
        stored = request.model_copy(deep=True)
        if stored.requested_at is None:
            stored.requested_at = datetime.now(UTC)
        with self._sf() as session:
            row = ApprovalRequestRow(id=stored.id)
            self._apply(row, stored)
            session.add(row)
            session.commit()
        return stored.model_copy(deep=True)

    async def get(
        self, approval_id: uuid.UUID, *, workspace_id: uuid.UUID
    ) -> ApprovalRequest | None:
        with self._sf() as session:
            row = session.get(ApprovalRequestRow, approval_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return self._to_domain(row)

    async def find_pending(
        self,
        *,
        workspace_id: uuid.UUID,
        subject_type: str,
        subject_id: uuid.UUID | None,
        gate_type: GateType,
    ) -> ApprovalRequest | None:
        if subject_id is None:
            return None
        with self._sf() as session:
            row = session.scalars(
                select(ApprovalRequestRow)
                .where(
                    ApprovalRequestRow.workspace_id == workspace_id,
                    ApprovalRequestRow.status == _db_status(GateStatus.PENDING),
                    ApprovalRequestRow.subject_type == subject_type,
                    ApprovalRequestRow.subject_id == subject_id,
                    ApprovalRequestRow.gate == gate_type,
                )
                .limit(1)
            ).first()
            return self._to_domain(row) if row is not None else None

    async def list(
        self,
        *,
        workspace_id: uuid.UUID,
        status: GateStatus | None = None,
        gate_type: GateType | None = None,
        project_id: uuid.UUID | None = None,
    ) -> builtins.list[ApprovalRequest]:
        with self._sf() as session:
            stmt = select(ApprovalRequestRow).where(ApprovalRequestRow.workspace_id == workspace_id)
            if status is not None:
                stmt = stmt.where(ApprovalRequestRow.status == _db_status(status))
            if gate_type is not None:
                stmt = stmt.where(ApprovalRequestRow.gate == gate_type)
            if project_id is not None:
                stmt = stmt.where(ApprovalRequestRow.project_id == project_id)
            # Deterministic insertion order (``created_at`` == ``requested_at``);
            # the service re-sorts the inbox by risk afterwards.
            stmt = stmt.order_by(ApprovalRequestRow.created_at.asc(), ApprovalRequestRow.id.asc())
            rows = session.scalars(stmt).all()
            return [self._to_domain(r) for r in rows]

    async def update(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._sf() as session:
            row = session.get(ApprovalRequestRow, request.id)
            if row is None:
                raise ApprovalNotFoundError(request.id)
            self._apply(row, request)
            session.commit()
        return request.model_copy(deep=True)

    async def add_decision(self, record: ApprovalDecisionRecord) -> ApprovalDecisionRecord:
        stored = record.model_copy(deep=True)
        if stored.created_at is None:
            stored.created_at = datetime.now(UTC)
        with self._sf() as session:
            parent = session.get(ApprovalRequestRow, stored.approval_request_id)
            if parent is None:
                raise ApprovalNotFoundError(stored.approval_request_id)
            session.add(
                ApprovalDecisionRow(
                    workspace_id=parent.workspace_id,
                    approval_request_id=stored.approval_request_id,
                    approver_user_id=stored.approver_user_id,
                    decision=stored.decision.value,
                    note=stored.note,
                    created_at=stored.created_at,
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:  # one-vote-per-approver unique
                session.rollback()
                raise DuplicateDecisionError(
                    stored.approval_request_id, stored.approver_user_id
                ) from exc
        return stored.model_copy(deep=True)

    async def decisions_for(self, approval_id: uuid.UUID) -> builtins.list[ApprovalDecisionRecord]:
        with self._sf() as session:
            rows = session.scalars(
                select(ApprovalDecisionRow)
                .where(ApprovalDecisionRow.approval_request_id == approval_id)
                .order_by(ApprovalDecisionRow.created_at.asc(), ApprovalDecisionRow.id.asc())
            ).all()
            return [self._decision_to_domain(r) for r in rows]
