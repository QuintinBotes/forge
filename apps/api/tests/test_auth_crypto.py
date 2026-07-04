"""Tests for the authenticated-encryption primitive (Task 1.15 — auth & secrets).

The cipher provides confidentiality + integrity for BYOK secrets at rest. It is
built on the Python standard library (HMAC-SHA256 keystream + encrypt-then-MAC)
so the vault is fully testable without a third-party crypto dependency.
"""

from __future__ import annotations

import pytest

from forge_api.auth.crypto import (
    FernetCipher,
    HmacAeadCipher,
    InvalidTokenError,
    SecretCipher,
    default_cipher,
    generate_key,
)


def test_roundtrip_recovers_plaintext() -> None:
    cipher = HmacAeadCipher(generate_key())
    blob = cipher.encrypt("sk-super-secret-value")
    assert cipher.decrypt(blob) == "sk-super-secret-value"


def test_ciphertext_is_not_plaintext() -> None:
    cipher = HmacAeadCipher(generate_key())
    secret = "sk-super-secret-value"
    blob = cipher.encrypt(secret)
    assert isinstance(blob, bytes)
    assert secret.encode() not in blob


def test_encrypting_same_value_twice_differs() -> None:
    """A fresh random nonce per call means ciphertexts must not be identical."""
    cipher = HmacAeadCipher(generate_key())
    a = cipher.encrypt("same")
    b = cipher.encrypt("same")
    assert a != b
    assert cipher.decrypt(a) == cipher.decrypt(b) == "same"


def test_unicode_roundtrip() -> None:
    cipher = HmacAeadCipher(generate_key())
    value = "clé-secrète-✨-密钥"
    assert cipher.decrypt(cipher.encrypt(value)) == value


def test_empty_string_roundtrip() -> None:
    cipher = HmacAeadCipher(generate_key())
    assert cipher.decrypt(cipher.encrypt("")) == ""


def test_wrong_key_cannot_decrypt() -> None:
    blob = HmacAeadCipher(generate_key()).encrypt("secret")
    with pytest.raises(InvalidTokenError):
        HmacAeadCipher(generate_key()).decrypt(blob)


def test_tampered_ciphertext_is_rejected() -> None:
    cipher = HmacAeadCipher(generate_key())
    blob = bytearray(cipher.encrypt("secret"))
    blob[-1] ^= 0x01  # flip a ciphertext bit
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(bytes(blob))


def test_truncated_blob_is_rejected() -> None:
    cipher = HmacAeadCipher(generate_key())
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(b"\x01tooshort")


def test_short_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        HmacAeadCipher(b"too-short")


def test_generate_key_is_random_and_sized() -> None:
    a = generate_key()
    b = generate_key()
    assert len(a) == 32
    assert a != b


# --- Fernet backend (cryptography) ------------------------------------------ #


def test_fernet_roundtrip_recovers_plaintext() -> None:
    cipher = FernetCipher(generate_key())
    blob = cipher.encrypt("sk-super-secret-value")
    assert isinstance(blob, bytes)
    assert cipher.decrypt(blob) == "sk-super-secret-value"


def test_fernet_ciphertext_is_not_plaintext() -> None:
    cipher = FernetCipher(generate_key())
    secret = "sk-super-secret-value"
    blob = cipher.encrypt(secret)
    assert secret.encode() not in blob


def test_fernet_unicode_and_empty_roundtrip() -> None:
    cipher = FernetCipher(generate_key())
    assert cipher.decrypt(cipher.encrypt("clé-secrète-✨-密钥")) == "clé-secrète-✨-密钥"
    assert cipher.decrypt(cipher.encrypt("")) == ""


def test_fernet_encrypting_same_value_twice_differs() -> None:
    cipher = FernetCipher(generate_key())
    a = cipher.encrypt("same")
    b = cipher.encrypt("same")
    assert a != b
    assert cipher.decrypt(a) == cipher.decrypt(b) == "same"


def test_fernet_wrong_key_cannot_decrypt() -> None:
    blob = FernetCipher(generate_key()).encrypt("secret")
    with pytest.raises(InvalidTokenError):
        FernetCipher(generate_key()).decrypt(blob)


def test_fernet_tampered_ciphertext_is_rejected() -> None:
    cipher = FernetCipher(generate_key())
    blob = bytearray(cipher.encrypt("secret"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(bytes(blob))


def test_fernet_garbage_blob_is_rejected() -> None:
    cipher = FernetCipher(generate_key())
    with pytest.raises(InvalidTokenError):
        cipher.decrypt(b"not-a-valid-fernet-token")


def test_fernet_short_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        FernetCipher(b"too-short")


def test_fernet_key_derivation_is_stable() -> None:
    """Same raw key material yields an interoperable cipher (stable derivation)."""
    key = generate_key()
    blob = FernetCipher(key).encrypt("persisted-across-restart")
    assert FernetCipher(key).decrypt(blob) == "persisted-across-restart"


def test_default_cipher_is_fernet_backed() -> None:
    cipher = default_cipher(generate_key())
    assert isinstance(cipher, FernetCipher)
    assert isinstance(cipher, SecretCipher)
    assert cipher.decrypt(cipher.encrypt("v")) == "v"
