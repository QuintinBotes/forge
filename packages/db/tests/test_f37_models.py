"""Postgres integration tests for the F37 auth-secrets models (AC1 substance).

Exercises the real Postgres code paths: the ``platform_api_key`` unique
``key_id``, the ``agent_runner → expires_at`` CHECK, the ``oauth_account``
global ``(provider, provider_subject)`` uniqueness, enum round-trips, and the
workspace-CASCADE tenancy. Uses the shared ``pg_engine`` fixture; parks
without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    OAuthAccount,
    OAuthProvider,
    PlatformAPIKey,
    PlatformKeyKind,
    User,
    UserRole,
    Workspace,
)

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    user = User(workspace_id=ws.id, email=f"admin-{uuid.uuid4().hex[:6]}@acme.dev")
    session.add(user)
    session.flush()
    return ws.id, user.id


def _key(ws_id: uuid.UUID, user_id: uuid.UUID | None = None, **overrides) -> PlatformAPIKey:
    defaults: dict = {
        "workspace_id": ws_id,
        "name": "ci deploy bot",
        "key_id": uuid.uuid4().hex[:8],
        "key_hash": "a" * 64,
        "key_prefix": "forge_svc_abcd1234…wxyz",
        "kind": PlatformKeyKind.SERVICE,
        "role": UserRole.AGENT_RUNNER,
        "created_by": user_id,
    }
    defaults.update(overrides)
    return PlatformAPIKey(**defaults)


def test_platform_key_round_trip_and_enums(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, user_id = _seed(session)
        session.add(_key(ws_id, user_id))
        session.commit()
        row = session.scalars(select(PlatformAPIKey)).one()
        assert row.kind is PlatformKeyKind.SERVICE
        assert row.role is UserRole.AGENT_RUNNER
        assert row.revoked_at is None and row.expires_at is None
        assert "redacted" in repr(row) and "a" * 64 not in repr(row)


def test_platform_key_key_id_unique(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed(session)
        session.add(_key(ws_id, key_id="dupdupid"))
        session.commit()
        session.add(_key(ws_id, key_id="dupdupid"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_agent_runner_requires_expiry_check(factory: sessionmaker[Session]) -> None:
    """CHECK: kind='agent_runner' → expires_at IS NOT NULL (AC17 precondition)."""
    with factory() as session:
        ws_id, _ = _seed(session)
        session.add(_key(ws_id, kind=PlatformKeyKind.AGENT_RUNNER, expires_at=None))
        with pytest.raises(IntegrityError):
            session.commit()
    with factory() as session:
        ws_id, _ = _seed(session)
        session.add(
            _key(
                ws_id,
                kind=PlatformKeyKind.AGENT_RUNNER,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.commit()  # with expiry it is accepted


def test_oauth_account_global_identity_unique(factory: sessionmaker[Session]) -> None:
    """One external identity → one Forge user, globally (across workspaces)."""
    with factory() as session:
        ws1, user1 = _seed(session)
        ws2, user2 = _seed(session)
        session.add(
            OAuthAccount(
                workspace_id=ws1,
                user_id=user1,
                provider=OAuthProvider.GITHUB,
                provider_subject="gh-123",
                email="alice@acme.dev",
            )
        )
        session.commit()
        session.add(
            OAuthAccount(
                workspace_id=ws2,
                user_id=user2,
                provider=OAuthProvider.GITHUB,
                provider_subject="gh-123",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_multi_provider_linking_same_user(factory: sessionmaker[Session]) -> None:
    """AC10 substrate: two providers may point at the same Forge user."""
    with factory() as session:
        ws_id, user_id = _seed(session)
        session.add_all(
            [
                OAuthAccount(
                    workspace_id=ws_id,
                    user_id=user_id,
                    provider=OAuthProvider.GITHUB,
                    provider_subject="gh-9",
                ),
                OAuthAccount(
                    workspace_id=ws_id,
                    user_id=user_id,
                    provider=OAuthProvider.GOOGLE,
                    provider_subject="goog-9",
                ),
            ]
        )
        session.commit()
        rows = session.scalars(select(OAuthAccount)).all()
        assert {r.provider for r in rows} == {OAuthProvider.GITHUB, OAuthProvider.GOOGLE}
        assert all(r.user_id == user_id for r in rows)
        assert all(r.linked_at is not None for r in rows)


def test_workspace_cascade_deletes_f37_rows(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, user_id = _seed(session)
        session.add(_key(ws_id, user_id))
        session.add(
            OAuthAccount(
                workspace_id=ws_id,
                user_id=user_id,
                provider=OAuthProvider.GITLAB,
                provider_subject="gl-1",
            )
        )
        session.commit()
        ws = session.get(Workspace, ws_id)
        session.delete(ws)
        session.commit()
        assert session.scalars(select(PlatformAPIKey)).all() == []
        assert session.scalars(select(OAuthAccount)).all() == []
