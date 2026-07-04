"""F30 worker test: expired-grant purge (AC13).

Hermetic in-memory SQLite. Seeds expired + live grants; asserts the purge
deletes only the expired ones, writes one ``role_grant.expired`` audit event per
deleted grant, and is idempotent (a re-run deletes nothing and emits no further
events). Also asserts the Celery task + Beat schedule are registered.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.authz import PrincipalType, ScopeType
from forge_contracts.enums import UserRole
from forge_db.base import Base
from forge_db.models import AuditLog, RoleGrant, Workspace
from forge_worker.beat import AUTHZ_PURGE_TASK, BEAT_SCHEDULE
from forge_worker.celery_app import celery_app
from forge_worker.tasks.authz import PURGE_EXPIRED_GRANTS_TASK, run_purge

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def _grant(expires_at: datetime | None) -> RoleGrant:
    return RoleGrant(
        workspace_id=WS,
        principal_type=PrincipalType.USER,
        principal_id=uuid.uuid4(),
        scope_type=ScopeType.PROJECT,
        scope_id=uuid.uuid4(),
        role=UserRole.AGENT_RUNNER,
        expires_at=expires_at,
    )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
        yield s


def test_purge_deletes_expired_and_audits(session: Session) -> None:
    now = datetime.now(UTC)
    expired1 = _grant(now - timedelta(hours=1))
    expired2 = _grant(now - timedelta(minutes=5))
    live = _grant(now + timedelta(hours=1))
    permanent = _grant(None)
    session.add_all([expired1, expired2, live, permanent])
    session.commit()

    deleted = run_purge(session, now=now)
    assert deleted == 2

    remaining = session.scalars(select(RoleGrant)).all()
    assert {g.id for g in remaining} == {live.id, permanent.id}

    events = session.scalars(select(AuditLog).where(AuditLog.action == "role_grant.expired")).all()
    assert len(events) == 2
    assert all(e.actor_type == "system" for e in events)


def test_purge_is_idempotent(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(_grant(now - timedelta(hours=1)))
    session.commit()

    assert run_purge(session, now=now) == 1
    # Re-run: nothing left to purge, no second event.
    assert run_purge(session, now=now) == 0
    events = session.scalars(select(AuditLog).where(AuditLog.action == "role_grant.expired")).all()
    assert len(events) == 1


def test_task_and_beat_registered() -> None:
    assert PURGE_EXPIRED_GRANTS_TASK in celery_app.tasks
    assert AUTHZ_PURGE_TASK == PURGE_EXPIRED_GRANTS_TASK
    assert any(entry["task"] == AUTHZ_PURGE_TASK for entry in BEAT_SCHEDULE.values())
