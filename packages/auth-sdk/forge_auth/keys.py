"""Platform API-key primitives (F37): generate / hash / verify / parse.

Inbound machine/agent auth tokens are **one-way hashed** (peppered
HMAC-SHA256), never decryptable — the opposite primitive from the BYOK vault.
The token embeds a non-secret ``key_id`` for O(1) row lookup:

    token = f"{prefix}_{key_id}_{secret}"      # prefix per kind, see _PREFIX

The high-entropy 32-byte secret makes a fast MAC sufficient (Argon2 is for
low-entropy passwords) and keeps the hot auth path O(1).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from forge_contracts.auth import PlatformKeyKind

__all__ = [
    "KEY_ID_LENGTH",
    "display_prefix",
    "generate_api_key",
    "hash_api_key",
    "parse_token",
    "verify_api_key",
]

#: Token prefix per key kind (also how ``parse_token`` recovers the kind).
_PREFIX: dict[PlatformKeyKind, str] = {
    PlatformKeyKind.PERSONAL: "forge_pat",
    PlatformKeyKind.SERVICE: "forge_svc",
    PlatformKeyKind.AGENT_RUNNER: "forge_agt",
}
_KIND_BY_PREFIX = {v: k for k, v in _PREFIX.items()}

#: Public lookup id embedded in the token (indexed, non-secret).
KEY_ID_LENGTH = 8
#: ``key_id`` alphabet deliberately excludes ``_`` so token parsing is unambiguous.
_KEY_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
#: Entropy of the verified secret portion (32 random bytes, base64url).
_SECRET_BYTES = 32


def generate_api_key(kind: PlatformKeyKind) -> tuple[str, str, str, str]:
    """Return ``(token, key_id, secret, display_prefix)`` for a fresh key."""
    key_id = "".join(secrets.choice(_KEY_ID_ALPHABET) for _ in range(KEY_ID_LENGTH))
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    token = f"{_PREFIX[kind]}_{key_id}_{secret}"
    return token, key_id, secret, display_prefix(kind, key_id, secret)


def display_prefix(kind: PlatformKeyKind, key_id: str, secret: str) -> str:
    """Masked display form, e.g. ``forge_svc_a1b2c3d4…wxyz`` (never the secret)."""
    return f"{_PREFIX[kind]}_{key_id}…{secret[-4:]}"


def hash_api_key(secret: str, *, pepper: str) -> str:
    """Hex HMAC-SHA256(pepper, secret) — one-way, never reversible."""
    if not pepper:
        raise ValueError("pepper must be non-empty")
    return hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_api_key(secret: str, key_hash: str, *, pepper: str) -> bool:
    """Constant-time comparison of the presented secret against the stored hash."""
    return hmac.compare_digest(hash_api_key(secret, pepper=pepper), key_hash)


def parse_token(token: str) -> tuple[PlatformKeyKind, str, str] | None:
    """``(kind, key_id, secret)`` or ``None`` if not a well-formed forge key.

    The secret segment may itself contain ``_``/``-`` (base64url); only the
    ``key_id`` segment is restricted to the underscore-free alphabet.
    """
    parts = token.split("_", 3)
    if len(parts) != 4:
        return None
    prefix = f"{parts[0]}_{parts[1]}"
    kind = _KIND_BY_PREFIX.get(prefix)
    key_id, secret = parts[2], parts[3]
    if kind is None or not secret:
        return None
    if len(key_id) != KEY_ID_LENGTH or any(c not in _KEY_ID_ALPHABET for c in key_id):
        return None
    return kind, key_id, secret
