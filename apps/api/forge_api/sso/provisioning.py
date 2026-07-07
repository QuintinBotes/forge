"""Shared SSO provisioning / deprovisioning (F33) — used by SAML *and* SCIM.

``link_or_jit_provision`` is the single path from a validated identity to a
Forge ``app_user``: link by external id, else attach to an existing user by
email, else JIT-create (bounded to the one workspace owning the IdP config —
a NameID can never provision into another tenant).

``deprovision_user`` is the immediate-revocation path (SCIM ``active=false`` /
``DELETE``): deactivates the user, revokes their sessions/agent tokens through
the injected revoker (F37's in-memory API-key store in this foundation), and
writes the ``sso.user_deprovisioned`` audit event on the caller's session
(fail-closed: a failed audit write rolls the whole action back).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forge_api.observability.redaction import redact_mapping
from forge_api.services.audit import SqlAuditWriter
from forge_api.sso.errors import LastAdminError, SsoConfigError
from forge_contracts.audit import AuditEvent
from forge_contracts.sso import MappedIdentity
from forge_db.models import ExternalIdentity, SsoConfiguration, User
from forge_db.models.enums import ExternalIdentityProvider, UserRole

#: Revokes every session / agent token owned by (workspace_id, user_id).
SessionRevoker = Callable[[uuid.UUID, uuid.UUID], int]


def _now() -> datetime:
    return datetime.now(UTC)


def emit_sso_audit(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "user",
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    result: str = "success",
    details: dict | None = None,
) -> None:
    """Write one immutable, redacted audit event on the caller's session."""
    SqlAuditWriter(session).emit(
        AuditEvent(
            workspace_id=workspace_id,
            action=action,
            actor_id=actor_id,
            actor_type=actor_type,
            target_type=target_type,
            target_id=target_id,
            result=result,
            details=redact_mapping({"severity": "critical", **(details or {})}),
        )
    )


def count_local_active_admins(
    session: Session, workspace_id: uuid.UUID, *, excluding: uuid.UUID | None = None
) -> int:
    """Active break-glass admins: role=admin, active, not directory-managed."""
    stmt = (
        select(func.count())
        .select_from(User)
        .where(
            User.workspace_id == workspace_id,
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
            User.external_managed.is_(False),
        )
    )
    if excluding is not None:
        stmt = stmt.where(User.id != excluding)
    return int(session.execute(stmt).scalar_one())


def ensure_not_last_admin(session: Session, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Raise :class:`LastAdminError` if deactivating ``user_id`` orphans the tenant."""
    user = session.get(User, user_id)
    if user is None or user.workspace_id != workspace_id:
        return
    is_local_admin = user.role == UserRole.ADMIN and user.is_active and not user.external_managed
    if is_local_admin and count_local_active_admins(session, workspace_id, excluding=user_id) == 0:
        raise LastAdminError(
            "cannot deprovision the last active local admin (break-glass protection)"
        )


def link_or_jit_provision(
    *,
    session: Session,
    config: SsoConfiguration,
    identity: MappedIdentity,
    provider: ExternalIdentityProvider = ExternalIdentityProvider.SAML,
) -> User:
    """Resolve a validated identity to a Forge user (link → attach → JIT).

    Role is written to the flat ``app_user.role`` (the F37 substrate; when the
    F30 grant API becomes the authz source of truth the resolved role is applied
    there instead). Raises :class:`SsoConfigError` when JIT is disabled and no
    user exists, or when the target user is deactivated.
    """
    link = session.execute(
        select(ExternalIdentity).where(
            ExternalIdentity.workspace_id == config.workspace_id,
            ExternalIdentity.provider == provider,
            ExternalIdentity.external_id == identity.external_id,
        )
    ).scalar_one_or_none()

    if link is not None:
        user = session.get(User, link.user_id)
        if user is None:
            raise SsoConfigError("external identity references a missing user")
        if not user.is_active:
            raise SsoConfigError("user is deactivated")
        if identity.name and user.name != identity.name:
            user.name = identity.name
        if user.role != UserRole(identity.role):
            user.role = UserRole(identity.role)
        link.last_login_at = _now()
        session.flush()
        return user

    user = session.execute(
        select(User).where(
            User.workspace_id == config.workspace_id,
            func.lower(User.email) == identity.email.lower(),
        )
    ).scalar_one_or_none()

    if user is None:
        if not config.jit_provisioning:
            raise SsoConfigError("JIT provisioning is disabled and no matching user exists")
        user = User(
            workspace_id=config.workspace_id,
            email=identity.email,
            name=identity.name,
            role=UserRole(identity.role),
            is_active=True,
            auth_provider=provider.value,
            auth_subject=identity.external_id,
        )
        session.add(user)
        session.flush()
        emit_sso_audit(
            session,
            workspace_id=config.workspace_id,
            action="sso.user_provisioned",
            actor_type="idp",
            target_type="user",
            target_id=user.id,
            details={"email": identity.email, "role": identity.role},
        )
    else:
        if not user.is_active:
            raise SsoConfigError("user is deactivated")
        if identity.name and user.name != identity.name:
            user.name = identity.name
        if user.role != UserRole(identity.role):
            user.role = UserRole(identity.role)

    session.add(
        ExternalIdentity(
            workspace_id=config.workspace_id,
            user_id=user.id,
            provider=provider,
            idp_entity_id=config.idp_entity_id if provider.value == "saml" else None,
            external_id=identity.external_id,
            name_id_format=identity.name_id_format,
            last_login_at=_now(),
        )
    )
    session.flush()
    return user


def deprovision_user(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    revoke_sessions: SessionRevoker,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "scim",
) -> User:
    """Deactivate + immediately revoke a user (SCIM ``active=false`` / DELETE)."""
    user = session.get(User, user_id)
    if user is None or user.workspace_id != workspace_id:
        raise SsoConfigError("user not found in workspace")
    ensure_not_last_admin(session, workspace_id, user_id)
    user.is_active = False
    user.deactivated_at = _now()
    revoked = revoke_sessions(workspace_id, user_id)
    emit_sso_audit(
        session,
        workspace_id=workspace_id,
        action="sso.user_deprovisioned",
        actor_id=actor_id,
        actor_type=actor_type,
        target_type="user",
        target_id=user_id,
        details={"revoked_credentials": revoked},
    )
    session.flush()
    return user


__all__ = [
    "SessionRevoker",
    "count_local_active_admins",
    "deprovision_user",
    "emit_sso_audit",
    "ensure_not_last_admin",
    "link_or_jit_provision",
]
