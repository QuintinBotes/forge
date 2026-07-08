"""ao-config: per-role model+effort override table ``agent_role_config``.

Adaptive Orchestration's per-role model+effort config store (spec: ao-config).
Creates one new, self-contained table:

* ``agent_role_config`` -- one row per workspace- or project-scoped override of
  a role's ``{model_or_tier, effort}`` pair (``role`` one of ``planner`` /
  ``coder`` / ``reviewer`` / ``spec_author`` / ``coordinator``; ``effort`` one
  of ``low`` / ``medium`` / ``high`` / ``max``). A ``NULL`` ``project_id`` is a
  workspace-wide override; a set ``project_id`` scopes to that project and
  takes precedence. The hardcoded per-role defaults
  (``forge_contracts.orchestration_config.DEFAULT_ROLE_CONFIG``) never live in
  this table -- only human overrides do.

Two indexes back the two override scopes: the partial unique
``uq_agent_role_config_workspace_default`` (``project_id IS NULL``, at most one
workspace-wide override per role) and the plain
``uq_agent_role_config_project`` (``workspace_id, project_id, role`` -- at most
one project-scoped override per role per project).

Idempotent like 0024/0025/0027/0028: ``upgrade`` creates only what is missing,
``downgrade`` drops only what this revision introduced.

Revision ID: 0029_ao_config_role_model_config
Revises: 0028_idempotency_store
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0029_ao_config_role_model_config"
down_revision: str | None = "0028_idempotency_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables owned by this revision (downgrade drops them in reverse).
_TABLES: tuple[str, ...] = ("agent_role_config",)


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
