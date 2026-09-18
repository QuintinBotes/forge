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

The one post-processing step is a narrow, exact-name exclusion allowlist
(``KNOWN_UNREPRESENTABLE``) applied to the *result* diff — never via
``include_object`` — for the handful of Postgres objects that exist in the
migrated schema but genuinely cannot be represented in ORM metadata (raw-DDL
expression/GIN indexes and Postgres-only FKs the models deliberately omit for
SQLite portability). It matches by exact object name only, drops only ``remove_*``
ops, and leaves the ``add_*`` direction completely unfiltered so real "model grew
something without a migration" drift still fails. Each excluded name cites the
migration/DDL that owns it (see the allowlist below).

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
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    create_engine,
    text,
)
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


# --------------------------------------------------------------------------- #
# Structurally-unrepresentable schema objects — exact-name exclusion allowlist. #
#                                                                               #
# Each object below genuinely exists in the migrated Postgres schema but CANNOT #
# be expressed in the SQLAlchemy ORM ``Base.metadata`` (raw-DDL expression/GIN  #
# indexes, or Postgres-only FKs the models deliberately omit so SQLite          #
# ``create_all`` stays portable). ``compare_metadata`` therefore emits a        #
# perpetual, un-actionable ``remove_*`` op for each — a false positive the gate #
# must tolerate WITHOUT weakening its real job.                                 #
#                                                                               #
# The exclusion below is deliberately narrow: it matches by EXACT object name   #
# only (no wildcard, no prefix), it never suppresses a whole diff kind, and it  #
# only ever drops ``remove_*`` ops. The ``add_*`` direction is left completely  #
# unfiltered — a model that *grows* one of these names without a migration must #
# still fail the gate, because that is real, dangerous drift. Every name cites  #
# the migration / DDL that owns it and why Alembic cannot round-trip it:        #
KNOWN_UNREPRESENTABLE: frozenset[str] = frozenset(
    {
        # Raw-DDL GIN index over a ``jsonb_path_ops`` expression, created by
        # ``marketplace.py``'s ``_pg_ddl(...)`` (Postgres-only; mirrored by
        # migration ``0015_f32_integration_marketplace``). Alembic metadata
        # cannot represent a functional / opclass GIN index, so it round-trips
        # as a phantom removal.
        "ix_marketplace_listing_tags_gin",
        # Raw-DDL full-text GIN index over ``to_tsvector('english', name || ' '
        # || summary)``, created by ``marketplace.py``'s ``_pg_ddl(...)``
        # (Postgres-only; mirrored by ``0015``). Expression indexes are not
        # expressible in ORM metadata.
        "ix_marketplace_listing_fts",
        # Postgres-only FK ``project.owner_team_id -> team.id`` (ON DELETE SET
        # NULL) added by migration ``0012_f30_multi_team_rbac``. The model keeps
        # a plain, FK-less column so SQLite ``create_all`` can drop it on
        # downgrade; the FK lives only in the migration and reflects as a phantom
        # removal.
        "fk_project_owner_team_id_team",
        # Postgres-only FK ``sub_agent_run.agent_run_id -> agent_run.id`` (ON
        # DELETE SET NULL) added by migration ``0009_f27_multi_agent`` (the
        # child-run FK pattern ``project.owner_team_id`` mirrors). The model omits
        # the ORM FK, so it reflects as a phantom removal.
        "fk_sub_agent_run_child",
    }
)


def _removal_object_name(op: tuple) -> str | None:
    """Exact schema-object name a ``remove_*`` diff op targets, else ``None``.

    Defined only for ``remove_*`` ops whose payload is a named schema construct
    (``Index`` / ``ForeignKeyConstraint`` and other constraints expose ``.name``).
    Returns ``None`` for every other op — crucially every ``add_*`` op — so the
    exclusion below can never match anything but an exact-named removal.
    """
    kind = op[0]
    if not kind.startswith("remove_") or len(op) < 2:
        return None
    return getattr(op[1], "name", None)


