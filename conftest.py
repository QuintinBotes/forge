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
    """A SQLAlchemy engine bound to a pgvector-enabled Postgres.

    Each test session is isolated in its **own private schema** (``forge_test_*``)
    rather than sharing ``public``. Every connection this engine opens routes its
    ``search_path`` to that schema (libpq ``options``), so the per-module
    ``Base.metadata.create_all``/``drop_all`` every DB test performs lands in the
    session-private schema and is torn down with it.

    Why a private schema instead of resetting ``public``: the supported test
    Postgres is a long-lived container (``docker start forge-test-pg``) or a CI
    service reused across runs, and **multiple pytest sessions can run against it
    concurrently or overlap** (parallel gate invocations, lingering connections
    from a prior run). The old strategy reset ``public`` once at session start;
    a concurrent session's reset then wiped another session's tables mid-run, and
    their per-module ``create_all`` raced on ``public`` — surfacing as
    ``pg_type_typname_nsp_index`` ``UniqueViolation`` on ``(workspace, ...)`` plus
    cascading ``relation ... does not exist`` errors (the flaky-RED whole-suite
    gate). Giving every session its own schema removes all cross-session sharing,
    so the suite is deterministic regardless of what else touches the database.

    ``public`` is kept on the ``search_path`` only so the shared, database-global
    ``vector`` extension type resolves; it is created once and never dropped. This
    is safe because every Postgres test owns its schema via ``create_all``/
    ``drop_all`` (or transactional ``pg_connection``); none relies on a
    pre-existing schema, and the configured URL is a disposable test database,
    never production.
    """
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

    schema = f"forge_test_{uuid.uuid4().hex}"

    # Bootstrap on the default search_path to provision the private schema and the
    # shared pgvector extension, then dispose — the test engine below routes every
    # connection into the private schema instead.
    bootstrap = create_engine(postgres_url, future=True)
    try:
        # pgvector lives in `public` and is shared by every session (created once,
        # left in place). Tolerate the rare race where concurrent sessions both
        # try to create it for the first time.
        try:
            with bootstrap.begin() as conn:
                conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
                )
        except (IntegrityError, OperationalError, ProgrammingError):
            pass
        with bootstrap.begin() as conn:
            # Self-heal: a reused DB may carry leftover Forge tables in `public`
            # from a pre-isolation or crashed run. No session keeps tables in
            # public anymore, so drop any stragglers — otherwise they would shadow
            # a session schema's create_all (has_table sees the whole search_path).
            conn.execute(
                text(
                    "DO $$ DECLARE r record; BEGIN "
                    "FOR r IN SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' LOOP "
                    "EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', "
                    "r.tablename); END LOOP; END $$;"
                )
            )
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    finally:
        bootstrap.dispose()

    url = make_url(postgres_url)
    query = dict(url.query)
    query["options"] = f"-c search_path={schema},public"
    engine = create_engine(url.set(query=query), future=True)
    try:
        yield engine
    finally:
        engine.dispose()
        teardown = create_engine(postgres_url, future=True)
        try:
            with teardown.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            teardown.dispose()


@pytest.fixture
def pg_connection(pg_engine: Engine) -> Iterator[Connection]:
    """A transaction-wrapped connection; rolled back after each test."""
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()
