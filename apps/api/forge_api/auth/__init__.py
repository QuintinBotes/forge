"""Auth & secrets layer for the Forge API (Task 1.15).

Public surface:
- :mod:`crypto` — authenticated encryption for secrets at rest (Fernet default).
- :mod:`vault` — encrypted, per-workspace BYOK secret store.
- :mod:`apikeys` — Forge API-key minting / verification (hashed, never stored raw).
- :mod:`rbac` — role -> permission matrix and evaluation helpers.
- :mod:`service` — facade + FastAPI auth/permission dependencies.
"""

from __future__ import annotations

from forge_api.auth.apikeys import APIKeyInfo, APIKeyStore, generate_api_token
from forge_api.auth.crypto import (
    FernetCipher,
    HmacAeadCipher,
    InvalidTokenError,
    default_cipher,
    generate_key,
)
from forge_api.auth.oauth import (
    OAuthClient,
    OAuthClientCredentials,
    OAuthConfigError,
    OAuthError,
    OAuthExchangeError,
    OAuthProviderConfig,
    OAuthStateError,
    UnsupportedOAuthProviderError,
)
from forge_api.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    PermissionDeniedError,
    can,
    ensure,
    permissions_for,
)
from forge_api.auth.service import (
    AuthenticationError,
    AuthService,
    get_auth_service,
    get_authenticated_principal,
    require_admin,
    require_permission,
    require_role,
)
from forge_api.auth.vault import SecretInfo, SecretNotFoundError, SecretVault

__all__ = [
    "ROLE_PERMISSIONS",
    "APIKeyInfo",
    "APIKeyStore",
    "AuthService",
    "AuthenticationError",
    "FernetCipher",
    "HmacAeadCipher",
    "InvalidTokenError",
    "OAuthClient",
    "OAuthClientCredentials",
    "OAuthConfigError",
    "OAuthError",
    "OAuthExchangeError",
    "OAuthProviderConfig",
    "OAuthStateError",
    "Permission",
    "PermissionDeniedError",
    "SecretInfo",
    "SecretNotFoundError",
    "SecretVault",
    "UnsupportedOAuthProviderError",
    "can",
    "default_cipher",
    "ensure",
    "generate_api_token",
    "generate_key",
    "get_auth_service",
    "get_authenticated_principal",
    "permissions_for",
    "require_admin",
    "require_permission",
    "require_role",
]
