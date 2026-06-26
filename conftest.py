"""Root pytest configuration and shared test fixtures (plan Task 0.6).

This is the SHARED TEST SUBSTRATE every package's DB/integration tests build on.
Unit tests stay hermetic (no live services). Tests that genuinely need Postgres
request the :func:`postgres_url` / :func:`pg_engine` fixtures, which resolve a
database in this order:

1. ``FORGE_TEST_DATABASE_URL`` if set (CI uses a Postgres service container);
2. a ``testcontainers`` pgvector container if the extra is installed; otherwise
3. the test is **skipped** with a clear PARKED reason — never faked.

This keeps the unit suite green in a no-network sandbox while letting Phase 2
run the same tests against real Postgres (docker compose / CI service).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

PG_SKIP_REASON = (
    "PARKED: no Postgres available — set FORGE_TEST_DATABASE_URL to a "
    "pgvector-enabled Postgres, or install the 'testcontainers' extra. "
    "DB/integration tests run in Phase 2 against docker compose / CI services."
)


def resolve_test_database_url() -> str | None:
    """Return an explicitly configured test database URL, or ``None``."""
    return os.environ.get("FORGE_TEST_DATABASE_URL") or None


def _maybe_start_testcontainer() -> Any | None:
    """Start a pgvector Postgres testcontainer if the extra is installed."""
    try:
        from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return None
    container = PostgresContainer("pgvector/pgvector:pg16", driver="psycopg")
    container.start()
    return container


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Yield a usable Postgres URL, or skip (parked) when none is available."""
    configured = resolve_test_database_url()
    if configured:
        yield configured
        return

    container = _maybe_start_testcontainer()
    if container is None:
        pytest.skip(PG_SKIP_REASON)

    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
def pg_engine(postgres_url: str) -> Iterator[Engine]:
    """A SQLAlchemy engine bound to a pgvector-enabled Postgres."""
    from sqlalchemy import create_engine, text

    engine = create_engine(postgres_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def pg_connection(pg_engine: Engine) -> Iterator[Connection]:
    """A transaction-wrapped connection; rolled back after each test."""
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()
