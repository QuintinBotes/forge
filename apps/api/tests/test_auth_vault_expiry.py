"""Vault expiry, value-rotation, KEK re-wrap, and sweep tests (HARD-13 AC7/8/14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from forge_api.auth.crypto import EnvelopeCipher, HmacAeadCipher, generate_key
from forge_api.auth.keyring import KeyRing
from forge_api.auth.vault import (
    InMemorySecretStore,
    SecretExpiredError,
    SecretNotFoundError,
    SecretVault,
)
from forge_contracts.enums import APIKeyKind

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _vault() -> SecretVault:
    return SecretVault(cipher=HmacAeadCipher(generate_key()))


def test_get_secret_raises_when_expired() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws,
        name="anthropic",
        secret="sk-ant-EXPIRED0001",
        kind=APIKeyKind.MODEL_PROVIDER,
        expires_at=_NOW - timedelta(seconds=1),
    )
    with pytest.raises(SecretExpiredError):
        vault.get_secret(ws, info.id, now=_NOW)


def test_get_secret_returns_plaintext_before_expiry() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws,
        name="anthropic",
        secret="sk-ant-VALID0001",
        kind=APIKeyKind.MODEL_PROVIDER,
        expires_at=_NOW + timedelta(days=1),
    )
    assert vault.get_secret(ws, info.id, now=_NOW) == "sk-ant-VALID0001"


def test_expired_error_is_a_not_found_subclass() -> None:
    # Existing catchers of SecretNotFoundError keep failing closed.
    assert issubclass(SecretExpiredError, SecretNotFoundError)


def test_raw_record_returns_expired_ciphertext_for_rotation() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws,
        name="k",
        secret="still-here",
        kind=APIKeyKind.SYSTEM,
        expires_at=_NOW - timedelta(days=1),
    )
    record = vault.raw_record(ws, info.id)
    assert isinstance(record.ciphertext, bytes)


def test_secret_info_is_expired_reflects_expires_at() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    vault.put_secret(
        workspace_id=ws,
        name="expired",
        secret="x",
        kind=APIKeyKind.SYSTEM,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    vault.put_secret(
        workspace_id=ws,
        name="valid",
        secret="y",
        kind=APIKeyKind.SYSTEM,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    by_name = {i.name: i for i in vault.list_secrets(ws)}
    assert by_name["expired"].is_expired is True
    assert by_name["valid"].is_expired is False


def test_rotate_secret_reencrypts_and_preserves_identity() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws,
        name="gh",
        secret="ghp_OLD_TOKEN_00000000",
        kind=APIKeyKind.INTEGRATION_TOKEN,
    )
    rotated = vault.rotate_secret(
        workspace_id=ws, secret_id=info.id, new_secret="ghp_NEW_TOKEN_11111111"
    )
    assert rotated.id == info.id
    assert rotated.name == "gh"
    assert vault.get_secret(ws, info.id) == "ghp_NEW_TOKEN_11111111"
    # The stored ciphertext must not contain either plaintext.
    record = vault.raw_record(ws, info.id)
    assert b"ghp_NEW_TOKEN_11111111" not in record.ciphertext


def test_rotate_missing_secret_raises() -> None:
    vault = _vault()
    with pytest.raises(SecretNotFoundError):
        vault.rotate_secret(
            workspace_id=uuid.uuid4(), secret_id=uuid.uuid4(), new_secret="x"
        )


def test_sweep_expired_counts_and_purges() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    vault.put_secret(
        workspace_id=ws, name="e1", secret="a", kind=APIKeyKind.SYSTEM,
        expires_at=_NOW - timedelta(days=1),
    )
    vault.put_secret(
        workspace_id=ws, name="e2", secret="b", kind=APIKeyKind.SYSTEM,
        expires_at=_NOW - timedelta(hours=1),
    )
    vault.put_secret(
        workspace_id=ws, name="ok", secret="c", kind=APIKeyKind.SYSTEM,
        expires_at=_NOW + timedelta(days=1),
    )
    assert vault.sweep_expired(now=_NOW, purge=False) == 2
    # Still present (flag-only).
    assert len(vault.list_secrets(ws)) == 3
    assert vault.sweep_expired(now=_NOW, purge=True) == 2
    assert {i.name for i in vault.list_secrets(ws)} == {"ok"}


def test_rewrap_all_rotates_kek_without_touching_data() -> None:
    """AC14 shape (offline): seed v1 rows, rewrap to v2, all still decrypt."""
    kek1, kek2 = b"k" * 32, b"z" * 32
    store = InMemorySecretStore()
    ws = uuid.uuid4()

    v1_vault = SecretVault(cipher=EnvelopeCipher(KeyRing({1: kek1}, 1)), store=store)
    infos = [
        v1_vault.put_secret(
            workspace_id=ws, name=f"s{i}", secret=f"secret-value-{i}", kind=APIKeyKind.SYSTEM
        )
        for i in range(3)
    ]

    ring2 = KeyRing({1: kek1, 2: kek2}, 2)
    v2_vault = SecretVault(cipher=EnvelopeCipher(ring2), store=store)
    result = v2_vault.rewrap_all(keyring=ring2, to_version=2)
    assert result == {"rewrapped": 3, "skipped": 0}

    for i, info in enumerate(infos):
        assert v2_vault.get_secret(ws, info.id) == f"secret-value-{i}"
        record = v2_vault.raw_record(ws, info.id)
        assert record.key_version == 2
        assert EnvelopeCipher.kek_version(record.ciphertext) == 2
        assert record.rotated_at is not None

    # A second rewrap to the same version is a no-op (idempotent).
    assert v2_vault.rewrap_all(keyring=ring2, to_version=2) == {"rewrapped": 0, "skipped": 3}


def test_rewrap_all_requires_envelope_cipher() -> None:
    vault = _vault()  # single-tier HmacAead
    with pytest.raises(TypeError, match="EnvelopeCipher"):
        vault.rewrap_all(keyring=KeyRing({1: b"k" * 32}, 1))
