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
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, Header, HTTPException, status

from forge_api.auth.apikeys import APIKeyInfo, APIKeyStore
from forge_api.auth.crypto import EnvelopeCipher, default_cipher
from forge_api.auth.keyring import KeyRing
from forge_api.auth.models import OAuthChallenge, OAuthResult
from forge_api.auth.oauth import OAuthClient, UnsupportedOAuthProviderError
from forge_api.auth.providers import get_default_provider, resolve_secret
from forge_api.auth.rbac import Permission, PermissionDeniedError, ensure
from forge_api.auth.vault import SecretVault
from forge_api.deps import Principal
from forge_api.observability.redaction import redact_text
from forge_api.services.auth_audit import AuthAuditEmitter
from forge_auth.errors import AuthenticationError as SdkAuthenticationError
from forge_auth.rbac import has_at_least
from forge_auth.tokens import decode_session_jwt, looks_like_jwt
from forge_contracts import ModelClient
from forge_contracts.audit import AuditSink
from forge_contracts.enums import APIKeyKind, UserRole


class AuthenticationError(Exception):
    """Raised when a presented credential is missing, unknown, or expired."""


#: Environments treated as development (no ``FORGE_SECRET_KEY`` required). Matches
#: ``forge_api.settings.Settings.environment`` (default ``"development"``).
_DEV_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})

#: Truthy string values for boolean env flags.
_TRUE = frozenset({"1", "true", "yes", "on"})

#: Default agent-runner token lifetime (seconds) — spec "automatic expiry".
_DEFAULT_AGENT_TOKEN_TTL = 86_400


def _subkey(master: bytes, label: bytes) -> bytes:
    """Derive a 32-byte subkey from the master secret (key separation)."""
    return hmac.new(master, label, hashlib.sha256).digest()


def _resolve_environment() -> str:
    """Resolve the canonical environment name (with the ``FORGE_ENV`` alias shim).

    ``FORGE_ENVIRONMENT`` is authoritative; the deprecated ``FORGE_ENV`` (shipped
    in the compose files) is honoured for one release with a loud warning, which
    is the concrete fix for the config-drift bug that mis-classified production
    as development.
    """
    env = resolve_secret("FORGE_ENVIRONMENT")
    if env:
        return env.strip().lower()
    legacy = resolve_secret("FORGE_ENV")
    if legacy:
        warnings.warn(
            "FORGE_ENV is deprecated and will be removed; set FORGE_ENVIRONMENT "
            "instead (the canonical name across api/worker/mcp-gateway).",
            DeprecationWarning,
            stacklevel=2,
        )
        return legacy.strip().lower()
    return "development"


def _is_development_environment() -> bool:
    """Whether the running environment is a development one (key optional)."""
    return _resolve_environment() in _DEV_ENVIRONMENTS


def _dev_insecure_enabled() -> bool:
    """Whether the explicit ``FORGE_DEV_INSECURE`` ephemeral-key opt-in is set."""
    val = resolve_secret("FORGE_DEV_INSECURE")
    return val is not None and val.strip().lower() in _TRUE


def _envelope_enabled() -> bool:
    """Whether envelope encryption is active (default: on in prod, off elsewhere)."""
    val = resolve_secret("FORGE_ENVELOPE_ENCRYPTION")
    if val is not None:
        return val.strip().lower() in _TRUE
    return _resolve_environment() == "production"


def _agent_token_ttl_seconds() -> int:
    """Default minted-agent-token lifetime from ``FORGE_AGENT_TOKEN_TTL``."""
    val = resolve_secret("FORGE_AGENT_TOKEN_TTL")
    if val:
        try:
            return int(val)
        except ValueError:
            return _DEFAULT_AGENT_TOKEN_TTL
    return _DEFAULT_AGENT_TOKEN_TTL


