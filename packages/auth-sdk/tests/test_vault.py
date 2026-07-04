"""F37 vault unit tests (AC2, AC3, AC4, AC20) — pure crypto, no DB."""

from __future__ import annotations

import base64
import uuid

import pytest

from forge_auth.errors import DecryptionError, KeyMaterialError, KeyRotationError
from forge_auth.vault import (
    FORMAT_VERSION,
    NONCE_SIZE,
    EnvKeyProvider,
    SecretVault,
    StaticKeyProvider,
    parse_vault_keys,
)

W1 = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
W2 = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

KEY1 = bytes(range(32))
KEY2 = bytes(reversed(range(32)))


@pytest.fixture
def vault() -> SecretVault:
    return SecretVault(StaticKeyProvider({1: KEY1}))


def test_round_trip(vault: SecretVault) -> None:
    """AC2: encrypt→decrypt round-trips; blob has format/version/nonce header."""
    pt = "sk-ant-api03-super-secret-value"
    blob = vault.encrypt(pt, workspace_id=W1)
    assert vault.decrypt(blob, workspace_id=W1) == pt
    assert blob[0] == FORMAT_VERSION
    assert blob[1] == 1  # embedded KEK version
    assert len(blob) >= 2 + NONCE_SIZE + 16  # header + GCM tag
    assert pt.encode() not in blob  # no plaintext substring


def test_nondeterministic_nonce(vault: SecretVault) -> None:
    assert vault.encrypt("x", workspace_id=W1) != vault.encrypt("x", workspace_id=W1)


def test_cross_workspace_aad_binding_fails(vault: SecretVault) -> None:
    """AC3: a blob for W1 is cryptographically useless under W2."""
    blob = vault.encrypt("secret", workspace_id=W1)
    with pytest.raises(DecryptionError):
        vault.decrypt(blob, workspace_id=W2)


def test_bit_flip_tamper_fails(vault: SecretVault) -> None:
    blob = bytearray(vault.encrypt("secret", workspace_id=W1))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        vault.decrypt(bytes(blob), workspace_id=W1)


@pytest.mark.parametrize(
    "bad",
    [b"", b"\x01", b"\x02" + b"\x01" + b"0" * 40, b"\x01\x01short"],
)
def test_malformed_blob_raises(vault: SecretVault, bad: bytes) -> None:
    """AC20: malformed blobs raise rather than returning garbage."""
    with pytest.raises(DecryptionError):
        vault.decrypt(bad, workspace_id=W1)


def test_unknown_key_version_raises(vault: SecretVault) -> None:
    blob = bytearray(vault.encrypt("secret", workspace_id=W1))
    blob[1] = 9  # point at a KEK version the provider does not hold
    with pytest.raises(DecryptionError):
        vault.decrypt(bytes(blob), workspace_id=W1)


def test_rotation_across_kek_versions() -> None:
    """AC4: v1 blobs rotate to the active v2 and still decrypt; retired
    versions keep decrypting."""
    v1_vault = SecretVault(StaticKeyProvider({1: KEY1}))
    blob_v1 = v1_vault.encrypt("rotate-me", workspace_id=W1)

    v2_vault = SecretVault(StaticKeyProvider({1: KEY1, 2: KEY2}, active=2))
    # Retired-version decrypt still works.
    assert v2_vault.decrypt(blob_v1, workspace_id=W1) == "rotate-me"
    blob_v2 = v2_vault.rotate(blob_v1, workspace_id=W1)
    assert blob_v2[1] == 2
    assert v2_vault.decrypt(blob_v2, workspace_id=W1) == "rotate-me"


def test_rotate_wrong_workspace_raises() -> None:
    v = SecretVault(StaticKeyProvider({1: KEY1, 2: KEY2}, active=2))
    blob = v.encrypt("x", workspace_id=W1)
    with pytest.raises(KeyRotationError):
        v.rotate(blob, workspace_id=W2)


def test_per_workspace_deks_differ(vault: SecretVault) -> None:
    """Same KEK, different workspaces ⇒ different DEKs (HKDF salt = ws id)."""
    assert vault._dek(1, W1) != vault._dek(1, W2)


@pytest.mark.parametrize(
    "plaintext",
    ["", "a", "unicode ✓ ünïcode", "x" * 5000, "line1\nline2\ttab"],
)
def test_round_trip_property(vault: SecretVault, plaintext: str) -> None:
    blob = vault.encrypt(plaintext, workspace_id=W1)
    assert vault.decrypt(blob, workspace_id=W1) == plaintext
    if plaintext:
        assert plaintext.encode() not in blob


# -- key providers (AC20 fail-closed config) -------------------------------- #


def test_static_provider_rejects_bad_material() -> None:
    with pytest.raises(KeyMaterialError):
        StaticKeyProvider({})
    with pytest.raises(KeyMaterialError):
        StaticKeyProvider({1: b"short"})
    with pytest.raises(KeyMaterialError):
        StaticKeyProvider({0: KEY1})
    with pytest.raises(KeyMaterialError):
        StaticKeyProvider({1: KEY1}, active=7)


def test_parse_vault_keys() -> None:
    raw = f"1:{base64.b64encode(KEY1).decode()},2:{base64.b64encode(KEY2).decode()}"
    keys = parse_vault_keys(raw)
    assert keys == {1: KEY1, 2: KEY2}
    with pytest.raises(KeyMaterialError):
        parse_vault_keys("nonsense")
    with pytest.raises(KeyMaterialError):
        parse_vault_keys("1:not-base64!!!")
    with pytest.raises(KeyMaterialError):
        parse_vault_keys("")


def test_env_key_provider_fails_closed() -> None:
    with pytest.raises(KeyMaterialError):
        EnvKeyProvider(environ={})
    with pytest.raises(KeyMaterialError):
        EnvKeyProvider(environ={"FORGE_VAULT_KEYS": "1:xx"})


def test_env_key_provider_reads_active_version() -> None:
    env = {
        "FORGE_VAULT_KEYS": (
            f"1:{base64.b64encode(KEY1).decode()},2:{base64.b64encode(KEY2).decode()}"
        ),
        "FORGE_VAULT_ACTIVE_KEY_VERSION": "1",
    }
    provider = EnvKeyProvider(environ=env)
    assert provider.active_version() == 1
    assert provider.get(2) == KEY2
    # Default active = highest version when unset.
    del env["FORGE_VAULT_ACTIVE_KEY_VERSION"]
    assert EnvKeyProvider(environ=env).active_version() == 2
