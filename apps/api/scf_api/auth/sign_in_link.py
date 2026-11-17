"""One-time sign-in links for the first administrator of a workspace (#260, FR-010).

``scf-cli admin sign-in-link`` issues a link on the server; ``GET /auth/sign-in-link``
redeems it into the session cookie an SSO sign-in sets. The link carries 32 random
bytes, of which only a SHA-256 hash is stored. It works once, until it expires, and
only for an active administrator of a workspace with no enabled identity provider.
Every failure is a refusal with no session (ADR-0007).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.orm import Session

from scf_api.sso.provisioning import emit_sso_audit
from scf_db.models import OidcConfiguration, SignInLink, SsoConfiguration, User, Workspace
from scf_db.models.enums import UserRole

#: Where a link is redeemed, under ``public_url`` (the ``/auth`` router).
REDEEM_PATH = "/auth/sign-in-link"
TOKEN_BYTES = 32


class SignInLinkRefused(Exception):
    """A link cannot be issued; the message says why, for the operator."""


@dataclass(frozen=True)
class IssuedLink:
    url: str
    expires_at: datetime


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _has_enabled_identity_provider(session: Session, workspace_id: uuid.UUID) -> bool:
    return any(
        session.scalar(
            select(config.id).where(config.workspace_id == workspace_id, config.enabled.is_(True))
        )
        is not None
        for config in (OidcConfiguration, SsoConfiguration)
    )


def issue(
    session: Session,
    *,
    workspace_slug: str,
    email: str,
    public_url: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> IssuedLink:
    """Issue a link for ``email`` in ``workspace_slug`` and commit; raise if refused.

    A workspace with no administrator gets one with ``email``; otherwise ``email`` must
    name an active administrator. The user's earlier unredeemed links stop working.
    """
    now = now or datetime.now(UTC)
    email = email.strip()
    if "@" not in email:
        raise SignInLinkRefused(f"{email!r} is not an email address")
    workspace = session.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
    if workspace is None:
        raise SignInLinkRefused(f"no workspace {workspace_slug!r}")
    if _has_enabled_identity_provider(session, workspace.id):
        raise SignInLinkRefused(
            f"workspace {workspace_slug!r} has an enabled identity provider: sign in through it"
        )
    in_workspace = select(User).where(
        User.workspace_id == workspace.id, func.lower(User.email) == email.lower()
    )
    admins = select(User.id).where(User.workspace_id == workspace.id, User.role == UserRole.ADMIN)
    if session.scalar(admins.limit(1)) is None:
        if session.scalar(in_workspace) is not None:
            raise SignInLinkRefused(f"{email!r} is a user of {workspace_slug!r}, not an admin")
        user = User(workspace_id=workspace.id, email=email, role=UserRole.ADMIN, is_active=True)
        session.add(user)
        session.flush()
    else:
        found = session.scalar(
            in_workspace.where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        if found is None:
            raise SignInLinkRefused(
                f"{email!r} is not an active administrator of {workspace_slug!r}"
            )
        user = found

    session.execute(
        delete(SignInLink).where(SignInLink.user_id == user.id, SignInLink.used_at.is_(None))
    )
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = now + timedelta(seconds=ttl_seconds)
    session.add(
        SignInLink(
            workspace_id=workspace.id,
            user_id=user.id,
            token_hash=_hash(token),
            expires_at=expires_at,
        )
    )
    emit_sso_audit(
        session,
        workspace_id=workspace.id,
        action="auth.sign_in_link_issued",
        actor_type="system",
        target_type="user",
        target_id=user.id,
        details={"expires_at": expires_at.isoformat()},
    )
    session.commit()
    return IssuedLink(
        url=f"{public_url.rstrip('/')}{REDEEM_PATH}?token={token}", expires_at=expires_at
    )


def redeem(session: Session, token: str, *, now: datetime | None = None) -> User | None:
    """Consume the link ``token`` names and return its administrator, else ``None``.

    One conditional UPDATE consumes the link, so of concurrent redemptions exactly one
    succeeds. A link refused after that (its user is no longer an active admin, or the
    workspace has enabled an identity provider since) stays consumed.
    """
    now = now or datetime.now(UTC)
    token_hash = _hash(token)
    # Matched by the hash of 256 random bits, so the lookup's timing reveals nothing usable.
    consumed = cast(
        "CursorResult[Any]",
        session.execute(
            update(SignInLink)
            .where(
                SignInLink.token_hash == token_hash,
                SignInLink.used_at.is_(None),
                SignInLink.expires_at > now,
            )
            .values(used_at=now)
        ),
    )
    if consumed.rowcount != 1:
        session.rollback()
        return None
    link = session.scalars(select(SignInLink).where(SignInLink.token_hash == token_hash)).one()
    user = session.get(User, link.user_id)
    if (
        user is None
        or user.workspace_id != link.workspace_id
        or not user.is_active
        or user.role != UserRole.ADMIN
        or _has_enabled_identity_provider(session, link.workspace_id)
    ):
        session.commit()
        return None
    emit_sso_audit(
        session,
        workspace_id=link.workspace_id,
        action="auth.sign_in_link_redeemed",
        actor_id=user.id,
        actor_type="user",
        target_type="user",
        target_id=user.id,
    )
    session.commit()
    return user
