"""spec M: GitHub Issues PM provider (``github_issues``) on the pm_* tables

Spec M (bring your own tracker) persists the M1 GitHub Issues adapter behind
the same ``pm_connection`` / ``pm_task_link`` / ``pm_webhook_delivery`` tables
as Jira and Linear. That needs one new ``PMProvider`` value
(``github_issues``, wider than the ``jira``/``linear`` vocabulary the columns
were created for):

* every ``pm_*`` ``provider`` column is widened to ``VARCHAR(32)`` — headroom
  for the next tracker, mirroring the explicit ``enum_type(PMProvider,
  length=32)`` now on the models;
* any CHECK constraint that constrains ``provider`` is rewritten to include
  ``github_issues`` (and restored without it on downgrade).

Note on the CHECK rewrite: ``enum_type`` renders ``native_enum=False`` with
``create_constraint=False``, so a schema built by ``0002_pm_adapters`` (or
``create_all``) carries *no* provider CHECK today — verified against a
``0041`` Postgres (provider is a bare ``VARCHAR`` with no check; see also
0033/0017, which make the same observation for other enum columns). The
rewrite below is therefore written defensively: it discovers whatever
provider CHECK a deployment actually has and rewrites exactly that, and is a
no-op when there is none. No new CHECK is created where none existed, so the
migrated schema keeps matching the ORM metadata (schema-drift gate).

Idempotent like 0038-0041: ``upgrade`` widens only a column that is not
already ``VARCHAR(32)`` and rewrites only a CHECK that does not already allow
``github_issues``; re-running it changes nothing. ``downgrade`` refuses while
any ``github_issues`` row exists (the old vocabulary cannot represent it) and
otherwise restores the pre-0042 column shape.

Numbered ``0042`` provisionally — renumbered at merge per ADR-0009.

Revision ID: 0042_pm_github_issues
Revises: 0041_world_model
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)

# revision identifiers, used by Alembic.
revision: str = "0042_pm_github_issues"
down_revision: str | None = "0041_world_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_LEN = 32
_PM_TABLES: tuple[str, ...] = ("pm_connection", "pm_task_link", "pm_webhook_delivery")
_OLD_VALUES = "('jira', 'linear')"
_NEW_VALUES = "('jira', 'linear', 'github_issues')"


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_length(table: str, column: str) -> int | None:
    """Current declared VARCHAR length of ``table.column`` (``None`` if unbounded)."""
    for col in sa.inspect(op.get_bind()).get_columns(table):
        if col["name"] == column:
            return getattr(col["type"], "length", None)
    return None


def _provider_checks(table: str) -> list[dict]:
    """CHECK constraints on ``table`` whose text constrains ``provider``."""
    return [
        c
        for c in sa.inspect(op.get_bind()).get_check_constraints(table)
        if "provider" in (c.get("sqltext") or "")
    ]


def _rewrite_provider_checks(table: str, values: str, *, skip_if_contains: str) -> None:
    for check in _provider_checks(table):
        name = check["name"]
        if name is None or skip_if_contains in (check.get("sqltext") or ""):
            continue
        # Raw DDL with the *inspected* name: ``op.drop_constraint`` would run
        # the name through the metadata naming convention a second time and
        # look for a constraint that does not exist.
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
        op.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" CHECK (provider IN {values})')


def upgrade() -> None:
    # SQLite provisions via create_all against the current models, so the
    # columns are already correct there; only Postgres needs the ALTER (0033).
    if op.get_bind().dialect.name != "postgresql":
        return
    existing = _existing_tables()
    for table in _PM_TABLES:
        if table not in existing:
            continue
        if _column_length(table, "provider") != _TARGET_LEN:
            op.alter_column(
                table,
                "provider",
                type_=sa.String(length=_TARGET_LEN),
                existing_nullable=False,
            )
        _rewrite_provider_checks(table, _NEW_VALUES, skip_if_contains="github_issues")


def downgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables()
    for table in _PM_TABLES:
        if table not in existing:
            continue
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE provider = 'github_issues'")
        ).scalar_one()
        if count:
            raise RuntimeError(
                f"cannot downgrade 0042_pm_github_issues: {table} holds {count} "
                "github_issues row(s), which the pre-0042 provider vocabulary "
                "('jira', 'linear') cannot represent; remove them first"
            )
    if bind.dialect.name != "postgresql":
        return
    for table in _PM_TABLES:
        if table not in existing:
            continue
        _rewrite_provider_checks(table, _OLD_VALUES, skip_if_contains="'linear')")
        # Pre-0042 shape: a bare VARCHAR (enum_type carried no explicit width).
        if _column_length(table, "provider") is not None:
            op.alter_column(
                table,
                "provider",
                type_=sa.String(),
                existing_nullable=False,
            )
