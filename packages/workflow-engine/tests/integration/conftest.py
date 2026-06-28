"""DB fixtures for the F28 editor integration tests (real Postgres)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

import forge_db.models  # noqa: F401 - registers all tables on Base.metadata
from forge_db.base import Base
from forge_db.models.workspace import User, Workspace


@pytest.fixture
def db_session(pg_engine: Engine) -> Iterator[Session]:
    """A session against a freshly-created schema, torn down after the test."""
    Base.metadata.create_all(pg_engine)
    session = Session(pg_engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(pg_engine)


def make_workspace(session: Session) -> uuid.UUID:
    """Insert a workspace and return its id (FK target for definitions)."""
    workspace = Workspace(
        id=uuid.uuid4(),
        name=f"ws-{uuid.uuid4().hex[:8]}",
        slug=f"ws-{uuid.uuid4().hex}",
        settings={},
    )
    session.add(workspace)
    session.flush()
    return workspace.id


def make_user(session: Session, workspace_id: uuid.UUID) -> uuid.UUID:
    """Insert a user in ``workspace_id`` and return its id (FK target for actor)."""
    user = User(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        email=f"{uuid.uuid4().hex[:8]}@forge.test",
        name="Test Admin",
    )
    session.add(user)
    session.flush()
    return user.id
