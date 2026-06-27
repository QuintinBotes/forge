"""Tests for instance master-key resolution (Task H3 — Fernet crypto backend).

The auth layer requires a stable ``FORGE_SECRET_KEY`` and uses a real
authenticated cipher (Fernet) for the vault. The ephemeral-key prod fallback is
gone: production refuses to start without a key, while development keeps a
clearly opt-in, loudly-warned dev-only path so the dev server still runs.
"""

from __future__ import annotations

import uuid

import pytest

from forge_api.auth.crypto import FernetCipher
from forge_api.auth.service import AuthService, _resolve_master_key
from forge_contracts.enums import APIKeyKind


def test_explicit_secret_key_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_SECRET_KEY", raising=False)
    assert _resolve_master_key(b"x" * 32) == b"x" * 32


def test_env_secret_key_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SECRET_KEY", "a-very-stable-instance-secret")
    assert _resolve_master_key(None) == b"a-very-stable-instance-secret"


def test_missing_key_in_production_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_SECRET_KEY", raising=False)
    monkeypatch.setenv("FORGE_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="FORGE_SECRET_KEY"):
        _resolve_master_key(None)


def test_missing_key_in_development_warns_and_returns_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORGE_SECRET_KEY", raising=False)
    monkeypatch.setenv("FORGE_ENVIRONMENT", "development")
    with pytest.warns(UserWarning, match="FORGE_SECRET_KEY"):
        key = _resolve_master_key(None)
    assert isinstance(key, bytes)
    assert len(key) >= 16


def test_default_environment_is_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_SECRET_KEY", raising=False)
    monkeypatch.delenv("FORGE_ENVIRONMENT", raising=False)
    with pytest.warns(UserWarning):
        assert isinstance(_resolve_master_key(None), bytes)


def test_service_vault_uses_fernet_cipher_by_default() -> None:
    service = AuthService(secret_key=b"k" * 32)
    assert isinstance(service.vault._cipher, FernetCipher)


def test_service_vault_roundtrip_with_fernet() -> None:
    service = AuthService(secret_key=b"k" * 32)
    ws = uuid.uuid4()
    info = service.vault.put_secret(
        workspace_id=ws,
        name="anthropic",
        secret="sk-ant-SECRETVALUE0001",
        kind=APIKeyKind.MODEL_PROVIDER,
    )
    assert service.vault.get_secret(ws, info.id) == "sk-ant-SECRETVALUE0001"
