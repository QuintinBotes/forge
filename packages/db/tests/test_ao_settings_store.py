"""Postgres integration tests for the ao-settings-api ``ao_workspace_settings``
model + :class:`~forge_db.ao_settings.SqlAoSettingsStore`.

Exercises the real constraints: the unique ``workspace_id`` index enforcing at
most one settings row per workspace, the insert-then-partial-update upsert
path, the ``clear_*`` reset-to-default flags, and workspace CASCADE delete.
Uses the shared ``pg_engine`` fixture; parks without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_db.ao_settings import SqlAoSettingsStore
from forge_db.base import Base
from forge_db.models import AoWorkspaceSettings, Workspace

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed(session: Session) -> uuid.UUID:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    return ws.id


def test_get_settings_returns_none_when_absent(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id = _seed(session)
        store = SqlAoSettingsStore(session)
        assert store.get_settings(ws_id) is None


def test_upsert_creates_row_with_defaults(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id = _seed(session)
        store = SqlAoSettingsStore(session)
        settings = store.upsert_settings(ws_id)
        assert settings.workspace_id == ws_id
        assert settings.auto_route is True
        assert settings.tier_model_overrides == {}
        assert settings.junior_max is None
        assert settings.medior_max is None


def test_upsert_sets_and_then_partially_updates(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id = _seed(session)
        store = SqlAoSettingsStore(session)
        store.upsert_settings(
            ws_id,
            auto_route=False,
            tier_model_overrides={"anthropic": {"junior": "claude-haiku-4-5"}},
            junior_max=4,
            medior_max=12,
        )
        # Partial update: only auto_route changes; the rest is left alone.
        updated = store.upsert_settings(ws_id, auto_route=True)
        assert updated.auto_route is True
        assert updated.tier_model_overrides == {"anthropic": {"junior": "claude-haiku-4-5"}}
        assert updated.junior_max == 4
        assert updated.medior_max == 12


def test_clear_junior_max_resets_to_none(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id = _seed(session)
        store = SqlAoSettingsStore(session)
        store.upsert_settings(ws_id, junior_max=4, medior_max=12)
        cleared = store.upsert_settings(ws_id, clear_junior_max=True)
        assert cleared.junior_max is None
        assert cleared.medior_max == 12


def test_only_one_row_per_workspace(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id = _seed(session)
        store = SqlAoSettingsStore(session)
        store.upsert_settings(ws_id, auto_route=False)
        store.upsert_settings(ws_id, auto_route=True)
        rows = session.scalars(
            select(AoWorkspaceSettings).where(AoWorkspaceSettings.workspace_id == ws_id)
        ).all()
        assert len(rows) == 1


def test_workspace_cascade_delete_removes_settings(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id = _seed(session)
        store = SqlAoSettingsStore(session)
        store.upsert_settings(ws_id, auto_route=False)
        session.execute(Workspace.__table__.delete().where(Workspace.id == ws_id))
        session.commit()
        assert store.get_settings(ws_id) is None
