"""Tests for instance master-key resolution (HARD-13 — fail-closed secrets).

The auth layer requires a stable ``FORGE_SECRET_KEY`` resolved through the secret
provider. HARD-13 hardens the ephemeral fallback: a missing key fails closed
*regardless of environment*, and the dev-only ephemeral path is reachable ONLY via
an explicit ``FORGE_DEV_INSECURE`` opt-in (no environment string can accidentally
land on it). ``SECRET_KEY``/``FORGE_ENV`` remain deprecated aliases for one
release with a loud warning.
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
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FORGE_DEV_INSECURE", raising=False)
    monkeypatch.setenv("FORGE_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="FORGE_SECRET_KEY"):
        _resolve_master_key(None)


def test_missing_key_without_dev_insecure_raises_even_in_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HARD-13: no environment string reaches the ephemeral path on its own."""
    monkeypatch.delenv("FORGE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FORGE_DEV_INSECURE", raising=False)
    monkeypatch.setenv("FORGE_ENVIRONMENT", "development")
    with pytest.raises(RuntimeError, match="FORGE_SECRET_KEY"):
        _resolve_master_key(None)


def test_dev_insecure_opt_in_warns_and_returns_ephemeral_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORGE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("FORGE_DEV_INSECURE", "1")
    with pytest.warns(UserWarning, match="FORGE_DEV_INSECURE"):
        key = _resolve_master_key(None)
    assert isinstance(key, bytes)
    assert len(key) >= 16


def test_legacy_secret_key_alias_is_honoured_with_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORGE_SECRET_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "legacy-compose-secret-value-000000")
    with pytest.warns(DeprecationWarning, match="SECRET_KEY"):
        assert _resolve_master_key(None) == b"legacy-compose-secret-value-000000"


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
