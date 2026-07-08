"""Adaptive Orchestration per-role model+effort override store (``ao-config``).

Backs the storage boundary defined by
:class:`forge_contracts.orchestration_config.RoleConfigStore`: one row per
workspace- or project-scoped override of a role's ``{model_or_tier, effort}``
pair. The hardcoded per-role defaults
(:data:`forge_contracts.orchestration_config.DEFAULT_ROLE_CONFIG`) never live
in this table -- only human overrides do; the resolver
(:func:`forge_orchestration_policy.role_config.resolve_effective_config`) falls
back to them when no override row exists.

Two independent scopes share this one table, distinguished by ``project_id``:

* ``project_id IS NULL`` -- a workspace-wide override for ``role``. At most one
  such row per ``(workspace_id, role)`` (enforced by the
  ``uq_agent_role_config_workspace_default`` partial unique index).
* ``project_id`` set -- a project-scoped override for ``role`` that takes
  precedence over the workspace-wide row. At most one such row per
  ``(workspace_id, project_id, role)`` (the plain
  ``uq_agent_role_config_project`` unique constraint -- NULLs are distinct
  under a standard unique index on both Postgres and SQLite, so it does not
  also constrain the workspace-wide rows).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type
from forge_db.models.enums import AgentRole, Effort


class AgentRoleConfig(WorkspaceScopedModel):
    """One workspace- or project-scoped ``{role -> model_or_tier, effort}`` override."""

    __tablename__ = "agent_role_config"
    __table_args__ = (
        # At most one workspace-wide override (``project_id IS NULL``) per role.
        Index(
            "uq_agent_role_config_workspace_default",
            "workspace_id",
            "role",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        # At most one project-scoped override per (workspace, project, role).
        # NULLs are distinct under a plain unique index, so this constraint
        # does not interact with the workspace-default rows above.
        UniqueConstraint(
            "workspace_id",
            "project_id",
            "role",
            name="uq_agent_role_config_project",
        ),
    )

    #: ``NULL`` = workspace-wide override; set = project-scoped override.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=True,
    )
    role: Mapped[AgentRole] = mapped_column(enum_type(AgentRole), nullable=False)
    #: A tier keyword (``junior``/``medior``/``senior``) or a concrete model id
    #: a human pinned verbatim; the (separate) model router resolves a tier.
    model_or_tier: Mapped[str] = mapped_column(String(128), nullable=False)
    effort: Mapped[Effort] = mapped_column(enum_type(Effort), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"AgentRoleConfig(workspace_id={self.workspace_id!r}, "
            f"project_id={self.project_id!r}, role={self.role!r}, "
            f"model_or_tier={self.model_or_tier!r}, effort={self.effort!r})"
        )


__all__ = ["AgentRoleConfig"]
