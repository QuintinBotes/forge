"""Tenant root and identity models: Workspace, User, APIKey, SignInLink, DeviceAuthorization."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scf_db.base import ScfModel, WorkspaceScopedModel, enum_type, json_type
from scf_db.models.enums import APIKeyKind, UserRole

if TYPE_CHECKING:
    from scf_db.models.knowledge import KnowledgeSource
    from scf_db.models.project import Project


class Workspace(ScfModel):
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


class SignInLink(WorkspaceScopedModel):
    """A one-time sign-in link for a workspace administrator (#260).

    Holds only a SHA-256 hash of the link's token; ``used_at`` is set once, by the
    redemption that consumes it.
    """

    __tablename__ = "sign_in_link"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial, secret-safe
        return f"SignInLink(id={self.id!r}, user_id={self.user_id!r}, used_at={self.used_at!r})"


class DeviceAuthorization(ScfModel):
    """A device sign-in a CLI client started (#207, RFC 8628).

    Holds only SHA-256 hashes of the device code and the user code, plus the user
    code's first half (``user_code_selector``), which names the request so that wrong
    codes count against it. The workspace and the person are unknown until someone
    approves or denies it, so both are nullable. A pending request's user code is
    unique, so an approval names exactly one request.
    """

    __tablename__ = "device_authorization"
    __table_args__ = (
        Index(
            "uq_device_authorization_pending_user_code",
            "user_code_hash",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=True
    )
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_code_selector: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    user_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: pending, approved, denied, issued (the token was returned), invalid (wrong codes).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), nullable=False
    )
    #: Who asked, as the API saw them, for the approver and the audit log.
    requester_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    requester_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial, secret-safe
        return f"DeviceAuthorization(id={self.id!r}, status={self.status!r})"
