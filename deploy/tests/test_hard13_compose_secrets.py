"""HARD-13 compose config-drift fix (AC6): canonical names + fail-closed key.

Asserts the post-fix ``deploy/docker-compose.yml`` uses the canonical
``FORGE_ENVIRONMENT`` / ``FORGE_SECRET_KEY`` names (not the deprecated
``FORGE_ENV`` / ``SECRET_KEY`` that mis-classified production as development) and
that ``FORGE_SECRET_KEY`` is fail-closed (``${...:?...}``) on the api service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
_APP_SERVICES = ("api", "worker", "mcp-gateway")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _env(service: str) -> dict:
    return _compose()["services"][service].get("environment", {}) or {}


def test_no_deprecated_env_aliases_on_app_services() -> None:
    for name in _APP_SERVICES:
        env = _env(name)
        assert "FORGE_ENV" not in env, f"{name} still sets deprecated FORGE_ENV"
        assert "SECRET_KEY" not in env, f"{name} still sets deprecated SECRET_KEY"


def test_app_services_use_canonical_environment_name() -> None:
    for name in _APP_SERVICES:
        assert _env(name).get("FORGE_ENVIRONMENT") == "production"


def test_api_secret_key_is_fail_closed() -> None:
    raw = _env("api").get("FORGE_SECRET_KEY", "")
    # Compose ``:?`` fails the `up` when the variable is unset (fail-closed).
    assert raw.startswith("${FORGE_SECRET_KEY:?"), raw


def test_api_enables_envelope_encryption() -> None:
    assert "FORGE_ENVELOPE_ENCRYPTION" in _env("api")