def _filter_known_unrepresentable(diffs: list) -> list:
    """Drop only exact-named ``remove_*`` ops listed in ``KNOWN_UNREPRESENTABLE``.

    Everything else is preserved verbatim: grouped ops (lists, e.g. ``modify_*``),
    every ``add_*`` op, and any ``remove_*`` op whose name is not in the frozen
    set. This keeps the gate live for all real drift while tolerating the handful
    of structurally-unrepresentable Postgres objects documented above.
    """
    kept: list = []
    for entry in diffs:
        if isinstance(entry, list):
            kept.append(entry)
            continue
        name = _removal_object_name(entry)
        if name is not None and name in KNOWN_UNREPRESENTABLE:
            continue
        kept.append(entry)
    return kept


# --------------------------------------------------------------------------- #
# Unit tests for the exclusion filter itself — no Postgres required.          #
#                                                                               #
# `_filter_known_unrepresentable` / `_removal_object_name` were previously     #
# exercised only indirectly, inside `test_no_schema_drift_between_models_and_  #
# migrations` (``@pytest.mark.postgres``), which skips with no live DB. These  #
# table-driven cases pin the filter's actual decision logic — using real       #
# ``sqlalchemy.Index`` / ``ForeignKeyConstraint`` objects so ``.name`` behaves  #
# exactly as it does on a genuine ``compare_metadata`` diff — and run          #
# unconditionally (unmarked; the module has no module-level ``pytestmark``,    #
# each Postgres test opts in individually via ``@pytest.mark.postgres``).      #
# --------------------------------------------------------------------------- #


def _named_index(name: str) -> Index:
    """A real, standalone ``Index`` with the given ``.name`` (no DB needed)."""
    metadata = MetaData()
    table = Table(f"_t_{name}", metadata, Column("id", Integer))
    return Index(name, table.c.id)


def _named_fk(name: str) -> ForeignKeyConstraint:
    """A real, standalone ``ForeignKeyConstraint`` with the given ``.name``."""
    return ForeignKeyConstraint(["parent_id"], ["parent.id"], name=name)


_ALLOWLISTED_INDEX = _named_index("ix_marketplace_listing_fts")
_OTHER_INDEX = _named_index("ix_other_random")
_ALLOWLISTED_FK = _named_fk("fk_sub_agent_run_child")
_MODIFY_TYPE_GROUP = [("modify_type", None, "some_table", "some_column", {}, "OLD", "NEW")]
_REMOVE_COLUMN_NO_SCHEMA = ("remove_column", None, "sub_agent_run", Column("agent_run_id", Integer))
_REMOVE_COLUMN_WITH_SCHEMA = (
    "remove_column",
    "public",
    "sub_agent_run",
    Column("agent_run_id", Integer),
)


