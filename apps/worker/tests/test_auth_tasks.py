"""F37 worker test: expired platform-key purge (AC17).

Hermetic in-memory SQLite. Seeds expired + live + already-revoked keys;
asserts the purge revokes only the expired-and-unrevoked ones (``revoked_at =
expires_at``, rows kept), writes one ``apikey.expired`` audit event per key
with secret-free metadata, and is idempotent. Also asserts the Celery task +
Beat schedule are registered.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import AuditLog, PlatformAPIKey, PlatformKeyKind, UserRole, Workspace
from forge_worker.beat import AUTH_PURGE_KEYS_TASK, BEAT_SCHEDULE
from forge_worker.celery_app import celery_app
from forge_worker.tasks.auth import PURGE_EXPIRED_KEYS_TASK, run_purge_expired_keys

WS = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def _key(
    *,
    expires_at: datetime | None,
    revoked_at: datetime | None = None,
    kind: PlatformKeyKind = PlatformKeyKind.SERVICE,
) -> PlatformAPIKey:
    key_id = uuid.uuid4().hex[:8]
    return PlatformAPIKey(
        workspace_id=WS,
        name="agent run key",
        key_id=key_id,
        key_hash="f" * 64,
        key_prefix=f"forge_agt_{key_id}…wxyz",
        kind=kind,
        role=UserRole.AGENT_RUNNER,
        expires_at=expires_at,
        revoked_at=revoked_at,
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


def test_purge_revokes_expired_and_audits(session: Session) -> None:
    now = datetime.now(UTC)
    expired1 = _key(expires_at=now - timedelta(hours=1), kind=PlatformKeyKind.AGENT_RUNNER)
    expired2 = _key(expires_at=now - timedelta(minutes=5))
    live = _key(expires_at=now + timedelta(hours=1), kind=PlatformKeyKind.AGENT_RUNNER)
    everlasting = _key(expires_at=None)
    already_revoked = _key(
        expires_at=now - timedelta(days=1), revoked_at=now - timedelta(days=1)
    )
    session.add_all([expired1, expired2, live, everlasting, already_revoked])
    session.commit()

    assert run_purge_expired_keys(session, now=now) == 2

    # Rows are kept (audit trail), only revoked_at is set — to the expiry time.
    assert {k.id for k in session.scalars(select(PlatformAPIKey)).all()} >= {
        expired1.id,
        expired2.id,
    }
    assert session.get(PlatformAPIKey, expired1.id).revoked_at == expired1.expires_at
    assert session.get(PlatformAPIKey, live.id).revoked_at is None
    assert session.get(PlatformAPIKey, everlasting.id).revoked_at is None

    events = session.scalars(select(AuditLog).where(AuditLog.action == "apikey.expired")).all()
    assert len(events) == 2
    assert {e.target_id for e in events} == {expired1.id, expired2.id}
    assert all(e.actor_type == "system" for e in events)
    # Metadata never carries the hash or a token (AC16 discipline).
    for event in events:
        assert "f" * 64 not in str(event.before)
        assert event.before["key_prefix"].startswith("forge_agt_")


def test_purge_is_idempotent(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(_key(expires_at=now - timedelta(hours=1)))
    session.commit()

    assert run_purge_expired_keys(session, now=now) == 1
    assert run_purge_expired_keys(session, now=now) == 0
    events = session.scalars(select(AuditLog).where(AuditLog.action == "apikey.expired")).all()
    assert len(events) == 1  # no second event on re-run


def test_task_and_beat_registered() -> None:
    assert PURGE_EXPIRED_KEYS_TASK == AUTH_PURGE_KEYS_TASK
    assert PURGE_EXPIRED_KEYS_TASK in celery_app.tasks
    assert BEAT_SCHEDULE["auth-purge-expired-keys"]["task"] == AUTH_PURGE_KEYS_TASK
