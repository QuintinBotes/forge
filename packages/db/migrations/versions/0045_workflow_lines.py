"""workflow lines: open run states, marketplace definitions, station visits (spec K §4.3)

The four persistence items of ADR-0020:

1. ``workflow_run.current_state`` becomes an open ``VARCHAR(64)`` (ORM type
   ``str``). ``enum_type`` never emitted a DB CHECK for it (SQLAlchemy 2's
   ``Enum`` defaults to ``create_constraint=False``), so the feature vocabulary
   was held only by the ORM enum type, which is now gone. On Postgres the column
   narrows from unbounded ``VARCHAR`` to ``VARCHAR(64)``; SQLite ignores lengths.
2. ``workflow_definition.source`` gains ``marketplace``. Same reason: the column
   is an unbounded ``VARCHAR`` with no CHECK, so only the ORM enum changes.
3. ``uq_marketplace_installation_pkg`` becomes ``(workspace_id, registry_slug,
   kind, listing_slug)``, matching ``marketplace_listing``'s key. It is looser,
   so existing rows already satisfy it.
4. New table ``workflow_station_visit``.

The downgrade restores the 0044 schema. It refuses, with a clear error and
before changing anything, while a ``workflow_run`` holds a state outside the
feature vocabulary or a ``workflow_definition`` came from the marketplace: the
0044 code cannot load either row.

Idempotent like 0038-0044: the baseline and 0010/0015 are metadata-driven, so on
a fresh chain items 1-3 already hold and ``upgrade`` changes only what differs.

Revision ID: 0045_workflow_lines
Revises: 0044_agent_run_context_refs
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base
from forge_db.models.enums import WorkflowState

# revision identifiers, used by Alembic.
revision: str = "0045_workflow_lines"
down_revision: str | None = "0044_agent_run_context_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN = "workflow_run"
_DEFINITION = "workflow_definition"
_INSTALL = "marketplace_installation"
_INSTALL_KEY = "uq_marketplace_installation_pkg"
_VISIT = "workflow_station_visit"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _set_install_key(columns: list[str]) -> None:
    inspector = sa.inspect(op.get_bind())
    keys = {uc["name"]: uc["column_names"] for uc in inspector.get_unique_constraints(_INSTALL)}
    if keys.get(_INSTALL_KEY) == columns:
        return
    # Batch: a plain ALTER on Postgres, a table rebuild on SQLite.
    with op.batch_alter_table(_INSTALL) as batch:
        if _INSTALL_KEY in keys:
            batch.drop_constraint(_INSTALL_KEY, type_="unique")
        batch.create_unique_constraint(_INSTALL_KEY, columns)


def _refuse_rows(table: str, where: sa.ColumnElement[bool], why: str) -> None:
    count = (
        op.get_bind()
        .execute(sa.select(sa.func.count()).select_from(sa.table(table)).where(where))
        .scalar_one()
    )
    if count:
        raise RuntimeError(f"cannot downgrade 0045_workflow_lines: {count} {table} row(s) {why}")


def _refuse_shared_slugs() -> None:
    """The 0044 key cannot hold two kinds that share a registry slug."""
    install = sa.table(
        _INSTALL, sa.column("workspace_id"), sa.column("registry_slug"), sa.column("listing_slug")
    )
    shared = (
        sa.select(install.c.workspace_id)
        .group_by(install.c.workspace_id, install.c.registry_slug, install.c.listing_slug)
        .having(sa.func.count() > 1)
        .subquery()
    )
    count = op.get_bind().execute(sa.select(sa.func.count()).select_from(shared)).scalar_one()
    if count:
        raise RuntimeError(
            f"cannot downgrade 0045_workflow_lines: {count} {_INSTALL} slug(s) are installed "
            "as more than one kind; uninstall all but one kind of each before downgrading"
        )


def upgrade() -> None:
    if _is_postgres():
        op.alter_column(_RUN, "current_state", type_=sa.String(64), existing_nullable=False)
    _set_install_key(["workspace_id", "registry_slug", "kind", "listing_slug"])
    if _VISIT not in sa.inspect(op.get_bind()).get_table_names():
        Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[_VISIT]])


def downgrade() -> None:
    _refuse_rows(
        _RUN,
        sa.column("current_state").not_in([state.value for state in WorkflowState]),
        "hold a state outside the feature workflow vocabulary (line runs); "
        "delete those runs before downgrading",
    )
    _refuse_rows(
        _DEFINITION,
        sa.column("source") == "marketplace",
        "came from the marketplace; delete those definitions before downgrading",
    )
    _refuse_shared_slugs()
    if _VISIT in sa.inspect(op.get_bind()).get_table_names():
        Base.metadata.drop_all(bind=op.get_bind(), tables=[Base.metadata.tables[_VISIT]])
    _set_install_key(["workspace_id", "registry_slug", "listing_slug"])
    if _is_postgres():
        op.alter_column(_RUN, "current_state", type_=sa.String(), existing_nullable=False)
