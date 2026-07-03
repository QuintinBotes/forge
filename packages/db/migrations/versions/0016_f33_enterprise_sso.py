"""f33 enterprise sso: SAML config/domains, external identities, SCIM tables.

Creates the seven F33 tables from ``forge_db`` metadata (so the migration can
never drift from the models): ``sso_configuration``, ``sso_domain``,
``external_identity``, ``scim_token``, ``scim_group``, ``scim_group_member``,
and the Redis-fallback replay store ``saml_replay`` — including the uniqueness
guarantees the protocol surface relies on (one SAML config per workspace,
globally-unique HRD domains ``uq_sso_domain_domain``, unique
``(workspace, provider, external_id)`` identity links, unique SCIM ids).

Also extends ``app_user`` with the two F33 lifecycle columns:

* ``deactivated_at`` — when SCIM deprovisioned the user (audit trail);
* ``external_managed`` — the directory owns this user's lifecycle.

Revision ID: 0016_f33_enterprise_sso
Revises: 0015_f32_integration_marketplace
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

# revision identifiers, used by Alembic.
revision: str = "0016_f33_enterprise_sso"
down_revision: str | None = "0015_f32_integration_marketplace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

F33_TABLES = (
    "sso_configuration",
    "sso_domain",
    "external_identity",
    "scim_token",
    "scim_group",
    "scim_group_member",
    "saml_replay",
)


def _f33_tables() -> list:
    by_name = {t.name: t for t in Base.metadata.sorted_tables}
    return [by_name[name] for name in F33_TABLES if name in by_name]


def _app_user_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("app_user")}


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_f33_tables())
    # The baseline migration creates app_user from live model metadata, which
    # already carries the F33 columns on a fresh install — guard by existence
    # (the 0009/0013 convention) so both fresh and upgraded databases converge.
    columns = _app_user_columns()
    if "deactivated_at" not in columns:
        op.add_column(
            "app_user",
            sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "external_managed" not in columns:
        op.add_column(
            "app_user",
            sa.Column(
                "external_managed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    columns = _app_user_columns()
    if "external_managed" in columns:
        op.drop_column("app_user", "external_managed")
    if "deactivated_at" in columns:
        op.drop_column("app_user", "deactivated_at")
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(_f33_tables())))