@pytest.mark.parametrize(
    ("diffs", "expected"),
    [
        pytest.param(
            [("remove_index", _ALLOWLISTED_INDEX)],
            [],
            id="remove_index-allowlisted-name-filtered",
        ),
        pytest.param(
            [("add_index", _ALLOWLISTED_INDEX)],
            [("add_index", _ALLOWLISTED_INDEX)],
            id="add_index-allowlisted-name-kept-add-star-never-filtered",
        ),
        pytest.param(
            [("remove_index", _OTHER_INDEX)],
            [("remove_index", _OTHER_INDEX)],
            id="remove_index-non-allowlisted-name-kept",
        ),
        pytest.param(
            [_MODIFY_TYPE_GROUP],
            [_MODIFY_TYPE_GROUP],
            id="grouped-op-list-kept-unconditionally",
        ),
        pytest.param(
            [("remove_fk", _ALLOWLISTED_FK)],
            [],
            id="remove_fk-allowlisted-name-filtered",
        ),
        pytest.param(
            [_REMOVE_COLUMN_NO_SCHEMA],
            [_REMOVE_COLUMN_NO_SCHEMA],
            id="remove_column-schema-is-None-no-name-attr-kept-no-raise",
        ),
        pytest.param(
            [_REMOVE_COLUMN_WITH_SCHEMA],
            [_REMOVE_COLUMN_WITH_SCHEMA],
            id="remove_column-schema-is-str-no-name-attr-kept-no-raise",
        ),
    ],
)
def test_filter_known_unrepresentable_cases(diffs: list, expected: list) -> None:
    """Table-driven pin of the exclusion filter's decision logic (no Postgres).

    Each case is a synthetic ``compare_metadata``-shaped diff entry:

    * ``remove_index`` / ``remove_fk`` on an allowlisted name -> dropped.
    * ``add_index`` on the *same* allowlisted name -> kept: only ``remove_*``
      is ever eligible for filtering, per ``_removal_object_name``'s
      ``kind.startswith("remove_")`` guard.
    * ``remove_index`` on a non-allowlisted name -> kept.
    * A grouped op (a ``list``, e.g. Alembic's per-column ``modify_*`` bundle)
      -> kept unconditionally; ``_filter_known_unrepresentable`` never
      recurses into grouped entries.
    * ``remove_column`` -> kept and does not raise: its diff shape is
      ``("remove_column", schema, tablename, column)``, so ``op[1]`` is the
      *schema* (a ``str`` or ``None``), not the column. ``getattr(op[1],
      "name", None)`` safely returns ``None`` for both, so remove_column ops
      are never filterable by this function regardless of the column's name.
    """
    assert _filter_known_unrepresentable(diffs) == expected


def test_compare_metadata_detects_add_table_without_postgres() -> None:
    """Sensitivity guard, add_* direction — proven hermetically (no Postgres).

    Folded-in minor: the existing ``test_compare_metadata_is_drift_sensitive``
    (``@pytest.mark.postgres``) only proves the *remove* direction (comparing a
    populated DB against empty metadata). This proves the *add* direction —
    metadata carrying a table the database lacks yields an ``add_table`` op —
    without needing Postgres or any of ``env.py``'s pgvector-specific baseline:
    an in-memory SQLite connection is enough to drive a real
    ``MigrationContext``/``compare_metadata`` round-trip, so this direction is
    on watch even in the hermetic, no-network default lane.
    """
    baseline_metadata = MetaData()
    grown_metadata = MetaData()
    Table("throwaway_extra_table", grown_metadata, Column("id", Integer, primary_key=True))

    engine = create_engine("sqlite://", future=True)
    try:
        with engine.connect() as conn:
            baseline_metadata.create_all(conn)  # the "existing" DB: no tables
            context = MigrationContext.configure(
                connection=conn,
                opts={"compare_type": True, "target_metadata": grown_metadata},
            )
            diffs = compare_metadata(context, grown_metadata)
    finally:
        engine.dispose()

    ops = {op[0] for entry in diffs for op in (entry if isinstance(entry, list) else [entry])}
    assert "add_table" in ops, (
        "expected an 'add_table' op when comparing metadata that grew a table "
        f"the database lacks; got op kinds: {sorted(ops)}"
    )


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
            raw_diffs = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    # Drop only the exact-named, structurally-unrepresentable Postgres objects
    # documented in KNOWN_UNREPRESENTABLE (raw-DDL expression/GIN indexes +
    # Postgres-only FKs). The add_* direction and every other object stay live.
    diffs = _filter_known_unrepresentable(raw_diffs)

    assert not diffs, (
        "Schema drift detected: the ORM models and the Alembic migrations "
        "disagree. `alembic revision --autogenerate` against a fresh Postgres "
        f"would emit {sum(len(d) if isinstance(d, list) else 1 for d in diffs)} "
        "operation(s):\n"
        + _diff_lines(diffs)
        + "\n\nResolve by adding/adjusting an Alembic migration under "
        "packages/db/migrations/versions so the schema matches the models "
        "(do NOT silence this by editing the test). If a divergence is genuinely "
        "intentional and cannot be represented in metadata, add its exact name to "
        "KNOWN_UNREPRESENTABLE above with a comment citing the migration/DDL "
        "source (never a wildcard, never a whole diff kind, never an add_* op)."
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
