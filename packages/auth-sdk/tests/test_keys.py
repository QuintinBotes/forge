"""F37 platform-key helper tests (AC6, AC7)."""

from __future__ import annotations

import pytest

from forge_auth.keys import (
    KEY_ID_LENGTH,
    display_prefix,
    generate_api_key,
    hash_api_key,
    parse_token,
    verify_api_key,
)
from forge_contracts.auth import PlatformKeyKind

EXPECTED_PREFIX = {
    PlatformKeyKind.PERSONAL: "forge_pat",
    PlatformKeyKind.SERVICE: "forge_svc",
    PlatformKeyKind.AGENT_RUNNER: "forge_agt",
}


@pytest.mark.parametrize("kind", list(PlatformKeyKind))
def test_generate_shape_per_kind(kind: PlatformKeyKind) -> None:
    token, key_id, secret, prefix = generate_api_key(kind)
    assert token == f"{EXPECTED_PREFIX[kind]}_{key_id}_{secret}"
    assert len(key_id) == KEY_ID_LENGTH
    assert "_" not in key_id
    assert len(secret) >= 40  # 32 random bytes base64url
    assert prefix == display_prefix(kind, key_id, secret)
    assert secret not in prefix  # masked display never carries the secret
    assert prefix.endswith(secret[-4:])


@pytest.mark.parametrize("kind", list(PlatformKeyKind))
def test_parse_round_trip(kind: PlatformKeyKind) -> None:
    token, key_id, secret, _ = generate_api_key(kind)
    assert parse_token(token) == (kind, key_id, secret)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "forge_svc",
        "forge_svc_short_x",  # key_id wrong length
        "forge_xxx_abcd1234_secret",  # unknown kind prefix
        "forge_svc_ABCD1234_secret",  # key_id outside alphabet
        "forge_svc_abcd1234_",  # empty secret
        "notaforgekey",
        "sk-ant-api03-whatever",
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    assert parse_token(bad) is None


def test_hash_and_verify() -> None:
    _, _, secret, _ = generate_api_key(PlatformKeyKind.SERVICE)
    digest = hash_api_key(secret, pepper="pepper-1")
    assert len(digest) == 64 and secret not in digest
    assert verify_api_key(secret, digest, pepper="pepper-1")
    assert not verify_api_key(secret + "x", digest, pepper="pepper-1")
    assert not verify_api_key(secret, digest, pepper="pepper-2")


def test_hash_requires_pepper() -> None:
    with pytest.raises(ValueError, match="pepper"):
        hash_api_key("secret", pepper="")


def test_key_ids_and_secrets_are_unique() -> None:
    seen = {generate_api_key(PlatformKeyKind.PERSONAL)[1:3] for _ in range(50)}
    assert len(seen) == 50
