"""Tests for the encrypted BYOK secret vault (Task 1.15 — auth & secrets).

Spec Security: secrets encrypted at rest, per-workspace isolation, never present
in serialized output.
"""

from __future__ import annotations

import json
import uuid

import pytest

from forge_api.auth.crypto import HmacAeadCipher, generate_key
from forge_api.auth.vault import SecretInfo, SecretNotFoundError, SecretVault
from forge_contracts.enums import APIKeyKind


def _vault() -> SecretVault:
    return SecretVault(cipher=HmacAeadCipher(generate_key()))


def test_put_returns_redacted_info_without_secret() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws,
        name="anthropic",
        secret="sk-ant-SECRETVALUE0001",
        kind=APIKeyKind.MODEL_PROVIDER,
        provider="anthropic",
    )
    assert isinstance(info, SecretInfo)
    dumped = json.dumps(info.model_dump(mode="json"))
    assert "sk-ant-SECRETVALUE0001" not in dumped
    assert "SECRETVALUE" not in dumped


def test_secret_is_encrypted_at_rest() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws,
        name="anthropic",
        secret="sk-ant-SECRETVALUE0001",
        kind=APIKeyKind.MODEL_PROVIDER,
    )
    stored = vault.raw_record(ws, info.id)
    assert isinstance(stored.ciphertext, bytes)
    assert b"sk-ant-SECRETVALUE0001" not in stored.ciphertext


def test_get_secret_decrypts() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws,
        name="gh",
        secret="ghp_TOKEN_VALUE_123456",
        kind=APIKeyKind.INTEGRATION_TOKEN,
    )
    assert vault.get_secret(ws, info.id) == "ghp_TOKEN_VALUE_123456"


def test_workspace_isolation_blocks_cross_tenant_read() -> None:
    vault = _vault()
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws_a, name="k", secret="topsecret", kind=APIKeyKind.SYSTEM
    )
    with pytest.raises(SecretNotFoundError):
        vault.get_secret(ws_b, info.id)


def test_list_only_returns_own_workspace_secrets() -> None:
    vault = _vault()
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    vault.put_secret(workspace_id=ws_a, name="a1", secret="x", kind=APIKeyKind.SYSTEM)
    vault.put_secret(workspace_id=ws_a, name="a2", secret="y", kind=APIKeyKind.SYSTEM)
    vault.put_secret(workspace_id=ws_b, name="b1", secret="z", kind=APIKeyKind.SYSTEM)
    assert {i.name for i in vault.list_secrets(ws_a)} == {"a1", "a2"}
    assert {i.name for i in vault.list_secrets(ws_b)} == {"b1"}


def test_key_prefix_is_stored_for_display() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws, name="k", secret="sk-ant-abcdef", kind=APIKeyKind.MODEL_PROVIDER
    )
    assert info.key_prefix is not None
    assert info.key_prefix.startswith("sk-")
    # Prefix must not reveal the whole secret.
    assert "abcdef" not in info.key_prefix


def test_delete_removes_secret() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(workspace_id=ws, name="k", secret="v", kind=APIKeyKind.SYSTEM)
    vault.delete_secret(ws, info.id)
    with pytest.raises(SecretNotFoundError):
        vault.get_secret(ws, info.id)


def test_record_repr_does_not_leak_secret() -> None:
    vault = _vault()
    ws = uuid.uuid4()
    info = vault.put_secret(
        workspace_id=ws, name="k", secret="VERYSECRET", kind=APIKeyKind.SYSTEM
    )
    record = vault.raw_record(ws, info.id)
    assert "VERYSECRET" not in repr(record)
