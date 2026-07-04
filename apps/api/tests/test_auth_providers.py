"""Secret-provider tests (HARD-13 AC10): the single, swappable secret ingress."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from forge_api.auth import providers
from forge_api.auth.providers import (
    ChainSecretProvider,
    EnvSecretProvider,
    FileSecretProvider,
    resolve_secret,
    set_default_provider,
)


def test_env_provider_reads_plain_value() -> None:
    provider = EnvSecretProvider(environ={"FOO": "bar"})
    assert provider.get("FOO") == "bar"
    assert provider.get("MISSING") is None


def test_env_provider_honours_file_indirection(tmp_path: Path) -> None:
    secret_file = tmp_path / "foo.secret"
    secret_file.write_text("  file-value\n")  # stripped
    provider = EnvSecretProvider(environ={"FOO_FILE": str(secret_file)})
    assert provider.get("FOO") == "file-value"


def test_env_provider_plain_wins_over_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "foo.secret"
    secret_file.write_text("file-value")
    provider = EnvSecretProvider(environ={"FOO": "env-value", "FOO_FILE": str(secret_file)})
    assert provider.get("FOO") == "env-value"


def test_file_provider_reads_run_secrets_convention(tmp_path: Path) -> None:
    (tmp_path / "FORGE_SECRET_KEY").write_text("mounted-key\n")
    provider = FileSecretProvider(root=tmp_path)
    assert provider.get("FORGE_SECRET_KEY") == "mounted-key"
    assert provider.get("ABSENT") is None


def test_chain_returns_first_non_none() -> None:
    first = EnvSecretProvider(environ={})
    second = EnvSecretProvider(environ={"K": "from-second"})
    chain = ChainSecretProvider([first, second])
    assert chain.get("K") == "from-second"
    assert chain.get("NOPE") is None


def test_chain_precedence_first_wins() -> None:
    chain = ChainSecretProvider(
        [EnvSecretProvider(environ={"K": "first"}), EnvSecretProvider(environ={"K": "second"})]
    )
    assert chain.get("K") == "first"


def test_resolve_secret_uses_the_default_provider() -> None:
    calls: list[str] = []

    class RecordingProvider:
        name = "recording"

        def get(self, key: str) -> str | None:
            calls.append(key)
            return "resolved" if key == "FORGE_SECRET_KEY" else None

    set_default_provider(RecordingProvider())
    try:
        assert resolve_secret("FORGE_SECRET_KEY") == "resolved"
        assert resolve_secret("OTHER") is None
        assert calls == ["FORGE_SECRET_KEY", "OTHER"]
    finally:
        set_default_provider(None)


def test_resolve_secret_is_the_ingress_for_the_auth_service() -> None:
    """AC10: the auth service resolves the master key through resolve_secret."""
    from forge_api.auth.service import _resolve_master_key

    seen: list[str] = []

    class RecordingProvider:
        name = "recording"

        def get(self, key: str) -> str | None:
            seen.append(key)
            return "service-master-key-000000" if key == "FORGE_SECRET_KEY" else None

    set_default_provider(RecordingProvider())
    try:
        assert _resolve_master_key(None) == b"service-master-key-000000"
        assert "FORGE_SECRET_KEY" in seen
    finally:
        set_default_provider(None)


@pytest.fixture(autouse=True)
def _reset_default_provider() -> Iterator[None]:
    # Guard against a leaked default provider from any test above.
    yield
    providers.set_default_provider(None)
