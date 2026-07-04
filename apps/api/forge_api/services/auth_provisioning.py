"""F37 OAuth provisioning: external identity → Forge ``User`` (+ ``Workspace``).

Implements the DB-backed provisioning half of the web↔API auth seam (spec
§3.2 ``auth_service``): the web auth layer completes the OAuth dance and calls
into Forge with the provider identity; this service resolves it to a ``User``
(creating the ``Workspace``/``User``/``oauth_account`` linkage on first
sign-in) and manages additional identity links.

Rules (AC9/AC10):

* Idempotent on ``(provider, subject)`` — the ``oauth_account`` unique
  constraint is the source of truth; a repeat call returns the same user.
* First user in a fresh workspace becomes ``admin``; later users ``member``.
* A second provider may be linked to the same user; unlinking the **last**
  login method is refused (the account would become unreachable).

Every mutation emits a canonical ``AuditEvent`` on the caller's session via
F39's ``SqlAuditWriter`` (fail-closed: the audit row and the mutation commit
in one transaction).
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forge_api.services.audit import SqlAuditWriter
from forge_contracts.audit import AuditEvent
from forge_contracts.auth import OAuthProvider
from forge_db.models import OAuthAccount, User, UserRole, Workspace

__all__ = ["LastLoginMethodError", "OAuthProvisioningService", "workspace_slug_for_email"]


class LastLoginMethodError(ValueError):
    """Refusing to unlink a user's only remaining login method (→ HTTP 409)."""


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def workspace_slug_for_email(email: str | None) -> str:
    """Derive a workspace slug from the sign-in email's domain (Journey A)."""
    _, sep, domain_full = (email or "").rpartition("@")
    if not sep:
        return "workspace"
    domain = domain_full.partition(".")[0].lower()
    slug = _SLUG_RE.sub("-", domain).strip("-")
    return slug or "workspace"


class OAuthProvisioningService:
    """Find-or-provision Forge identities from OAuth sign-ins (DB-backed)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._audit = SqlAuditWriter(session)

    # -- provisioning -------------------------------------------------------- #

    def provision_from_oauth(
        self,
        *,
        provider: OAuthProvider,
        subject: str,
        email: str | None = None,
        name: str | None = None,
    ) -> User:
        """Resolve ``(provider, subject)`` to a ``User``, provisioning on first login."""
        linked = self._session.scalar(
            select(OAuthAccount).where(
                OAuthAccount.provider == provider,
                OAuthAccount.provider_subject == subject,
            )
        )
        if linked is not None:
            user = self._session.get(User, linked.user_id)
            if user is None:  # pragma: no cover - FK CASCADE prevents this
                raise LookupError(f"oauth_account {linked.id} points at a missing user")
            return user

        if not email:
            raise ValueError("email is required to provision a new user")

        workspace = self._find_or_create_workspace(email)
        is_first_user = (
            self._session.scalar(
                select(func.count()).select_from(User).where(User.workspace_id == workspace.id)
            )
            == 0
        )
        user = User(
            workspace_id=workspace.id,
            email=email,
            name=name,
            role=UserRole.ADMIN if is_first_user else UserRole.MEMBER,
            auth_provider=provider.value,
            auth_subject=subject,
        )
        self._session.add(user)
        self._session.flush()
        self._session.add(
            OAuthAccount(
                workspace_id=workspace.id,
                user_id=user.id,
                provider=provider,
                provider_subject=subject,
                email=email,
            )
        )
        self._session.flush()
        self._audit.emit(
            AuditEvent(
                workspace_id=workspace.id,
                action="auth.user_provisioned",
                actor_id=user.id,
                actor_type="user",
                target_type="user",
                target_id=user.id,
                details={"provider": provider.value, "role": user.role.value},
            )
        )
        return user

    def _find_or_create_workspace(self, email: str) -> Workspace:
        slug = workspace_slug_for_email(email)
        workspace = self._session.scalar(select(Workspace).where(Workspace.slug == slug))
        if workspace is None:
            workspace = Workspace(name=slug.replace("-", " ").title(), slug=slug)
            self._session.add(workspace)
            self._session.flush()
        return workspace

    # -- identity linking (Journey B) ---------------------------------------- #

    def link_oauth(
        self,
        *,
        user: User,
        provider: OAuthProvider,
        subject: str,
        email: str | None = None,
    ) -> OAuthAccount:
        """Attach an additional external identity to an existing user."""
        account = OAuthAccount(
            workspace_id=user.workspace_id,
            user_id=user.id,
            provider=provider,
            provider_subject=subject,
            email=email,
        )
        self._session.add(account)
        self._session.flush()
        self._audit.emit(
            AuditEvent(
                workspace_id=user.workspace_id,
                action="auth.oauth_linked",
                actor_id=user.id,
                actor_type="user",
                target_type="oauth_account",
                target_id=account.id,
                details={"provider": provider.value},
            )
        )
        return account

    def unlink_oauth(self, *, user: User, account_id: uuid.UUID) -> None:
        """Detach a linked identity; the last login method cannot be removed."""
        account = self._session.get(OAuthAccount, account_id)
        if account is None or account.user_id != user.id:
            raise LookupError(f"oauth account {account_id} not linked to this user")
        remaining = self._session.scalar(
            select(func.count()).select_from(OAuthAccount).where(OAuthAccount.user_id == user.id)
        )
        if remaining is not None and remaining <= 1:
            raise LastLoginMethodError("cannot unlink the last remaining login method")
        provider = account.provider
        self._session.delete(account)
        self._session.flush()
        self._audit.emit(
            AuditEvent(
                workspace_id=user.workspace_id,
                action="auth.oauth_unlinked",
                actor_id=user.id,
                actor_type="user",
                target_type="oauth_account",
                target_id=account_id,
                details={"provider": provider.value},
            )
        )
