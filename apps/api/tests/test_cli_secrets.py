"""CLI ``check-config`` preflight tests (HARD-13 AC17).

``python -m forge_api.cli.secrets check-config`` exits non-zero on a missing key,
a deprecated alias, or envelope-off-in-prod, and zero on a valid prod config.
"""

from __future__ import annotations

import pytest

from forge_api.cli_secrets import main


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FORGE_SECRET_KEY",
        "SECRET_KEY",
        "FORGE_ENV",
        "FORGE_ENVIRONMENT",
        "FORGE_ENVELOPE_ENCRYPTION",
        "FORGE_DEV_INSECURE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_valid_production_config_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_ENVIRONMENT", "production")
    monkeypatch.setenv("FORGE_SECRET_KEY", "a-stable-production-master-key-000")
    assert main(["check-config"]) == 0


def test_missing_key_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_ENVIRONMENT", "production")
    assert main(["check-config"]) == 1


def test_deprecated_alias_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_ENVIRONMENT", "production")
    monkeypatch.setenv("FORGE_SECRET_KEY", "a-stable-production-master-key-000")
    monkeypatch.setenv("SECRET_KEY", "legacy-alias-still-set")
    assert main(["check-config"]) == 1


def test_envelope_off_in_production_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_ENVIRONMENT", "production")
    monkeypatch.setenv("FORGE_SECRET_KEY", "a-stable-production-master-key-000")
    monkeypatch.setenv("FORGE_ENVELOPE_ENCRYPTION", "false")
    assert main(["check-config"]) == 1


def test_rotate_kek_on_empty_envelope_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SECRET_KEY", "a-stable-production-master-key-000")
    monkeypatch.setenv("FORGE_ENVELOPE_ENCRYPTION", "true")
    assert main(["rotate-kek"]) == 0


def test_sweep_expired_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SECRET_KEY", "a-stable-production-master-key-000")
    assert main(["sweep-expired"]) == 0
