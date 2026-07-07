"""Session JWT (HS256) + internal service token (F37 web↔API auth seam).

The web auth layer mints a compact HS256 JWS over the shared ``AUTH_SECRET``
whose claims are the typed :class:`~forge_contracts.auth.SessionClaims`; the
API verifies signature, ``exp``, and ``aud`` before trusting it. HS256 is a
single HMAC-SHA256 over base64url segments — implemented directly on the
stdlib so the SDK stays dependency-light; an asymmetric JWKS scheme is a
documented, compatible upgrade behind :func:`decode_session_jwt`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time

from pydantic import ValidationError

from forge_auth.errors import InvalidToken, KeyMaterialError, TokenExpired
from forge_contracts.auth import SessionClaims

__all__ = [
    "decode_session_jwt",
    "encode_session_jwt",
    "looks_like_jwt",
    "make_service_token",
    "verify_service_token",
]

_HEADER = {"alg": "HS256", "typ": "JWT"}
_SERVICE_CONTEXT = b"forge-internal-service-token-v1"
_SERVICE_PREFIX = "forge_int_"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise InvalidToken("malformed base64url segment") from exc


def _require_secret(secret: str) -> bytes:
    if not secret:
        raise KeyMaterialError("AUTH_SECRET must be non-empty")
    return secret.encode("utf-8")


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(_require_secret(secret), signing_input, hashlib.sha256).digest()


def looks_like_jwt(token: str) -> bool:
    """Cheap structural check: three dot-separated non-empty segments."""
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def encode_session_jwt(claims: SessionClaims, *, secret: str) -> str:
    """Serialize + sign ``claims`` as a compact HS256 JWS."""
    payload = json.loads(claims.model_dump_json())  # UUIDs → strings
    signing_input = (
        f"{_b64url_encode(json.dumps(_HEADER, separators=(',', ':')).encode())}"
        f".{_b64url_encode(json.dumps(payload, separators=(',', ':')).encode())}"
    ).encode("ascii")
    return f"{signing_input.decode('ascii')}.{_b64url_encode(_sign(signing_input, secret))}"


def decode_session_jwt(token: str, *, secret: str, audience: str = "forge-api") -> SessionClaims:
    """Verify signature, ``exp``, and ``aud``; return the typed claims.

    Raises :class:`TokenExpired` for a stale token and :class:`InvalidToken`
    for anything else (bad signature, wrong audience, malformed structure,
    non-HS256 header — the ``none`` algorithm is rejected).
    """
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise InvalidToken("token is not a compact JWS")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    if not hmac.compare_digest(_sign(signing_input, secret), _b64url_decode(parts[2])):
        raise InvalidToken("signature verification failed")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidToken("malformed JWT segments") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidToken("unsupported JWS algorithm")
    try:
        claims = SessionClaims.model_validate(payload)
    except ValidationError as exc:
        raise InvalidToken("JWT claims do not match SessionClaims") from exc
    if claims.aud != audience:
        raise InvalidToken(f"audience mismatch (expected {audience!r})")
    if claims.exp <= int(time.time()):
        raise TokenExpired("session JWT has expired")
    return claims


def make_service_token(*, secret: str) -> str:
    """Deterministic internal service-principal token (worker→api, web→sync)."""
    mac = hmac.new(_require_secret(secret), _SERVICE_CONTEXT, hashlib.sha256).hexdigest()
    return f"{_SERVICE_PREFIX}{mac}"


def verify_service_token(token: str, *, secret: str) -> bool:
    """Constant-time verification of an internal service token."""
    return hmac.compare_digest(token, make_service_token(secret=secret))
