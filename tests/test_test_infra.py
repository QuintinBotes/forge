"""Tests for the shared test infrastructure (plan Task 0.6).

Covers the root ``conftest`` helpers + Postgres fixtures and the registered
pytest markers. The live-Postgres path is exercised as a ``postgres``-marked
test that skips (parked) when no database is configured — the honest state in a
no-network sandbox.
"""

from __future__ import annotations

import conftest
import pytest


def test_resolve_test_database_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_TEST_DATABASE_URL", raising=False)
    assert conftest.resolve_test_database_url() is None

    monkeypatch.setenv("FORGE_TEST_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/db")
    assert conftest.resolve_test_database_url() == "postgresql+psycopg://u:p@h:5432/db"


def test_resolve_test_database_url_treats_empty_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_TEST_DATABASE_URL", "")
    assert conftest.resolve_test_database_url() is None


def test_postgres_and_integration_markers_registered(
    pytestconfig: pytest.Config,
) -> None:
    markers = "\n".join(pytestconfig.getini("markers"))
    assert "postgres" in markers
    assert "integration" in markers


@pytest.mark.postgres
def test_postgres_fixture_yields_url(postgres_url: str) -> None:
    # Runs only when Postgres is configured; otherwise skipped (parked).
    assert postgres_url
