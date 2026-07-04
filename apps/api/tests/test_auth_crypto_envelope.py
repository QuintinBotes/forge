"""Envelope-cipher tests (HARD-13 AC1-4).

Two-tier envelope encryption: a per-secret DEK encrypts the plaintext; the DEK is
wrapped under a versioned KEK. Rotation re-wraps only the DEK. These are fully
hermetic (in-memory keyrings, no network, no creds).
"""

from __future__ import annotations

import pytest

from forge_api.auth.crypto import (
    ENVELOPE_VERSION,
    EnvelopeCipher,
    HmacAeadCipher,
    InvalidTokenError,
    default_cipher,
    envelope_cipher,
)
from forge_api.auth.keyring import KeyRing

_KEK_V1 = b"k" * 32
_KEK_V2 = b"z" * 32


def _ring_v1() -> KeyRing:
    return KeyRing({1: _KEK_V1}, 1)


def _ring_v1_v2() -> KeyRing:
    return KeyRing({1: _KEK_V1, 2: _KEK_V2}, 2)


def test_encrypt_writes_v2_blob_and_roundtrips() -> None:
    cipher = envelope_cipher(_ring_v1())
    blob = cipher.encrypt("sk-ant-SECRETVALUE0001")
    assert blob[0] == ENVELOPE_VERSION  # \x02
    assert blob[1] == 1  # KEK version
    assert cipher.decrypt(blob) == "sk-ant-SECRETVALUE0001"


def test_fresh_dek_makes_two_encryptions_differ() -> None:
    cipher = envelope_cipher(_ring_v1())
    a = cipher.encrypt("same-plaintext")
    b = cipher.encrypt("same-plaintext")
    assert a != b  # fresh DEK + IV each time
    assert cipher.decrypt(a) == cipher.decrypt(b) == "same-plaintext"


def test_plaintext_never_appears_in_blob() -> None:
    cipher = envelope_cipher(_ring_v1())
    blob = cipher.encrypt("sk-ant-SECRETVALUE0001")
    assert b"sk-ant-SECRETVALUE0001" not in blob


def test_decrypt_legacy_v1_single_tier_blob() -> None:
    """AC2: a pre-envelope single-tier row still decrypts (zero-downtime upgrade)."""
    legacy = HmacAeadCipher(_KEK_V1)
    legacy_blob = legacy.encrypt("legacy-value")
    assert legacy_blob[0] == 0x01  # HmacAead single-tier version byte
    cipher = EnvelopeCipher(_ring_v1(), legacy=legacy)
    assert cipher.decrypt(legacy_blob) == "legacy-value"


def test_decrypt_legacy_fernet_blob_via_default_legacy() -> None:
    ring = _ring_v1()
    legacy_blob = default_cipher(ring.current_kek()).encrypt("legacy-fernet")
    # Default legacy cipher wraps the current KEK, so a Fernet row decrypts.
    cipher = EnvelopeCipher(ring)
    assert cipher.decrypt(legacy_blob) == "legacy-fernet"


def test_rewrap_preserves_inner_ciphertext_and_bumps_version() -> None:
    """AC3: rewrap re-wraps the DEK only — inner data ciphertext is byte-identical."""
    cipher = EnvelopeCipher(_ring_v1_v2())
    # Encrypt under v1 by using a v1-only keyring, then rewrap to v2.
    v1_cipher = EnvelopeCipher(_ring_v1())
    blob_v1 = v1_cipher.encrypt("rotate-me")
    _, _, inner_v1 = EnvelopeCipher._parse(blob_v1)

    new_blob, to_version = cipher.rewrap(blob_v1, to_version=2)
    assert to_version == 2
    assert EnvelopeCipher.kek_version(new_blob) == 2
    _, _, inner_v2 = EnvelopeCipher._parse(new_blob)
    assert inner_v2 == inner_v1  # data ciphertext untouched
    assert cipher.decrypt(new_blob) == "rotate-me"


def test_rewrapped_blob_reads_under_new_kek_only() -> None:
    """AC14 shape: after rewrap to v2, a v2-only keyring still decrypts it."""
    cipher = EnvelopeCipher(_ring_v1_v2())
    blob_v1 = EnvelopeCipher(_ring_v1()).encrypt("survives-rotation")
    new_blob, _ = cipher.rewrap(blob_v1, to_version=2)
    v2_only = EnvelopeCipher(KeyRing({2: _KEK_V2}, 2))
    assert v2_only.decrypt(new_blob) == "survives-rotation"


def test_rewrap_upgrades_legacy_blob_to_envelope() -> None:
    ring = _ring_v1()
    legacy_blob = default_cipher(ring.current_kek()).encrypt("legacy-upgrade")
    cipher = EnvelopeCipher(ring)
    new_blob, version = cipher.rewrap(legacy_blob)
    assert new_blob[0] == ENVELOPE_VERSION
    assert version == 1
    assert cipher.decrypt(new_blob) == "legacy-upgrade"


def test_tampered_wrapped_dek_raises_invalid_token() -> None:
    cipher = EnvelopeCipher(_ring_v1())
    blob = bytearray(cipher.encrypt("secret"))
    blob[5] ^= 0x01  # flip a byte inside the wrapped DEK region
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(bytes(blob))


def test_tampered_inner_ciphertext_raises_invalid_token() -> None:
    cipher = EnvelopeCipher(_ring_v1())
    blob = bytearray(cipher.encrypt("secret"))
    blob[-1] ^= 0x01  # flip the last byte of the inner ciphertext
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(bytes(blob))


def test_truncated_envelope_raises_invalid_token() -> None:
    cipher = EnvelopeCipher(_ring_v1())
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(bytes([ENVELOPE_VERSION, 1, 0xFF]))


def test_empty_blob_raises_invalid_token() -> None:
    cipher = EnvelopeCipher(_ring_v1())
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(b"")


def test_missing_kek_version_is_uniform_error() -> None:
    """Decrypting a blob whose KEK version is not configured is a uniform error."""
    blob = EnvelopeCipher(_ring_v1_v2()).encrypt("x")  # wrapped under v2
    v1_only = EnvelopeCipher(KeyRing({1: _KEK_V1}, 1))
    with pytest.raises(InvalidTokenError):
        v1_only.decrypt(blob)
