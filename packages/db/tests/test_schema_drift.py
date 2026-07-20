"""Schema-drift gate (Task 11).

Upgrades a throwaway Postgres database to ``head`` and asserts Alembic's
:func:`alembic.autogenerate.compare_metadata` finds an *empty* diff between the
migrated schema and the ORM ``Base.metadata``. In other words: every model
change must be backed by a matching Alembic migration (and vice versa). This
closes the audit gap where a SQLAlchemy model change *without* a corresponding
migration passed CI and shipped — the drift only surfaces at runtime as a
missing column/table.

Contract — this test asserts *exactly* the diff a real ``alembic revision
--autogenerate`` would emit, by reusing the same autogenerate settings
``packages/db/migrations/env.py`` configures and nothing more:

* ``compare_type=True`` — mirrored from ``env.py``'s ``context.configure(...)``.
* ``compare_server_default`` — *not* set, exactly as in ``env.py`` (Alembic
  defaults it off), so ``server_default`` text is never compared. This is the
  documented reason the ``server_default`` false positives the brief warns about
  do not appear.
* No ``include_object`` / ``include_name`` filter — ``env.py`` defines none, so
  none is added here. Alembic's own version table (``alembic_version``) is
  excluded internally by ``compare_metadata``; pgvector's ``VECTOR`` /
  ``TSVECTOR`` types round-trip through reflection (pgvector registers them in
  the Postgres dialect ``ischema_names``), so neither needs a filter.

Keeping the settings identical to ``env.py`` means the gate can never disagree
with the tool that generates the migrations: if this test is green, autogenerate
is quiescent; if it is red, autogenerate would produce a non-empty revision.

Runs only against a real pgvector Postgres (the baseline's ``CREATE EXTENSION
vector`` + ``VECTOR(1536)`` / ``tsvector`` columns cannot be represented on
SQLite) and skips cleanly when no ``FORGE_TEST_DATABASE_URL`` (or a
``testcontainers`` pgvector) is available — mirroring the ``@pytest.mark.postgres``
tests in ``test_migration.py``. CI's ``python`` job provisions the database and
sets ``FORGE_TEST_DATABASE_URL``, so the gate is live there.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import make_url

import forge_db.models  # noqa: F401  (registers every model on Base.metadata)
from forge_db.base import Base
from forge_db.migration_utils import build_alembic_config


def _diff_lines(diffs: list) -> str:
    """Render a ``compare_metadata`` diff list as one human-readable line each.

    Each element is either a single ``(op, ...)`` tuple or a list of such tuples
    (Alembic groups per-column ops, e.g. ``modify_type``); flatten so every drift
    item prints verbatim in the failure message / CI log.
    """
    lines: list[str] = []
    for entry in diffs:
        group = entry if isinstance(entry, list) else [entry]
        for op in group:
            lines.append(f"  - {op!r}")
    return "\n".join(lines)


@pytest.fixture
def scratch_pg_config(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    """Provision a throwaway Postgres database and yield an Alembic ``Config``.

    A fresh, empty database (public schema, no Forge tables) is created on the
    same server the ``postgres_url`` fixture resolves, so ``alembic upgrade``
    runs against a clean slate and the baseline's ``CREATE EXTENSION vector`` +
    every table is provisioned end-to-end without touching any other test's data.
    The database is dropped on teardown. ``FORGE_DATABASE_URL`` is pointed at the
    scratch DB for the duration so Alembic's ``env.py`` targets it.

    (Copied from ``test_migration.py``'s live-Postgres tests so the drift gate
    shares their engine/env/isolation conventions verbatim.)
    """
    base_url = make_url(postgres_url)
    scratch_name = f"forge_drift_{uuid.uuid4().hex[:12]}"

    admin = create_engine(base_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{scratch_name}"'))
    finally:
        admin.dispose()

    scratch_url = base_url.set(database=scratch_name)
    url_str = scratch_url.render_as_string(hide_password=False)
    # env.py reads FORGE_DATABASE_URL first, then the config's sqlalchemy.url;
    # set both to the scratch DB so nothing external can redirect the migration.
    monkeypatch.setenv("FORGE_DATABASE_URL", url_str)
    cfg = build_alembic_config(url_str)
    try:
        yield cfg
    finally:
        teardown = create_engine(base_url, isolation_level="AUTOCOMMIT", future=True)
        try:
            with teardown.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :n AND pid <> pg_backend_pid()"
                    ),
                    {"n": scratch_name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}"'))
        finally:
            teardown.dispose()


@pytest.mark.postgres
def test_no_schema_drift_between_models_and_migrations(scratch_pg_config: Config) -> None:
    """The migrated ``head`` schema must equal the ORM metadata (no drift).

    Upgrades a fresh Postgres to ``head`` then runs ``compare_metadata`` with the
    exact autogenerate settings ``env.py`` uses. A non-empty diff means a model
    was changed without a matching migration (or a migration diverged from the
    models) — the gate the audit found missing. Every diff item is listed in the
    failure so the fix (add/adjust a migration, or align the model) is obvious.
    """
    url = scratch_pg_config.get_main_option("sqlalchemy.url")
    assert url is not None

    command.upgrade(scratch_pg_config, "head")

    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            # Mirror env.py's online autogenerate contract exactly:
            #   context.configure(connection=..., target_metadata=Base.metadata,
            #                     compare_type=True)
            context = MigrationContext.configure(
                connection=conn,
                opts={"compare_type": True, "target_metadata": Base.metadata},
            )
            diffs = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert not diffs, (
        "Schema drift detected: the ORM models and the Alembic migrations "
        "disagree. `alembic revision --autogenerate` against a fresh Postgres "
        f"would emit {sum(len(d) if isinstance(d, list) else 1 for d in diffs)} "
        "operation(s):\n"
        + _diff_lines(diffs)
        + "\n\nResolve by adding/adjusting an Alembic migration under "
        "packages/db/migrations/versions so the schema matches the models "
        "(do NOT silence this by editing the test). If a divergence is "
        "intentional and cannot be represented in metadata, document it here "
        "with a narrow, explicit exclusion."
    )


@pytest.mark.postgres
def test_compare_metadata_is_drift_sensitive(scratch_pg_config: Config) -> None:
    """Sensitivity guard: the comparison actually detects a missing schema.

    Proves the gate above can fail — without mutating any real model. Comparing
    the migrated ``head`` database against an empty ``MetaData()`` must yield a
    non-empty diff containing ``remove_table`` operations (Alembic's signal that
    the database has tables the compared metadata lacks). This is the same
    machinery, exercised in its failing direction, so a green
    ``test_no_schema_drift_*`` is a meaningful assertion rather than a
    vacuously-empty one.
    """
    url = scratch_pg_config.get_main_option("sqlalchemy.url")
    assert url is not None

    command.upgrade(scratch_pg_config, "head")

    empty_metadata = MetaData()
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                connection=conn,
                opts={"compare_type": True, "target_metadata": empty_metadata},
            )
            diffs = compare_metadata(context, empty_metadata)
    finally:
        engine.dispose()

    assert diffs, "compare_metadata found no diff against empty metadata — gate is blind"
    ops = {op[0] for entry in diffs for op in (entry if isinstance(entry, list) else [entry])}
    assert "remove_table" in ops, (
        "expected 'remove_table' ops when comparing a populated database against "
        f"empty metadata; got op kinds: {sorted(ops)}"
    )
