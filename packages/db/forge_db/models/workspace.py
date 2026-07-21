"""Tenant root and identity models: Workspace, User, APIKey."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge_db.base import ForgeModel, WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import APIKeyKind, UserRole

if TYPE_CHECKING:
    from forge_db.models.knowledge import KnowledgeSource
    from forge_db.models.project import Project


class Workspace(ForgeModel):
    """The tenant root. Everything else is scoped to a workspace."""

    __tablename__ = "workspace"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)

    users: Mapped[list[User]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    projects: Mapped[list[Project]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    knowledge_sources: Mapped[list[KnowledgeSource]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class User(WorkspaceScopedModel):
    """A workspace member with an RBAC role."""

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole), default=UserRole.MEMBER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    auth_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # F33 enterprise SSO: when the user was deprovisioned (SCIM active=false /
    # DELETE), distinct from is_active so the *when* survives re-activation; and
    # whether the directory (SCIM) owns this user's lifecycle.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_managed: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="users")


class APIKey(WorkspaceScopedModel):
    """BYOK credential (model provider / integration / MCP token).

    The secret is stored encrypted at rest; ``__repr__`` never reveals it.
    """

    __tablename__ = "api_key"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[APIKeyKind] = mapped_column(enum_type(APIKeyKind), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_prefix: Mapped[str | None] = mapped_column(String(32), nullable=True)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # HARD-13 envelope encryption: the KEK version the row's data key is wrapped
    # under (lets KEK rotation target ``WHERE key_version < :current`` cheaply),
    # and when the DEK was last re-wrapped (rotation audit). The wrapped DEK
    # itself travels inside ``encrypted_secret`` (self-describing envelope blob).
    key_version: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default=text("1"), nullable=False, index=True
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial, secret-safe
        return (
            f"APIKey(id={self.id!r}, name={self.name!r}, "
            f"kind={self.kind!r}, provider={self.provider!r}, secret=<redacted>)"
        )
