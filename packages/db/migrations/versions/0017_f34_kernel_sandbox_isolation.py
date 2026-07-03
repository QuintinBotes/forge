"""f34 kernel sandbox isolation: sandbox_instance runtime/VM provenance columns.

Extends ``sandbox_instance`` with the F34 kernel-boundary provenance columns
(``runtime``, ``isolation_class``, ``gvisor_platform``, ``guest_kernel_version``,
``vm_vcpus``, ``vm_memory_mb``, ``boot_ms``) plus the auditor index
``ix_sandbox_instance_isolation_class`` ("which runs executed under a microVM
boundary?").

The ``sandbox_kind`` enum values (``gvisor``/``microvm``) need no DDL: forge_db
stores enums as VARCHAR (``enum_type`` uses ``native_enum=False`` without a CHECK
constraint), so the new members are purely additive at the model layer.

Revision ID: 0017_f34_kernel_sandbox
Revises: 0016_f33_enterprise_sso
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_f34_kernel_sandbox"
down_revision: str | None = "0016_f33_enterprise_sso"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sandbox_instance"
_INDEX = "ix_sandbox_instance_isolation_class"

def _f34_columns() -> tuple[sa.Column, ...]:
    """Fresh Column objects per call (a Column may be bound to one table only)."""
    return (
        sa.Column("runtime", sa.String(length=64), nullable=True),
        sa.Column(
            "isolation_class",
            sa.String(length=32),
            nullable=False,
            server_default="host_process",
        ),
        sa.Column("gvisor_platform", sa.String(length=32), nullable=True),
        sa.Column("guest_kernel_version", sa.String(length=128), nullable=True),
        sa.Column("vm_vcpus", sa.Integer(), nullable=True),
        sa.Column("vm_memory_mb", sa.Integer(), nullable=True),
        sa.Column("boot_ms", sa.Integer(), nullable=True),
    )


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    # The baseline creates sandbox_instance from live model metadata, which
    # already carries the F34 columns on a fresh install — guard by existence
    # (the 0009/0013/0016 convention) so fresh and upgraded databases converge.
    columns = _existing_columns()
    for column in _f34_columns():
        if column.name not in columns:
            op.add_column(_TABLE, column)
    if _INDEX not in _existing_indexes():
        op.create_index(_INDEX, _TABLE, ["isolation_class"])


def downgrade() -> None:
    if _INDEX in _existing_indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = _existing_columns()
    for column in reversed(_f34_columns()):
        if column.name in columns:
            op.drop_column(_TABLE, column.name)
