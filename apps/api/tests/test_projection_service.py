"""Unit tests for the F23 projection-repository composition root (env-flagged).

Hermetic (no Postgres): proves ``build_projection_repository`` defaults to the
in-memory store and swaps to the DB-backed repository only when
``FORGE_PROJECTION_BACKEND=db``. The ``db`` branch stubs the shared session
factory so no engine/connection is opened.
"""

from __future__ import annotations

import pytest

import forge_api.db as db
from forge_api.services.projection_repository_db import SqlAlchemyProjectionRepository
from forge_api.services.projection_service import build_projection_repository
from forge_api.settings import get_settings
from forge_spec import InMemoryProjectionRepository


@pytest.fixture(autouse=True)
def _reset_settings() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_to_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_PROJECTION_BACKEND", raising=False)
    get_settings.cache_clear()
    assert isinstance(build_projection_repository(), InMemoryProjectionRepository)


def test_memory_flag_selects_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_PROJECTION_BACKEND", "memory")
    get_settings.cache_clear()
    assert isinstance(build_projection_repository(), InMemoryProjectionRepository)


def test_db_flag_selects_sqlalchemy_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(db, "get_session_factory", lambda: sentinel)
    monkeypatch.setenv("FORGE_PROJECTION_BACKEND", "db")
    get_settings.cache_clear()

    repo = build_projection_repository()
    assert isinstance(repo, SqlAlchemyProjectionRepository)
    assert repo._sf is sentinel
