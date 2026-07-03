"""F37 session-JWT + service-token tests (AC11)."""

from __future__ import annotations

import time
import uuid

import pytest

from forge_auth.errors import InvalidToken, KeyMaterialError, TokenExpired
from forge_auth.tokens import (
    decode_session_jwt,
    encode_session_jwt,
    looks_like_jwt,
    make_service_token,
    verify_service_token,
)
from forge_contracts.auth import SessionClaims, UserRole

SECRET = "test-auth-secret"


def _claims(**overrides) -> SessionClaims:
    now = int(time.time())
    payload = {
        "sub": uuid.uuid4(),
        "wsid": uuid.uuid4(),
        "role": UserRole.MEMBER,
        "email": "alice@example.com",
        "exp": now + 3600,
        "iat": now,
    }
    payload.update(overrides)
    return SessionClaims(**payload)


def test_encode_decode_round_trip() -> None:
    claims = _claims()
    token = encode_session_jwt(claims, secret=SECRET)
    assert looks_like_jwt(token)
    decoded = decode_session_jwt(token, secret=SECRET)
    assert decoded == claims


def test_expired_raises_token_expired() -> None:
    token = encode_session_jwt(_claims(exp=int(time.time()) - 10), secret=SECRET)
    with pytest.raises(TokenExpired):
        decode_session_jwt(token, secret=SECRET)


def test_wrong_secret_raises_invalid() -> None:
    token = encode_session_jwt(_claims(), secret=SECRET)
    with pytest.raises(InvalidToken):
        decode_session_jwt(token, secret="other-secret")


def test_wrong_audience_raises_invalid() -> None:
    token = encode_session_jwt(_claims(aud="not-forge"), secret=SECRET)
    with pytest.raises(InvalidToken):
        decode_session_jwt(token, secret=SECRET)


def test_tampered_payload_raises_invalid() -> None:
    header, payload, sig = encode_session_jwt(_claims(), secret=SECRET).split(".")
    tampered = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB")
    with pytest.raises(InvalidToken):
        decode_session_jwt(f"{header}.{tampered}.{sig}", secret=SECRET)


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c.d", "forge_svc_x_y"])
def test_structurally_invalid_tokens(bad: str) -> None:
    assert not looks_like_jwt(bad)
    with pytest.raises(InvalidToken):
        decode_session_jwt(bad, secret=SECRET)


def test_empty_secret_fails_closed() -> None:
    with pytest.raises(KeyMaterialError):
        encode_session_jwt(_claims(), secret="")


def test_service_token_round_trip() -> None:
    token = make_service_token(secret=SECRET)
    assert token.startswith("forge_int_")
    assert verify_service_token(token, secret=SECRET)
    assert not verify_service_token(token, secret="other")
    assert not verify_service_token("forge_int_bogus", secret=SECRET)
