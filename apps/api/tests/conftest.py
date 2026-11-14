"""Shared fixtures for the Forge API test-suite.

Phase-2 bug fix (Task 2.3): real authentication is now enforced on every feature
router — ``get_current_principal`` verifies an API key instead of returning a
hardcoded admin (see ``forge_api.deps``). Feature-router tests do not mint real
keys; they inject a deterministic authenticated :class:`Principal` via
``app.dependency_overrides`` using the :func:`authenticate_app` helper, which is
the idiomatic FastAPI way to bypass authentication in handler-focused tests.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session

# HARD-13: the auth service fails closed without a master key (no silent
# ephemeral fallback). Provide a stable, obviously-fake test key so the default
# ``AuthService()`` constructs deterministically; tests that exercise the
# missing-key / dev-insecure paths override this via ``monkeypatch``.
os.environ.setdefault("FORGE_SECRET_KEY", "test-forge-master-secret-0123456789")

from forge_api.deps import Principal, get_current_principal
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import Workspace

#: Deterministic identities used by tests (the knowledge tests seed a workspace
#: with this id so the principal-scoped search returns the seeded source).
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def make_test_principal(
    *,
    role: UserRole = UserRole.ADMIN,
    workspace_id: uuid.UUID = TEST_WORKSPACE_ID,
) -> Principal:
    """Build a deterministic authenticated principal for handler tests."""
    return Principal(
        user_id=TEST_USER_ID,
        workspace_id=workspace_id,
        role=role,
        email="test-principal@forge.local",
        auth_method="test",
        scopes=["*"],
    )


@pytest.fixture
def authenticate_app() -> Callable[..., FastAPI]:
    """Return a helper installing an authenticated principal override on an app.

    Usage in a ``client`` fixture::

        app = create_app()
        authenticate_app(app)  # now requests resolve as an admin in TEST_WORKSPACE
    """

    def _apply(app: FastAPI, principal: Principal | None = None) -> FastAPI:
        resolved = principal or make_test_principal()
        app.dependency_overrides[get_current_principal] = lambda: resolved
        return app

    return _apply


@pytest.fixture
def agent_session() -> Iterator[Session]:
    """Real run persistence without a broker or external database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Workspace(id=TEST_WORKSPACE_ID, name="Agent tests", slug="agent-tests"))
        session.commit()
        yield session
    engine.dispose()
