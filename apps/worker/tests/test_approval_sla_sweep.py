"""F40-POL-GOVERNANCE — the beat SLA ladder (route / escalate / expire).

The historical beat only *expired* overdue gates. ``run_sla_sweep`` now routes an
OOO assignee to their delegate and escalates an over-SLA gate to admin before the
hard deadline, writing one immutable audit row per action.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_approval.delegation import DelegationDirectory, DelegationEntry
from forge_db.base import Base
from forge_db.models import (
    ApprovalGate,
    ApprovalRequest,
    ApprovalStatus,
    AuditLog,
    Workspace,
)
from forge_worker.tasks.approvals import run_sla_sweep

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ASSIGNEE = uuid.UUID("00000000-0000-0000-0000-00000000b001")
DELEGATE = uuid.UUID("00000000-0000-0000-0000-00000000b002")

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine, expire_on_commit=False, class_=Session
    )
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
        yield s


def _gate(
    session: Session,
    *,
    created_ago: int,
    expires_in: int,
    payload: dict | None = None,
) -> ApprovalRequest:
    gate = ApprovalRequest(
        workspace_id=WS,
        gate=ApprovalGate.PR,
        status=ApprovalStatus.PENDING,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        created_at=NOW - timedelta(seconds=created_ago),
        expires_at=NOW + timedelta(seconds=expires_in),
        payload=payload or {},
    )
    session.add(gate)
    session.commit()
    return gate


def _actions(session: Session, action: str) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.action == action)).all())


def test_over_sla_gate_escalates(session: Session) -> None:
    gate = _gate(session, created_ago=80, expires_in=20)  # 80% through a 100s window
    result = run_sla_sweep(session, now=NOW)
    assert (result.escalated, result.expired, result.routed) == (1, 0, 0)

    refreshed = session.get(ApprovalRequest, gate.id)
    assert refreshed.escalated is True
    assert refreshed.risk_level == "critical"
    assert refreshed.status is ApprovalStatus.PENDING
    assert len(_actions(session, "approval.escalated")) == 1


def test_ooo_assignee_routes_to_delegate(session: Session) -> None:
    gate = _gate(session, created_ago=80, expires_in=20, payload={"assignee": str(ASSIGNEE)})
    directory = DelegationDirectory(
        entries=[DelegationEntry(user_id=ASSIGNEE, delegate_id=DELEGATE)]
    )
    result = run_sla_sweep(session, now=NOW, delegation=directory)
    assert (result.routed, result.escalated, result.expired) == (1, 0, 0)

    refreshed = session.get(ApprovalRequest, gate.id)
    assert refreshed.payload["assignee"] == str(DELEGATE)
    assert refreshed.status is ApprovalStatus.PENDING
    assert len(_actions(session, "approval.routed_to_delegate")) == 1


def test_past_deadline_expires(session: Session) -> None:
    gate = _gate(session, created_ago=120, expires_in=-20)
    result = run_sla_sweep(session, now=NOW)
    assert (result.expired, result.escalated, result.routed) == (1, 0, 0)
    assert session.get(ApprovalRequest, gate.id).status is ApprovalStatus.EXPIRED
    assert len(_actions(session, "approval.expired")) == 1


def test_within_sla_untouched(session: Session) -> None:
    gate = _gate(session, created_ago=10, expires_in=90)
    assert run_sla_sweep(session, now=NOW).total == 0
    assert session.get(ApprovalRequest, gate.id).escalated is False


def test_escalation_is_idempotent(session: Session) -> None:
    _gate(session, created_ago=80, expires_in=20)
    assert run_sla_sweep(session, now=NOW).escalated == 1
    assert run_sla_sweep(session, now=NOW).total == 0
    assert len(_actions(session, "approval.escalated")) == 1


def test_gate_without_sla_is_skipped(session: Session) -> None:
    gate = ApprovalRequest(
        workspace_id=WS,
        gate=ApprovalGate.PR,
        status=ApprovalStatus.PENDING,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        created_at=NOW - timedelta(hours=5),
        expires_at=None,
    )
    session.add(gate)
    session.commit()
    assert run_sla_sweep(session, now=NOW).total == 0
