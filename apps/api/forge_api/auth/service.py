"""Auth service facade + FastAPI dependencies (Task 1.15 — auth & secrets).

Combines API-key authentication, the encrypted BYOK vault, and RBAC behind one
object the ``/auth/*`` router depends on, and provides the request-time
dependencies that authenticate a caller and enforce permissions.

Key management: the instance master secret (``FORGE_SECRET_KEY``) is split into
independent subkeys — one for the secret cipher, one for API-key hashing — so a
single configured secret bootstraps the whole auth layer with proper key
separation. In tests an explicit ``secret_key`` is injected for determinism.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, Header, HTTPException, status

from forge_api.auth.apikeys import APIKeyInfo, APIKeyStore
from forge_api.auth.crypto import HmacAeadCipher
from forge_api.auth.models import OAuthChallenge
from forge_api.auth.rbac import Permission, PermissionDeniedError, ensure
from forge_api.auth.vault import SecretVault
from forge_api.deps import Principal
from forge_contracts.enums import APIKeyKind, UserRole

#: Provider authorize endpoints for the OAuth sign-in descriptor (V1: Google,
#: GitHub, GitLab per spec). No tokens are exchanged here.
_OAUTH_AUTHORIZE_URLS: dict[str, str] = {
    "google": "https://accounts.google.com/o/oauth2/v2/auth",
    "github": "https://github.com/login/oauth/authorize",
    "gitlab": "https://gitlab.com/oauth/authorize",
}


class AuthenticationError(Exception):
    """Raised when a presented credential is missing, unknown, or expired."""


def _subkey(master: bytes, label: bytes) -> bytes:
    """Derive a 32-byte subkey from the master secret (key separation)."""
    return hmac.new(master, label, hashlib.sha256).digest()


def _resolve_master_key(secret_key: bytes | None) -> bytes:
    """Return the instance master key from the arg or ``FORGE_SECRET_KEY``.

    Falls back to a process-ephemeral random key in development so the service is
    always usable; a stable ``FORGE_SECRET_KEY`` is required to persist secrets
    across restarts (documented for self-hosting).
    """
    if secret_key is not None:
        return bytes(secret_key)
    env = os.environ.get("FORGE_SECRET_KEY")
    if env:
        return env.encode("utf-8")
    # PARKED-FOR-PROD: no FORGE_SECRET_KEY set — use an ephemeral key so the dev
    # server runs; secrets will not survive a restart until a key is configured.
    return secrets.token_bytes(32)


class AuthService:
    """Facade over API-key auth, the encrypted vault, and RBAC."""

    def __init__(
        self,
        *,
        secret_key: bytes | None = None,
        api_keys: APIKeyStore | None = None,
        vault: SecretVault | None = None,
    ) -> None:
        master = _resolve_master_key(secret_key)
        self.api_keys = api_keys or APIKeyStore(secret_key=_subkey(master, b"forge-apikey"))
        self.vault = vault or SecretVault(
            cipher=HmacAeadCipher(_subkey(master, b"forge-cipher"))
        )

    # -- authentication ----------------------------------------------------- #

    def authenticate(self, token: str) -> Principal:
        """Verify an API-key token and return the resolved :class:`Principal`."""
        record = self.api_keys.verify(token)
        if record is None:
            raise AuthenticationError("invalid or expired API key")
        from forge_api.auth.rbac import permissions_for

        return Principal(
            user_id=record.user_id or record.id,
            workspace_id=record.workspace_id,
            role=record.role,
            email=None,
            auth_method="api_key",
            scopes=[p.value for p in sorted(permissions_for(record.role))],
        )

    def bootstrap_key(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        role: UserRole,
        user_id: uuid.UUID | None = None,
        kind: APIKeyKind = APIKeyKind.SYSTEM,
        expires_at: datetime | None = None,
    ) -> tuple[APIKeyInfo, str]:
        """Mint an API key directly (CLI ``users create-admin`` / seed / tests)."""
        return self.api_keys.mint(
            workspace_id=workspace_id,
            name=name,
            role=role,
            kind=kind,
            user_id=user_id,
            expires_at=expires_at,
        )

    # -- OAuth descriptor --------------------------------------------------- #

    def oauth_challenge(
        self, provider: str, redirect_uri: str | None = None
    ) -> OAuthChallenge:
        """Build an OAuth authorization-code descriptor (no external call).

        # PARKED: the authorization-code *exchange* (callback -> tokens -> user
        # provisioning) requires a live provider and client credentials and is
        # not performed in this no-network phase; the frontend / Better Auth
        # completes the flow. See MORNING_REPORT.
        """
        base = _OAUTH_AUTHORIZE_URLS.get(provider.lower())
        if base is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported OAuth provider '{provider}'",
            )
        state = secrets.token_urlsafe(24)
        params = {"response_type": "code", "state": state}
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        return OAuthChallenge(
            provider=provider.lower(),
            authorize_url=f"{base}?{urlencode(params)}",
            state=state,
        )


_default_service: AuthService | None = None


def get_auth_service() -> AuthService:
    """FastAPI dependency returning the process-wide auth service.

    Overridable via ``app.dependency_overrides`` for isolated tests.
    """
    global _default_service
    if _default_service is None:
        _default_service = AuthService()
    return _default_service


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def _extract_token(authorization: str | None, x_api_key: str | None) -> str | None:
    """Pull a bearer token from ``Authorization`` or fall back to ``X-API-Key``."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def get_authenticated_principal(
    service: AuthServiceDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Authenticate the request via API key; raise 401 if absent/invalid."""
    token = _extract_token(authorization, x_api_key)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return service.authenticate(token)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired API key",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


AuthedPrincipal = Annotated[Principal, Depends(get_authenticated_principal)]


def require_permission(permission: Permission) -> Callable[[Principal], Principal]:
    """Build a dependency callable that authenticates then enforces ``permission``.

    Use in a route via ``Depends(require_permission(Permission.MANAGE_KEYS))``;
    yields a 401 if unauthenticated and a 403 if the role lacks the permission.
    """

    def _dependency(principal: AuthedPrincipal) -> Principal:
        try:
            ensure(principal.role, permission)
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        return principal

    return _dependency


__all__ = [
    "AuthService",
    "AuthServiceDep",
    "AuthedPrincipal",
    "AuthenticationError",
    "get_auth_service",
    "get_authenticated_principal",
    "require_permission",
]
