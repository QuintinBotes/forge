"""HARD-02: ``AuthService.resolve_model_client`` resolves a BYOK key -> client.

Offline: ``build_model_client`` is monkeypatched to capture the config, so the
vault -> config -> factory wiring (and the key not leaking into ``repr``) is
asserted without the provider SDK or a network call.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import forge_agent.providers as providers
from forge_agent.providers import ModelClientError
from forge_api.auth.service import AuthService
from forge_contracts.enums import APIKeyKind

_VAULT_KEY = "vault-model-key-0001"


def test_resolve_model_client_reads_key_from_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FORGE_MODEL_API_KEY", raising=False)

    captured: dict[str, Any] = {}
    sentinel = object()

    def _fake_build(config: Any, **_kwargs: Any) -> Any:
        captured["config"] = config
        return sentinel

    monkeypatch.setattr(providers, "build_model_client", _fake_build)

    service = AuthService(secret_key=b"k" * 32)
    ws = uuid.uuid4()
    info = service.vault.put_secret(
        workspace_id=ws,
        name="anthropic",
        secret=_VAULT_KEY,
        kind=APIKeyKind.MODEL_PROVIDER,
    )

    client = service.resolve_model_client(ws, secret_id=info.id)

    assert client is sentinel
    config = captured["config"]
    assert config.api_key == _VAULT_KEY
    assert config.provider.value == "anthropic"
    # The resolved key never appears in the config repr.
    assert _VAULT_KEY not in repr(config)


def test_resolve_model_client_without_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    service = AuthService(secret_key=b"k" * 32)
    with pytest.raises(ModelClientError):
        service.resolve_model_client(uuid.uuid4())
