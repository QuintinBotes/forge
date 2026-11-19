"""device_authorization: pending device sign-ins for a CLI client (#207)

Adds ``device_authorization``: per request SHA-256 hashes of the device code and
the user code (never the codes), the user code's first half, the client's name,
its state, when it expires and was last polled, how many wrong user codes named
it, and, once someone approves or denies it, the workspace and the person. Not
workspace-scoped: the workspace is unknown until a person approves.

Idempotent like 0050: the baseline is metadata-driven, so on a fresh chain the
table already exists and ``upgrade`` changes nothing.

Revision ID: 0052_device_authorization
Revises: 0051_held_out_scenario_run
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import scf_db.models  # noqa: F401  (registers all models on Base.metadata)
from scf_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0052_device_authorization"
down_revision: str | None = "0051_held_out_scenario_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("device_authorization",)


def _owned_tables() -> list[sa.Table]:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in _TABLES if name in by_name]


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    to_create = [t for t in _owned_tables() if t.name not in existing]
    if to_create:
        Base.metadata.create_all(bind=op.get_bind(), tables=to_create)


def downgrade() -> None:
    existing = _existing_tables()
    to_drop = [t for t in reversed(_owned_tables()) if t.name in existing]
    if to_drop:
        Base.metadata.drop_all(bind=op.get_bind(), tables=to_drop)
