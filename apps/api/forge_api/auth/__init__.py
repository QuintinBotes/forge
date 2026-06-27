"""Auth & secrets layer for the Forge API (Task 1.15).

Public surface:
- :mod:`crypto` — standard-library authenticated encryption for secrets at rest.
- :mod:`vault` — encrypted, per-workspace BYOK secret store.
- :mod:`apikeys` — Forge API-key minting / verification (hashed, never stored raw).
- :mod:`rbac` — role -> permission matrix and evaluation helpers.
- :mod:`service` — facade + FastAPI auth/permission dependencies.
"""

from __future__ import annotations

from forge_api.auth.apikeys import APIKeyInfo, APIKeyStore, generate_api_token
from forge_api.auth.crypto import HmacAeadCipher, InvalidTokenError, generate_key
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
    require_permission,
)
from forge_api.auth.vault import SecretInfo, SecretNotFoundError, SecretVault

__all__ = [
    "ROLE_PERMISSIONS",
    "APIKeyInfo",
    "APIKeyStore",
    "AuthService",
    "AuthenticationError",
    "HmacAeadCipher",
    "InvalidTokenError",
    "Permission",
    "PermissionDeniedError",
    "SecretInfo",
    "SecretNotFoundError",
    "SecretVault",
    "can",
    "ensure",
    "generate_api_token",
    "generate_key",
    "get_auth_service",
    "get_authenticated_principal",
    "permissions_for",
    "require_permission",
]
