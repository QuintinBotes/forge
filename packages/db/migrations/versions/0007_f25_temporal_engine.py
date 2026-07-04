"""f25 temporal engine: workflow_run engine-attribution columns

Adds three columns to the existing ``workflow_run`` table (baseline-owned) so a
run can be driven by either the V1 Postgres FSM or the V2 Temporal durable
workflow engine:

* ``engine_backend`` — VARCHAR(16) + CHECK in (postgres_fsm, temporal); which
  engine owns the run. Defaults to ``postgres_fsm`` so every pre-F25 / in-flight
  run is unchanged.
* ``temporal_workflow_id`` — ``wf-<workflow_run_id>``; NULL for FSM runs. Carries
  a plain lookup index plus a **partial-unique** index (where not null) that
  gives the same single-active-run guarantee the FSM enforces.
* ``temporal_run_id`` — Temporal's run id of the latest execution (informational;
  changes on continue-as-new / reset).

Foundation note: ``forge_db``'s baseline migration (``0001_baseline``) is
metadata-driven (``create_all`` over the live models for every non-deferred
table), so once these columns/indexes live on the ``WorkflowRun`` model the
baseline already provisions them on a fresh chain. To stay idiomatic *and* still
own an explicit, reversible F25 step, this migration is **idempotent**: ``upgrade``
adds only what is missing, ``downgrade`` drops only what F25 introduced (never the
``workflow_run`` table, which the baseline owns).

Temporal's own server state lives in separate logical databases (``temporal`` /
``temporal_visibility``) created by the auto-setup image / ``forge-cli temporal
bootstrap`` — it is not Alembic-managed.

Revision ID: 0007_f25_temporal_engine
Revises: 0006_f22_multi_repo
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_f25_temporal_engine"
down_revision: str | None = "0006_f22_multi_repo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workflow_run"
_CHECK_NAME = "ck_workflow_run_engine_backend"
_LOOKUP_INDEX = "ix_workflow_run_temporal_wfid"
_UNIQUE_INDEX = "uq_workflow_run_temporal_workflow_id"
_PARTIAL_WHERE = "temporal_workflow_id IS NOT NULL"
_COLUMNS = ("temporal_run_id", "temporal_workflow_id", "engine_backend")


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def _existing_checks() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)}


def upgrade() -> None:
    columns = _existing_columns()
    if "engine_backend" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "engine_backend",
                sa.String(length=16),
                server_default="postgres_fsm",
                nullable=False,
            ),
        )
    if "temporal_workflow_id" not in columns:
        op.add_column(
            _TABLE, sa.Column("temporal_workflow_id", sa.String(length=255), nullable=True)
        )
    if "temporal_run_id" not in columns:
        op.add_column(
            _TABLE, sa.Column("temporal_run_id", sa.String(length=255), nullable=True)
        )

    indexes = _existing_indexes()
    if _LOOKUP_INDEX not in indexes:
        op.create_index(_LOOKUP_INDEX, _TABLE, ["temporal_workflow_id"])
    if _UNIQUE_INDEX not in indexes:
        op.create_index(
            _UNIQUE_INDEX,
            _TABLE,
            ["temporal_workflow_id"],
            unique=True,
            postgresql_where=sa.text(_PARTIAL_WHERE),
            sqlite_where=sa.text(_PARTIAL_WHERE),
        )

    # CHECK constraint: native on Postgres only (SQLite cannot ADD a CHECK via
    # plain ALTER TABLE; the SAEnum column type enforces it there under
    # create_all). Idempotent so re-running over a baseline-provisioned DB is safe.
    if op.get_bind().dialect.name == "postgresql" and _CHECK_NAME not in _existing_checks():
        op.create_check_constraint(
            _CHECK_NAME, _TABLE, "engine_backend IN ('postgres_fsm', 'temporal')"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql" and _CHECK_NAME in _existing_checks():
        op.drop_constraint(_CHECK_NAME, _TABLE, type_="check")

    indexes = _existing_indexes()
    if _UNIQUE_INDEX in indexes:
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
    if _LOOKUP_INDEX in indexes:
        op.drop_index(_LOOKUP_INDEX, table_name=_TABLE)

    columns = _existing_columns()
    for name in _COLUMNS:
        if name in columns:
            op.drop_column(_TABLE, name)
