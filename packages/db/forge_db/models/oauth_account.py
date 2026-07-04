"""Linked external OAuth identities (F37): ``oauth_account``.

Many external identities → one Forge user (multi-provider linking, Journey B).
**No provider access/refresh tokens are stored here** — the web auth runtime
holds OAuth tokens in its own tables; Forge stores only the linkage needed to
resolve a login to a :class:`~forge_db.models.workspace.User`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type
from forge_db.models.enums import OAuthProvider


class OAuthAccount(WorkspaceScopedModel):
    """One linked ``(provider, subject)`` identity resolving to a Forge user."""

    __tablename__ = "oauth_account"
    __table_args__ = (
        # One external identity → one Forge user, globally.
        UniqueConstraint("provider", "provider_subject", name="uq_oauth_account_provider"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[OAuthProvider] = mapped_column(enum_type(OAuthProvider), nullable=False)
    #: The provider's stable ``sub`` for this identity.
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Email asserted by the provider at link time (informational).
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
