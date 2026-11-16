"""Auth & secrets layer for the SCF API (Task 1.15).

Public surface:
- :mod:`crypto` — authenticated encryption for secrets at rest (Fernet default).
- :mod:`vault` — encrypted, per-workspace BYOK secret store.
- :mod:`apikeys` — SCF API-key minting / verification (hashed, never stored raw).
- :mod:`rbac` — role -> permission matrix and evaluation helpers.
- :mod:`service` — facade + FastAPI auth/permission dependencies.
"""

from __future__ import annotations

from scf_api.auth.apikeys import APIKeyInfo, APIKeyStore, generate_api_token
from scf_api.auth.crypto import (
    EnvelopeCipher,
    FernetCipher,
    HmacAeadCipher,
    InvalidTokenError,
    default_cipher,
    envelope_cipher,
    generate_key,
)
from scf_api.auth.keyring import KeyRing
from scf_api.auth.oauth import (
    OAuthClient,
    OAuthClientCredentials,
    OAuthConfigError,
    OAuthError,
    OAuthExchangeError,
    OAuthProviderConfig,
    OAuthStateError,
    UnsupportedOAuthProviderError,
)
from scf_api.auth.providers import (
    ChainSecretProvider,
    EnvSecretProvider,
    FileSecretProvider,
    SecretProvider,
    VaultSecretProvider,
    build_provider,
    resolve_secret,
)
from scf_api.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    PermissionDeniedError,
    can,
    ensure,
    permissions_for,
)
from scf_api.auth.service import (
    AuthenticationError,
    AuthService,
    get_auth_service,
    get_authenticated_principal,
    require_admin,
    require_permission,
    require_role,
)
from scf_api.auth.vault import (
    SecretExpiredError,
    SecretInfo,
    SecretNotFoundError,
    SecretVault,
)

__all__ = [
    "ROLE_PERMISSIONS",
    "APIKeyInfo",
    "APIKeyStore",
    "AuthService",
    "AuthenticationError",
    "ChainSecretProvider",
    "EnvSecretProvider",
    "EnvelopeCipher",
    "FernetCipher",
    "FileSecretProvider",
    "HmacAeadCipher",
    "InvalidTokenError",
    "KeyRing",
    "OAuthClient",
    "OAuthClientCredentials",
    "OAuthConfigError",
    "OAuthError",
    "OAuthExchangeError",
    "OAuthProviderConfig",
    "OAuthStateError",
    "Permission",
    "PermissionDeniedError",
    "SecretExpiredError",
    "SecretInfo",
    "SecretNotFoundError",
    "SecretProvider",
    "SecretVault",
    "UnsupportedOAuthProviderError",
    "VaultSecretProvider",
    "build_provider",
    "can",
    "default_cipher",
    "ensure",
    "envelope_cipher",
    "generate_api_token",
    "generate_key",
    "get_auth_service",
    "get_authenticated_principal",
    "permissions_for",
    "require_admin",
    "require_permission",
    "require_role",
    "resolve_secret",
]
