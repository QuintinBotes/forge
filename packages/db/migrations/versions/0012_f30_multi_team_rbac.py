"""f30 multi-team RBAC: team(_member), project_team_access, role_grant, audit_log

Creates the F30 authorization tables and extends ``project`` with the
``visibility`` + ``owner_team_id`` columns, then backfills one workspace-scope
``role_grant`` per existing ``app_user`` row from its flat ``app_user.role``
(the flat column is retained-but-deprecated; the resolver reads only
``role_grant``). Reversible: ``downgrade`` drops the project columns and the F30
tables (which removes the backfilled grants).

Foundation notes:

* The base ``team`` table does not exist in-tree, so this migration creates it
  with the full F30 column set.
* Tables are metadata-driven (``create_all`` over the live models) so the
  cross-dialect column variants + the ``audit_log`` append-only trigger (Postgres
  only, via the model's ``after_create`` listener) apply automatically.
* The ``project`` columns are added/dropped idempotently (the baseline is
  metadata-driven, so on a fresh chain they may already exist — this migration
  owns a clean, reversible step). ``owner_team_id`` is added as a plain column
  (the FK is enforced via the model on fresh chains + Postgres) so SQLite can
  drop it on downgrade, mirroring 0009.

Revision ID: 0012_f30_multi_team_rbac
Revises: 0011_f29_policy_rule_evaluation
Create Date: 2026-06-28
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0012_f30_multi_team_rbac"
down_revision: str | None = "0011_f29_policy_rule_evaluation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECT = "project"
_OWNER_TEAM_FK = "fk_project_owner_team_id_team"
# Dependency order matters for create (team before its dependents); create_all
# topologically sorts the passed tables, but we keep the list explicit.
F30_TABLES = ("team", "team_member", "project_team_access", "role_grant", "audit_log")


def _tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F30_TABLES if name in by_name]


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _as_uuid(value: object) -> uuid.UUID:
    """Coerce a DB-returned id (UUID on Postgres, hex str on SQLite) to a UUID."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _backfill_workspace_grants(bind: sa.engine.Connection) -> None:
    """Insert one workspace-scope ``role_grant`` per existing ``app_user`` row."""
    role_grant = Base.metadata.tables["role_grant"]
    users = bind.execute(sa.text("SELECT id, workspace_id, role FROM app_user")).fetchall()
    if not users:
        return
    # Skip any (principal, scope, role) tuples that already exist (idempotent).
    existing = {
        (str(_as_uuid(r[0])), str(_as_uuid(r[1])), r[2])
        for r in bind.execute(
            sa.text(
                "SELECT principal_id, scope_id, role FROM role_grant "
                "WHERE principal_type = 'user' AND scope_type = 'workspace'"
            )
        ).fetchall()
    }
    now = datetime.now(UTC)
    rows = []
    for user_id, workspace_id, role in users:
        uid, wsid = _as_uuid(user_id), _as_uuid(workspace_id)
        key = (str(uid), str(wsid), role)
        if key in existing:
            continue
        rows.append(
            {
                "id": uuid.uuid4(),
                "workspace_id": wsid,
                "principal_type": "user",
                "principal_id": uid,
                "scope_type": "workspace",
                "scope_id": wsid,
                "role": role,
                "granted_by": None,
                "expires_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    if rows:
        bind.execute(role_grant.insert(), rows)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    to_create = [t for t in _tables() if t.name not in existing]
    if to_create:
        Base.metadata.create_all(bind=bind, tables=to_create)

    project_cols = _columns(_PROJECT)
    if "visibility" not in project_cols:
        op.add_column(
            _PROJECT,
            sa.Column(
                "visibility",
                sa.String(length=32),
                server_default="workspace",
                nullable=False,
            ),
        )
    if "owner_team_id" not in project_cols:
        op.add_column(_PROJECT, sa.Column("owner_team_id", sa.Uuid(as_uuid=True), nullable=True))

    # The owner-team FK is Postgres-only (SQLite keeps a plain column so the
    # downgrade can drop it). Add it when missing on a fresh Postgres chain.
    if bind.dialect.name == "postgresql":
        fk_names = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys(_PROJECT)}
        if _OWNER_TEAM_FK not in fk_names:
            op.create_foreign_key(
                _OWNER_TEAM_FK,
                _PROJECT,
                "team",
                ["owner_team_id"],
                ["id"],
                ondelete="SET NULL",
            )

    _backfill_workspace_grants(bind)


def downgrade() -> None:
    bind = op.get_bind()
    # Drop the Postgres-only FK first so the column becomes droppable.
    if bind.dialect.name == "postgresql":
        fk_names = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys(_PROJECT)}
        if _OWNER_TEAM_FK in fk_names:
            op.drop_constraint(_OWNER_TEAM_FK, _PROJECT, type_="foreignkey")

    project_cols = _columns(_PROJECT)
    if "owner_team_id" in project_cols:
        op.drop_column(_PROJECT, "owner_team_id")
    if "visibility" in project_cols:
        op.drop_column(_PROJECT, "visibility")

    existing = set(sa.inspect(bind).get_table_names())
    to_drop = [t for t in reversed(_tables()) if t.name in existing]
    if to_drop:
        Base.metadata.drop_all(bind=bind, tables=to_drop)
