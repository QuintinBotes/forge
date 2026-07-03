"""DTO surface of the auth SDK — re-exported from the frozen contract layer.

The canonical definitions live in :mod:`forge_contracts.auth` (spec §4); this
module keeps the slice-documented ``forge_auth.models`` import path working.
"""

from __future__ import annotations

from forge_contracts.auth import (
    CreatedKey,
    OAuthProvider,
    PlatformKeyKind,
    PlatformKeyMeta,
    Principal,
    PrincipalType,
    RateDecision,
    SecretMeta,
    SessionClaims,
    UserRole,
)

__all__ = [
    "CreatedKey",
    "OAuthProvider",
    "PlatformKeyKind",
    "PlatformKeyMeta",
    "Principal",
    "PrincipalType",
    "RateDecision",
    "SecretMeta",
    "SessionClaims",
    "UserRole",
]