def _resolve_master_key(secret_key: bytes | None) -> bytes:
    """Return the instance master key from the arg or the secret provider.

    Resolution order:

    1. An explicit ``secret_key`` (injected by tests / embedders).
    2. ``FORGE_SECRET_KEY`` via the secret provider (env / file / Vault), with a
       one-release ``SECRET_KEY`` alias shim.
    3. Only when ``FORGE_DEV_INSECURE`` is explicitly enabled: a process-ephemeral
       random key, with a loud warning.

    A missing key with ``FORGE_DEV_INSECURE`` unset is a hard error *regardless of
    environment* — no environment string can accidentally land on the ephemeral
    path (HARD-13 fail-closed guarantee).
    """
    if secret_key is not None:
        return bytes(secret_key)
    val = resolve_secret("FORGE_SECRET_KEY")
    if val:
        return val.encode("utf-8")
    legacy = resolve_secret("SECRET_KEY")
    if legacy:
        warnings.warn(
            "SECRET_KEY is deprecated and will be removed; set FORGE_SECRET_KEY "
            "instead (the canonical name across api/worker/mcp-gateway).",
            DeprecationWarning,
            stacklevel=2,
        )
        return legacy.encode("utf-8")
    if _dev_insecure_enabled():
        warnings.warn(
            "FORGE_DEV_INSECURE is set: falling back to a process-ephemeral "
            "dev-only key. Encrypted secrets and minted API keys will NOT survive "
            "a restart. NEVER set FORGE_DEV_INSECURE in production.",
            stacklevel=2,
        )
        return secrets.token_bytes(32)
    raise RuntimeError(
        "FORGE_SECRET_KEY must be set; refusing to start with an ephemeral key "
        f"(FORGE_ENVIRONMENT={_resolve_environment()!r}). For a throwaway local "
        "dev server set FORGE_DEV_INSECURE=1 (never in production). Generate a key "
        "with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
    )


def _keyring_for_master(master: bytes) -> KeyRing:
    """Build a :class:`KeyRing` whose current KEK is the resolved ``master`` key.

    Older KEK versions (``FORGE_SECRET_KEY_V<n>``, retained during a rotation
    window) are read from the secret provider; ``FORGE_SECRET_KEY_VERSION`` (or
    the highest configured version, else 1) selects the current version, which
    the resolved ``master`` always occupies.
    """
    provider = get_default_provider()
    keys: dict[int, bytes] = {}
    for version in range(1, 256):
        material = provider.get(f"FORGE_SECRET_KEY_V{version}")
        if material:
            keys[version] = material.encode("utf-8")
    declared = provider.get("FORGE_SECRET_KEY_VERSION")
    current_version = int(declared) if declared else (max(keys) if keys else 1)
    keys[current_version] = master
    return KeyRing(keys, current_version)


def _build_vault(master: bytes) -> SecretVault:
    """Construct the vault, selecting envelope vs single-tier cipher via config."""
    cipher_subkey = _subkey(master, b"forge-cipher")
    if _envelope_enabled():
        keyring = _keyring_for_master(master)
        cipher = EnvelopeCipher(keyring, legacy=default_cipher(cipher_subkey))
        return SecretVault(cipher=cipher)
    return SecretVault(cipher=default_cipher(cipher_subkey))


