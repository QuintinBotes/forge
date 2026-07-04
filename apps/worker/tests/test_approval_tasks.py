"""F36 worker tests: approval-SLA sweep + single-use override-grant consume.

Hermetic in-memory SQLite (the authz-purge test pattern). Covers: overdue
pending gates flip to ``expired`` with one audit row each (idempotent re-run);
already-resolved / future-dated gates are untouched; the consume path is
single-use and denies on expiry, fingerprint mismatch, and unknown runs; the
Celery tasks + beat entry are registered.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    AgentRun,
    ApprovalGate,
    ApprovalRequest,
    ApprovalStatus,
    AuditLog,
    PolicyOverrideGrant,
    User,
    Workspace,
)
from forge_worker.beat import APPROVAL_EXPIRE_TASK, BEAT_SCHEDULE
from forge_worker.celery_app import celery_app
from forge_worker.tasks.approvals import (
    CONSUME_OVERRIDE_GRANT_TASK,
    EXPIRE_APPROVALS_TASK,
    run_consume_grant,
    run_expire_sweep,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


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
        s.add(User(id=ADMIN, workspace_id=WS, email="admin@acme.dev", name="Admin"))
        s.commit()
        yield s


def _gate(
    status: ApprovalStatus = ApprovalStatus.PENDING,
    expires_at: datetime | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        workspace_id=WS,
        gate=ApprovalGate.PR,
        status=status,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        expires_at=expires_at,
    )


def _grant(
    session: Session,
    fingerprint: str = "fp-1",
    expires_in: timedelta = timedelta(minutes=15),
    consumed: bool = False,
) -> tuple[uuid.UUID, PolicyOverrideGrant]:
    agent_run = AgentRun(workspace_id=WS)
    session.add(agent_run)
    session.flush()
    grant = PolicyOverrideGrant(
        workspace_id=WS,
        agent_run_id=agent_run.id,
        action_fingerprint=fingerprint,
        granted_by=ADMIN,
        consumed=consumed,
        expires_at=datetime.now(UTC) + expires_in,
    )
    session.add(grant)
    session.commit()
    return agent_run.id, grant


# --------------------------------------------------------------------------- #
# expire sweep                                                                 #
# --------------------------------------------------------------------------- #


def test_sweep_expires_only_overdue_pending(session: Session) -> None:
    now = datetime.now(UTC)
    overdue = _gate(expires_at=now - timedelta(minutes=5))
    fresh = _gate(expires_at=now + timedelta(hours=1))
    no_sla = _gate(expires_at=None)
    resolved = _gate(status=ApprovalStatus.APPROVED, expires_at=now - timedelta(hours=1))
    session.add_all([overdue, fresh, no_sla, resolved])
    session.commit()

    swept = run_expire_sweep(session, now=now)
    assert swept == 1

    assert session.get(ApprovalRequest, overdue.id).status is ApprovalStatus.EXPIRED
    assert session.get(ApprovalRequest, fresh.id).status is ApprovalStatus.PENDING
    assert session.get(ApprovalRequest, no_sla.id).status is ApprovalStatus.PENDING
    assert session.get(ApprovalRequest, resolved.id).status is ApprovalStatus.APPROVED

    events = session.scalars(
        select(AuditLog).where(AuditLog.action == "approval.expired")
    ).all()
    assert len(events) == 1
    assert events[0].target_id == overdue.id
    assert events[0].details["follow_up_state"] == "needs_human_input"


def test_sweep_is_idempotent(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(_gate(expires_at=now - timedelta(minutes=5)))
    session.commit()
    assert run_expire_sweep(session, now=now) == 1
    assert run_expire_sweep(session, now=now) == 0
    events = session.scalars(
        select(AuditLog).where(AuditLog.action == "approval.expired")
    ).all()
    assert len(events) == 1


# --------------------------------------------------------------------------- #
# grant consumption (the frozen PolicyOverrideGate.consume contract)           #
# --------------------------------------------------------------------------- #


def test_consume_grant_single_use(session: Session) -> None:
    """AC#14: True exactly once, then False; audit row on the consume."""
    run_id, _ = _grant(session)
    assert run_consume_grant(session, agent_run_id=run_id, action_fingerprint="fp-1") is True
    assert run_consume_grant(session, agent_run_id=run_id, action_fingerprint="fp-1") is False
    events = session.scalars(
        select(AuditLog).where(AuditLog.action == "policy_override.consumed")
    ).all()
    assert len(events) == 1


def test_consume_expired_grant_denies(session: Session) -> None:
    run_id, grant = _grant(session, expires_in=timedelta(minutes=-1))
    assert run_consume_grant(session, agent_run_id=run_id, action_fingerprint="fp-1") is False
    assert session.get(PolicyOverrideGrant, grant.id).consumed is False


def test_consume_fingerprint_mismatch_denies(session: Session) -> None:
    run_id, grant = _grant(session, fingerprint="fp-real")
    assert (
        run_consume_grant(session, agent_run_id=run_id, action_fingerprint="fp-other")
        is False
    )
    # The real grant is untouched by the mismatch and still consumable once.
    assert session.get(PolicyOverrideGrant, grant.id).consumed is False
    assert (
        run_consume_grant(session, agent_run_id=run_id, action_fingerprint="fp-real") is True
    )


def test_consume_unknown_run_denies(session: Session) -> None:
    _grant(session)
    assert (
        run_consume_grant(
            session, agent_run_id=uuid.uuid4(), action_fingerprint="fp-1"
        )
        is False
    )


# --------------------------------------------------------------------------- #
# registration                                                                 #
# --------------------------------------------------------------------------- #


def test_tasks_and_beat_registered() -> None:
    assert EXPIRE_APPROVALS_TASK in celery_app.tasks
    assert CONSUME_OVERRIDE_GRANT_TASK in celery_app.tasks
    entry = BEAT_SCHEDULE["approvals-expire-pending"]
    assert entry["task"] == APPROVAL_EXPIRE_TASK
