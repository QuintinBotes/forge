"""forge_auth — the pure auth & secrets crypto core (F37).

No FastAPI / SQLAlchemy imports (mirrors the ``policy-sdk`` / ``authz-sdk``
discipline). The API/worker layers wire these primitives; the frozen DTOs and
Protocols live in :mod:`forge_contracts.auth`.
"""

from __future__ import annotations

from forge_auth.errors import (
    AuthenticationError,
    AuthError,
    AuthorizationError,
    DecryptionError,
    InvalidToken,
    KeyMaterialError,
    KeyRotationError,
    TokenExpired,
)
from forge_auth.keys import (
    display_prefix,
    generate_api_key,
    hash_api_key,
    parse_token,
    verify_api_key,
)
from forge_auth.ratelimit import InMemoryRateLimiter
from forge_auth.rbac import ROLE_RANK, has_at_least, max_grantable_role
from forge_auth.redaction import REDACTED, SecretRedactor
from forge_auth.tokens import (
    decode_session_jwt,
    encode_session_jwt,
    looks_like_jwt,
    make_service_token,
    verify_service_token,
)
from forge_auth.vault import EnvKeyProvider, SecretVault, StaticKeyProvider

__version__ = "0.1.0"

__all__ = [
    "REDACTED",
    "ROLE_RANK",
    "AuthError",
    "AuthenticationError",
    "AuthorizationError",
    "DecryptionError",
    "EnvKeyProvider",
    "InMemoryRateLimiter",
    "InvalidToken",
    "KeyMaterialError",
    "KeyRotationError",
    "SecretRedactor",
    "SecretVault",
    "StaticKeyProvider",
    "TokenExpired",
    "decode_session_jwt",
    "display_prefix",
    "encode_session_jwt",
    "generate_api_key",
    "has_at_least",
    "hash_api_key",
    "looks_like_jwt",
    "make_service_token",
    "max_grantable_role",
    "parse_token",
    "verify_api_key",
    "verify_service_token",
]