class AuthService:
    """Facade over API-key auth, the encrypted vault, and RBAC."""

    def __init__(
        self,
        *,
        secret_key: bytes | None = None,
        api_keys: APIKeyStore | None = None,
        vault: SecretVault | None = None,
        oauth: OAuthClient | None = None,
        auth_secret: str | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        master = _resolve_master_key(secret_key)
        self.api_keys = api_keys or APIKeyStore(secret_key=_subkey(master, b"forge-apikey"))
        self.vault = vault or _build_vault(master)
        # Constructs without network access; the IdP is only contacted when an
        # authorization-code exchange is actually requested.
        self.oauth = oauth or OAuthClient.from_env()
        # F37 web↔API seam: the shared HS256 secret Better Auth signs the
        # session JWT with. Absent ⇒ the JWT auth path is disabled (API keys
        # keep working); never silently defaulted.
        self._auth_secret = auth_secret if auth_secret is not None else os.environ.get(
            "AUTH_SECRET"
        )
        # F37 audit producer: auth-domain events flow through the injected
        # AuditSink (F39's SqlAuditWriter at DB wire-up; a log-only fallback
        # otherwise so no event is silently dropped). Metadata is redacted by
        # the canonical forge_auth SecretRedactor inside the emitter.
        self.audit = AuthAuditEmitter(audit_sink)

    # -- authentication ----------------------------------------------------- #

    def authenticate(self, token: str) -> Principal:
        """Resolve a credential to a :class:`Principal` or raise.

        Order (spec §3.2 ``get_principal``): a session JWT minted by the web
        auth layer (HS256 over ``AUTH_SECRET``, ``aud=forge-api``) resolves a
        ``user`` principal; otherwise the token is verified as a platform API
        key. Anything else raises :class:`AuthenticationError` (→ 401).
        """
        from forge_api.auth.rbac import permissions_for

        if looks_like_jwt(token):
            if not self._auth_secret:
                raise AuthenticationError(
                    "session JWTs are not enabled (AUTH_SECRET is not configured)"
                )
            try:
                claims = decode_session_jwt(token, secret=self._auth_secret)
            except SdkAuthenticationError as exc:  # TokenExpired / InvalidToken
                raise AuthenticationError(str(exc)) from exc
            return Principal(
                user_id=claims.sub,
                workspace_id=claims.wsid,
                role=claims.role,
                email=claims.email,
                auth_method="session_jwt",
                scopes=[p.value for p in sorted(permissions_for(claims.role))],
            )

        record = self.api_keys.verify(token)
        if record is None:
            raise AuthenticationError("invalid or expired API key")

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

    def mint_agent_token(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        role: UserRole,
        user_id: uuid.UUID | None = None,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> tuple[APIKeyInfo, str]:
        """Mint a run-scoped agent-runner token with automatic expiry.

        Satisfies the spec's "automatic expiry for agent tokens": every minted
        agent token carries ``expires_at = now + FORGE_AGENT_TOKEN_TTL`` (default
        24h) so a dormant token cannot outlive its run. ``apikeys.verify`` rejects
        it once expired.
        """
        seconds = ttl_seconds if ttl_seconds is not None else _agent_token_ttl_seconds()
        reference = now or datetime.now(UTC)
        return self.api_keys.mint(
            workspace_id=workspace_id,
            name=name,
            role=role,
            kind=APIKeyKind.SYSTEM,
            user_id=user_id,
            expires_at=reference + timedelta(seconds=seconds),
        )

    # -- BYOK model client (HARD-02) ---------------------------------------- #

    def resolve_model_client(
        self,
        workspace_id: uuid.UUID,
        *,
        secret_id: uuid.UUID | None = None,
        redactor: Callable[[str], str] = redact_text,
    ) -> ModelClient:
        """Resolve a provider-agnostic BYOK :class:`ModelClient` for a workspace.

        The provider / model / limits come from the ``FORGE_MODEL_*`` environment;
        the BYOK key comes from the per-workspace vault when ``secret_id`` is given
        (``APIKeyKind.MODEL_PROVIDER``), else from the env key. The key is read per
        call and handed straight to the SDK client — never held in a module global,
        never logged. The injected ``redactor`` scrubs any provider exception before
        it is re-raised as ``ModelClientError``.

        Raises ``ModelClientError`` when no provider is configured, and
        ``ModelClientUnavailable`` when the provider SDK extra is not installed.
        """
        from forge_agent.providers import ModelClientConfig, ModelClientError, build_model_client

        if secret_id is not None:
            # The BYOK value comes from the per-workspace vault; provider / model /
            # limits still come from env. Inject the vault key via FORGE_MODEL_API_KEY
            # (dropping any env-provided key) so the vaulted secret always wins.
            key = self.vault.get_secret(workspace_id, secret_id)
            env = dict(os.environ)
            for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FORGE_MODEL_API_KEY"):
                env.pop(var, None)
            env["FORGE_MODEL_API_KEY"] = key
            config = ModelClientConfig.from_env(env)
        else:
            config = ModelClientConfig.from_env()

        if config is None:
            raise ModelClientError(
                "no model provider configured; set FORGE_MODEL_PROVIDER and a BYOK "
                "key (env or vault under MODEL_PROVIDER, + FORGE_MODEL_NAME for OpenAI)"
            )
        return build_model_client(config, redactor=redactor)

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


def require_role(min_role: UserRole) -> Callable[[Principal], Principal]:
    """Flat F37 RBAC dependency: authenticate, then assert role rank.

    ``ROLE_RANK[principal.role] >= ROLE_RANK[min_role]`` else 403. This is the
    contract ``cross-cutting/F30-multi-team-rbac`` shims onto scoped
    ``require_permission(...)`` grants; the permission-matrix variant above
    remains for capability-grained routes.
    """

    def _dependency(principal: AuthedPrincipal) -> Principal:
        if not has_at_least(principal.role, min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"forbidden: requires role '{min_role.value}' or higher",
            )
        return principal

    return _dependency


#: Convenience dependency for admin-only routes (spec §3.2).
require_admin = require_role(UserRole.ADMIN)


__all__ = [
    "AuthService",
    "AuthServiceDep",
    "AuthedPrincipal",
    "AuthenticationError",
    "get_auth_service",
    "get_authenticated_principal",
    "require_admin",
    "require_permission",
    "require_role",
]
