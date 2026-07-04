"""Inbound platform API-key table (F37): ``platform_api_key``.

Naming clarity (load-bearing, spec §3.1): ``api_key`` = BYOK secrets Forge
stores and **decrypts** to use *outbound*; ``platform_api_key`` (this table) =
inbound auth tokens Forge only **verifies** — one-way peppered HMAC hash,
never decryptable. The plaintext token is returned exactly once at mint time.

Revoke = set ``revoked_at`` (kept for audit), never delete. ``agent_runner``
keys must carry ``expires_at`` (Security: automatic expiry for agent tokens);
expiry is authoritative at verify time, the worker beat task is hygiene+audit.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type
from forge_db.models.enums import PlatformKeyKind, UserRole


class PlatformAPIKey(WorkspaceScopedModel):
    """An inbound machine/agent auth token (hashed; shown once at creation)."""

    __tablename__ = "platform_api_key"
    __table_args__ = (
        UniqueConstraint("key_id", name="uq_platform_api_key_key_id"),
        CheckConstraint(
            "kind != 'agent_runner' OR expires_at IS NOT NULL",
            name="agent_runner_expires",
        ),
        Index(
            "ix_platform_api_key_expiring",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Non-secret public lookup id embedded in the token (hot auth lookup).
    key_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    #: Hex HMAC-SHA256(pepper, secret) — one-way, never the token itself.
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Masked display form, e.g. ``forge_svc_a1b2c3d4…wxyz``.
    key_prefix: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[PlatformKeyKind] = mapped_column(enum_type(PlatformKeyKind), nullable=False)
    #: Role this key authenticates as; capped at the creator's role (AC8).
    role: Mapped[UserRole] = mapped_column(enum_type(UserRole), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    #: Refreshed on verify (throttled write; see slice §3.2).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: NULL = no expiry; required for ``agent_runner`` (CHECK above).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Non-null ⇒ rejected immediately; rows are kept for audit.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial, secret-safe
        return (
            f"PlatformAPIKey(id={self.id!r}, name={self.name!r}, key_id={self.key_id!r}, "
            f"kind={self.kind!r}, role={self.role!r}, hash=<redacted>)"
        )
