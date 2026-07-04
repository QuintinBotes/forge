"""Typed errors for the auth & secrets core (F37)."""

from __future__ import annotations

__all__ = [
    "AuthError",
    "AuthenticationError",
    "AuthorizationError",
    "DecryptionError",
    "InvalidToken",
    "KeyMaterialError",
    "KeyRotationError",
    "TokenExpired",
]


class AuthError(Exception):
    """Base class for every forge_auth error."""


class AuthenticationError(AuthError):
    """A presented credential is missing, malformed, unknown, or expired."""


class AuthorizationError(AuthError):
    """An authenticated principal lacks the required role/permission."""


class DecryptionError(AuthError):
    """A vault blob failed authentication or could not be parsed.

    Deliberately uniform: callers cannot distinguish a wrong key, a tampered
    ciphertext, a cross-workspace blob, or a malformed blob (avoids an oracle).
    """


class KeyRotationError(AuthError):
    """A blob could not be re-encrypted under the active KEK version."""


class TokenExpired(AuthenticationError):
    """A session JWT's ``exp`` is in the past."""


class InvalidToken(AuthenticationError):
    """A session JWT failed signature, audience, or structural validation."""


class KeyMaterialError(AuthError):
    """Required key material (KEK map, pepper, auth secret) is absent/invalid."""
