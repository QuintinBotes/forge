"""F30 authorization source of truth: ``role_grant``.

A grant binds ``(principal, scope, role)`` where scope is one of workspace /
team / project. This table replaces the flat ``app_user.role`` for authorization
(the flat column is retained-but-deprecated and backfilled into workspace-scope
grants by the F30 migration). Revocation is a row DELETE *plus* an immutable
audit event — the permanent history is the ``audit_log`` table, not this mutable
table.

Foundation deviations (slice doc §3.1 vs. in-tree): the table is named
``role_grant`` (singular, matching the foundation convention; the doc says
``role_grants``); the role enum reuses :class:`UserRole` (``Role``) verbatim.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type
from forge_db.models.enums import PrincipalType, ScopeType, UserRole


class RoleGrant(WorkspaceScopedModel):
    """A single ``(principal, scope, role)`` authorization grant."""

    __tablename__ = "role_grant"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "principal_type",
            "principal_id",
            "scope_type",
            "scope_id",
            "role",
            name="uq_role_grant_principal_scope_role",
        ),
        Index("ix_role_grant_principal", "workspace_id", "principal_type", "principal_id"),
        Index("ix_role_grant_scope", "scope_type", "scope_id"),
        Index(
            "ix_role_grant_expiring",
            "expires_at",
            postgresql_where="expires_at IS NOT NULL",
        ),
        CheckConstraint(
            "scope_type <> 'workspace' OR scope_id = workspace_id",
            name="workspace_scope_id_matches",
        ),
    )

    principal_type: Mapped[PrincipalType] = mapped_column(enum_type(PrincipalType), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scope_type: Mapped[ScopeType] = mapped_column(enum_type(ScopeType), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[UserRole] = mapped_column(enum_type(UserRole), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
