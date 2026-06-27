"""Auth service facade + FastAPI dependencies (Task 1.15 / H3 — auth & secrets).

Combines API-key authentication, the encrypted BYOK vault, and RBAC behind one
object the ``/auth/*`` router depends on, and provides the request-time
dependencies that authenticate a caller and enforce permissions.

Key management: the instance master secret (``FORGE_SECRET_KEY``) is split into
independent subkeys — one for the secret cipher, one for API-key hashing — so a
single configured secret bootstraps the whole auth layer with proper key
separation. In tests an explicit ``secret_key`` is injected for determinism.

``FORGE_SECRET_KEY`` is *required* in any non-development environment: there is
no silent ephemeral-key fallback in production (that would make every restart
discard the vault and rotate API-key verification). A development environment
(``FORGE_ENVIRONMENT`` unset or one of :data:`_DEV_ENVIRONMENTS`) keeps a
clearly-flagged, loudly-warned ephemeral-key path so the dev server still runs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
import warnings
from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, Header, HTTPException, status

from forge_api.auth.apikeys import APIKeyInfo, APIKeyStore
from forge_api.auth.crypto import default_cipher
from forge_api.auth.models import OAuthChallenge, OAuthResult
from forge_api.auth.oauth import OAuthClient, UnsupportedOAuthProviderError
from forge_api.auth.rbac import Permission, PermissionDeniedError, ensure
from forge_api.auth.vault import SecretVault
from forge_api.deps import Principal
from forge_contracts.enums import APIKeyKind, UserRole


class AuthenticationError(Exception):
    """Raised when a presented credential is missing, unknown, or expired."""


#: Environments treated as development (no ``FORGE_SECRET_KEY`` required). Matches
#: ``forge_api.settings.Settings.environment`` (default ``"development"``).
_DEV_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})


def _subkey(master: bytes, label: bytes) -> bytes:
    """Derive a 32-byte subkey from the master secret (key separation)."""
    return hmac.new(master, label, hashlib.sha256).digest()


def _is_development_environment() -> bool:
    """Whether the running environment is a development one (key optional)."""
    env = os.environ.get("FORGE_ENVIRONMENT", "development").strip().lower()
    return env in _DEV_ENVIRONMENTS


def _resolve_master_key(secret_key: bytes | None) -> bytes:
    """Return the instance master key from the arg or ``FORGE_SECRET_KEY``.

    Resolution order:

    1. An explicit ``secret_key`` (injected by tests / embedders).
    2. The ``FORGE_SECRET_KEY`` environment variable.
    3. Development only: a process-ephemeral random key, with a loud warning.

    In any non-development environment a missing ``FORGE_SECRET_KEY`` is a hard
    error — there is no silent ephemeral fallback in production.
    """
    if secret_key is not None:
        return bytes(secret_key)
    env = os.environ.get("FORGE_SECRET_KEY")
    if env:
        return env.encode("utf-8")
    if _is_development_environment():
        warnings.warn(
            "FORGE_SECRET_KEY is not set; falling back to a process-ephemeral "
            "dev-only key. Encrypted secrets and minted API keys will NOT survive "
            "a restart. Set FORGE_SECRET_KEY to a stable value before deploying.",
            stacklevel=2,
        )
        return secrets.token_bytes(32)
    raise RuntimeError(
        "FORGE_SECRET_KEY must be set in a non-development environment "
        f"(FORGE_ENVIRONMENT={os.environ.get('FORGE_ENVIRONMENT')!r}); refusing "
        "to start with an ephemeral key. Generate one with "
        "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
    )


class AuthService:
    """Facade over API-key auth, the encrypted vault, and RBAC."""

    def __init__(
        self,
        *,
        secret_key: bytes | None = None,
        api_keys: APIKeyStore | None = None,
        vault: SecretVault | None = None,
        oauth: OAuthClient | None = None,
    ) -> None:
        master = _resolve_master_key(secret_key)
        self.api_keys = api_keys or APIKeyStore(secret_key=_subkey(master, b"forge-apikey"))
        self.vault = vault or SecretVault(
            cipher=default_cipher(_subkey(master, b"forge-cipher"))
        )
        # Constructs without network access; the IdP is only contacted when an
        # authorization-code exchange is actually requested.
        self.oauth = oauth or OAuthClient.from_env()

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

        Returns the provider authorize URL (with ``client_id`` and requested
        scopes when configured) plus an anti-CSRF ``state``; the client redirects
        the user there and later hands the returned ``code`` back to
        :meth:`exchange_oauth_code` (the ``/auth/callback`` route).
        """
        try:
            config = self.oauth.provider_config(provider)
        except UnsupportedOAuthProviderError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        state = secrets.token_urlsafe(24)
        params = {"response_type": "code", "state": state}
        creds = self.oauth.credentials.get(config.name)
        if creds is not None:
            params["client_id"] = creds.client_id
        if config.scopes:
            params["scope"] = " ".join(config.scopes)
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        return OAuthChallenge(
            provider=config.name,
            authorize_url=f"{config.authorize_url}?{urlencode(params)}",
            state=state,
        )

    async def exchange_oauth_code(
        self,
        provider: str,
        code: str,
        *,
        redirect_uri: str | None = None,
        state: str | None = None,
        expected_state: str | None = None,
    ) -> OAuthResult:
        """Complete an OAuth flow: code -> tokens -> external user identity.

        Verifies ``state`` against ``expected_state`` when the latter is given,
        exchanges the authorization ``code`` for tokens at the provider token
        endpoint, then resolves the user from the userinfo endpoint. All network
        access flows through the injectable :class:`OAuthClient` transport, so the
        flow is fully mockable. Raises an :class:`~forge_api.auth.oauth.OAuthError`
        subclass on any failure (the ``/auth/callback`` route maps these to HTTP).
        """
        return await self.oauth.complete(
            provider,
            code,
            redirect_uri=redirect_uri,
            state=state,
            expected_state=expected_state,
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
