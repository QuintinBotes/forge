"""platform API key scopes and surface (spec L §6a)

Adds two nullable columns to ``platform_api_key``:

* ``scopes`` (JSON): ``NULL`` keeps a key's full role, as every existing key has;
  a narrowed key carries ``["read"]`` or ``["approvals:decide", "read"]``.
* ``surface`` (``VARCHAR(32)``): the surface a personal token names (``tervin``
  or ``cli``), recorded with the decisions it makes.

Existing rows read back unchanged (both ``NULL``). Idempotent like 0038-0045:
``upgrade`` adds only what is missing, ``downgrade`` drops only these columns.

The downgrade refuses, before changing anything, while an unrevoked key carries
scopes: without the column that key would act with its full role.

Revision ID: 0046_platform_api_key_scopes
Revises: 0045_workflow_lines
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from forge_db.base import json_type

# revision identifiers, used by Alembic.
revision: str = "0046_platform_api_key_scopes"
down_revision: str | None = "0045_workflow_lines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_api_key"


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _narrowed_active_keys() -> int:
    table = sa.table(_TABLE, sa.column("scopes", json_type()), sa.column("revoked_at"))
    rows = op.get_bind().execute(sa.select(table.c.scopes).where(table.c.revoked_at.is_(None)))
    # Counted in Python: an ORM-written "no scopes" may be JSON null rather than SQL NULL.
    return sum(1 for (scopes,) in rows if scopes)


def upgrade() -> None:
    existing = _columns()
    if "scopes" not in existing:
        op.add_column(_TABLE, sa.Column("scopes", json_type(), nullable=True))
    if "surface" not in existing:
        op.add_column(_TABLE, sa.Column("surface", sa.String(32), nullable=True))


def downgrade() -> None:
    existing = _columns()
    if "scopes" in existing and (count := _narrowed_active_keys()):
        raise RuntimeError(
            f"cannot downgrade 0046_platform_api_key_scopes: {count} unrevoked {_TABLE} "
            "row(s) carry scopes and would act with their full role; revoke them first"
        )
    # Batch: a plain ALTER on Postgres, a table rebuild on SQLite.
    with op.batch_alter_table(_TABLE) as batch:
        for column in ("surface", "scopes"):
            if column in existing:
                batch.drop_column(column)
