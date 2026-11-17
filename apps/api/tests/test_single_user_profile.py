"""The API boots on the single-user compose profile's environment (issue #205).

The profile (``deploy/docker-compose.single-user.yml``) has no object store, so
these tests hand the API exactly the environment that file gives it and check
that it starts, reports healthy and passes the production preflight.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from scf_api.cli_secrets import _check_config
from scf_api.main import create_app
from scf_api.services.agent_dispatch import ungoverned_runs_allowed
from scf_api.settings import get_settings

COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.single-user.yml"
#: ``${NAME}``, ``${NAME:-default}`` or ``${NAME:?message}``.
_REFERENCE = re.compile(r"\$\{(\w+)(?::([-?])([^}]*))?\}")


def _resolve(match: re.Match[str]) -> str:
    name, op, arg = match.groups()
    if op == "?":  # required at start: the operator supplies it
        return f"single-user-test-{name.lower()}"
    return arg if op == "-" else ""


def _profile_env() -> dict[str, str]:
    env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["api"]["environment"]
    return {key: _REFERENCE.sub(_resolve, str(value)) for key, value in env.items()}


@pytest.fixture
def profile_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    env = _profile_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("MINIO_ENDPOINT", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield env
    get_settings.cache_clear()


def test_the_api_starts_without_an_object_store_and_reports_healthy(
    profile_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not [key for key in profile_env if key.startswith("MINIO")]
    # The broker is its own container in the profile; keep this test off the network.
    monkeypatch.setenv("FORGE_REALTIME_BACKEND", "memory")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:  # runs the lifespan's startup checks
        health = client.get("/health")
        info = client.get("/")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert info.json()["environment"] == "production"


def test_the_profile_passes_the_production_preflight(
    profile_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _check_config() == 0, capsys.readouterr().err
    # No workflow server in the profile: a repository-changing run is refused (#277).
    assert not ungoverned_runs_allowed()
