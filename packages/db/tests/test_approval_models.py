"""F36 approval-framework model tests (hermetic SQLite).

Covers the generalized ``approval_request`` columns, the ``uq_pending_gate``
partial-unique (one open gate of a type per subject), the one-vote-per-approver
unique on ``approval_decision``, and the single-active ``uq_active_override``
partial-unique on ``policy_override_grant``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    ApprovalStatus,
    PolicyOverrideGrant,
    Project,
    Task,
    User,
    WorkflowRun,
    Workspace,
)
from forge_db.models.enums import TaskKind, TaskStatus

WS = uuid.uuid4()
USER = uuid.uuid4()
PROJECT = uuid.uuid4()


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
        s.add(User(id=USER, workspace_id=WS, email="reviewer@acme.dev", name="Rev"))
        s.add(Project(id=PROJECT, workspace_id=WS, name="Forge", key="FRG"))
        s.commit()
        yield s


def _workflow_run(session: Session) -> WorkflowRun:
    task = Task(
        workspace_id=WS,
        project_id=PROJECT,
        key=f"FRG-{uuid.uuid4().hex[:6]}",
        title="Feature",
        kind=TaskKind.FEATURE,
        status=TaskStatus.READY,
    )
    session.add(task)
    session.flush()
    run = WorkflowRun(workspace_id=WS, task_id=task.id)
    session.add(run)
    session.flush()
    return run


def _gate(
    session: Session,
    run: WorkflowRun,
    gate: ApprovalGate = ApprovalGate.PR,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    subject_id: uuid.UUID | None = None,
) -> ApprovalRequest:
    request = ApprovalRequest(
        workspace_id=WS,
        workflow_run_id=run.id,
        gate=gate,
        status=status,
        subject_type="workflow_run",
        subject_id=subject_id or run.id,
        risk_level="warning",
        required_approvals=1,
        payload={"summary": "generalized gate"},
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    session.add(request)
    return request


def test_generalized_columns_round_trip(session: Session) -> None:
    run = _workflow_run(session)
    request = _gate(session, run)
    session.commit()

    stored = session.scalars(select(ApprovalRequest)).one()
    assert stored.subject_type == "workflow_run"
    assert stored.subject_id == run.id
    assert stored.risk_level == "warning"
    assert stored.required_approvals == 1
    assert stored.expires_at is not None
    assert stored.status is ApprovalStatus.PENDING
    assert request.id == stored.id


def test_expired_status_value_storable(session: Session) -> None:
    """F36: the additive ``expired`` enum value persists (no CHECK constraint)."""
    run = _workflow_run(session)
    _gate(session, run, status=ApprovalStatus.EXPIRED)
    session.commit()
    stored = session.scalars(select(ApprovalRequest)).one()
    assert stored.status is ApprovalStatus.EXPIRED


def test_uq_pending_gate_blocks_duplicate_open_gate(session: Session) -> None:
    """AC#2 (DB layer): one open gate of a type per subject."""
    run = _workflow_run(session)
    _gate(session, run)
    session.commit()
    _gate(session, run)  # same subject + gate type, still pending
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # A RESOLVED gate does not collide with a new pending one (partial index).
    resolved = session.scalars(select(ApprovalRequest)).one()
    resolved.status = ApprovalStatus.APPROVED
    session.commit()
    _gate(session, run)
    session.commit()
    assert len(session.scalars(select(ApprovalRequest)).all()) == 2


def test_uq_pending_gate_allows_different_gate_types(session: Session) -> None:
    run = _workflow_run(session)
    _gate(session, run, gate=ApprovalGate.PR)
    _gate(session, run, gate=ApprovalGate.SPEC)
    session.commit()
    assert len(session.scalars(select(ApprovalRequest)).all()) == 2


def test_one_vote_per_approver(session: Session) -> None:
    """F36 §3.1: ``approval_decision`` unique per (request, approver)."""
    run = _workflow_run(session)
    request = _gate(session, run)
    session.commit()

    session.add(
        ApprovalDecision(
            workspace_id=WS,
            approval_request_id=request.id,
            approver_user_id=USER,
            decision="approve",
            note="lgtm",
        )
    )
    session.commit()
    session.add(
        ApprovalDecision(
            workspace_id=WS,
            approval_request_id=request.id,
            approver_user_id=USER,
            decision="reject",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    votes = session.scalars(select(ApprovalDecision)).all()
    assert len(votes) == 1
    assert votes[0].approval_request.id == request.id


def test_decisions_cascade_with_request(session: Session) -> None:
    run = _workflow_run(session)
    request = _gate(session, run)
    session.commit()
    session.add(
        ApprovalDecision(
            workspace_id=WS,
            approval_request_id=request.id,
            approver_user_id=USER,
            decision="approve",
        )
    )
    session.commit()

    session.delete(session.get(ApprovalRequest, request.id))
    session.commit()
    assert session.scalars(select(ApprovalDecision)).all() == []


def test_uq_active_override_single_active_grant(session: Session) -> None:
    """AC#14 (DB layer): one ACTIVE grant per (agent_run, fingerprint)."""
    from forge_db.models import AgentRun

    agent_run = AgentRun(workspace_id=WS)
    session.add(agent_run)
    session.flush()

    def grant(consumed: bool = False) -> PolicyOverrideGrant:
        return PolicyOverrideGrant(
            workspace_id=WS,
            agent_run_id=agent_run.id,
            action_fingerprint="fp-1",
            granted_by=USER,
            consumed=consumed,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    session.add(grant())
    session.commit()
    session.add(grant())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # Once consumed, a fresh active grant may be minted again.
    active = session.scalars(select(PolicyOverrideGrant)).one()
    active.consumed = True
    session.commit()
    session.add(grant())
    session.commit()
    assert len(session.scalars(select(PolicyOverrideGrant)).all()) == 2
