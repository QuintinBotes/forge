"""Single-user compose profile contract (issue #205; spec 006 FR-019, FR-023).

``deploy/docker-compose.single-user.yml`` runs the API, the worker, Postgres, the
Redis broker the Celery worker needs and a one-shot migration, and nothing else.
These tests parse the file; the last one also renders it with ``docker compose
config`` when the docker CLI is available.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parent.parent
COMPOSE = DEPLOY / "docker-compose.single-user.yml"

PROFILE_SERVICES = {"db", "redis", "migrate", "api", "worker"}
ONE_SHOT = {"migrate"}
PYTHON_SERVICES = ("migrate", "api", "worker")
#: Images and Dockerfiles of what the profile leaves out: object store, reverse
#: proxy, web app, workflow server, observability.
EXCLUDED = re.compile(
    r"minio|caddy|nginx|web\.|temporal|otel|prometheus|grafana|loki|tempo|alertmanager"
)
#: Flags the code or the docs name as development-only (ADR-0007), plus the
#: deprecated FORGE_ENV alias that mis-classified production as development.
DEV_ONLY = {
    "FORGE_DEV_INSECURE",
    "FORGE_ALLOW_SCRIPTED_AGENT",
    "FORGE_ALLOW_FAKE_ACTIVITIES",
    "FORGE_SEED_ADMIN_KEY",
    "FORGE_DEBUG",
    "FORGE_LOCAL_CLI_ENABLED",
    "FORGE_ENV",
}
SECRET_NAME = re.compile(r"(^|_)(SECRET|PASSWORD|TOKEN|KEY)$")
API_PORT = re.compile(r"^127\.0\.0\.1:\$\{SCF_API_PORT:-\d+\}:8000$")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _services() -> dict:
    return _compose()["services"]


def _env(service: str) -> dict:
    env = _services()[service].get("environment") or {}
    assert isinstance(env, dict), f"{service} uses list-form environment"
    return env


# --- criterion 1: the service set ------------------------------------------- #


def test_the_profile_runs_exactly_api_worker_database_migration_and_broker() -> None:
    compose = _compose()
    # Nothing that would make the resolved set differ from the parsed one.
    assert "include" not in compose
    for name, svc in compose["services"].items():
        assert "profiles" not in svc, f"{name} is profile-gated"
        assert "extends" not in svc, f"{name} extends another file"
    assert set(compose["services"]) == PROFILE_SERVICES


def test_the_profile_has_no_object_store_proxy_web_workflow_or_observability() -> None:
    for name, svc in _services().items():
        image = svc.get("image", "")
        dockerfile = (svc.get("build") or {}).get("dockerfile", "")
        assert not EXCLUDED.search(f"{image} {dockerfile}"), f"{name}: {image} {dockerfile}"
    for name in PYTHON_SERVICES:
        env = _env(name)
        assert not [k for k in env if k.startswith("MINIO")], f"{name} points at an object store"
        assert "MCP_GATEWAY_URL" not in env
        assert "TEMPORAL_HOST" not in env


def test_the_worker_reaches_no_container_daemon() -> None:
    """No docker-proxy here, and the host socket is never mounted (F19)."""
    for name, svc in _services().items():
        for mount in svc.get("volumes") or []:
            assert "docker.sock" not in str(mount), f"{name} mounts the Docker socket"
    worker = _env("worker")
    assert "DOCKER_HOST" not in worker
    assert worker["FORGE_SANDBOX_KIND"] == "worktree"


# --- criterion 2: healthchecks and start order ------------------------------ #


def test_every_long_running_service_has_a_healthcheck() -> None:
    for name, svc in _services().items():
        if name in ONE_SHOT:
            assert svc.get("restart") == "no", f"{name} is a one-shot"
            continue
        assert svc.get("healthcheck", {}).get("test"), f"{name} has no healthcheck"


def test_the_api_and_worker_start_only_after_the_migration_completes() -> None:
    services = _services()
    assert "upgrade" in services["migrate"]["command"]
    assert services["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    for name in ("api", "worker"):
        deps = services[name]["depends_on"]
        assert deps["migrate"]["condition"] == "service_completed_successfully", name
        assert deps["db"]["condition"] == "service_healthy", name
        assert deps["redis"]["condition"] == "service_healthy", name


# --- criterion 3: project name and ports ------------------------------------ #


def test_the_profile_has_its_own_project_name() -> None:
    others = {
        yaml.safe_load(path.read_text(encoding="utf-8")).get("name", DEPLOY.name)
        for path in (DEPLOY / "docker-compose.yml", DEPLOY / "docker-compose.dev.yml")
    }
    name = _compose().get("name")
    assert name, "the profile must set its own project name"
    assert name not in others


def test_only_the_api_port_is_published_on_loopback() -> None:
    published = {name: svc["ports"] for name, svc in _services().items() if svc.get("ports")}
    assert list(published) == ["api"], published
    assert len(published["api"]) == 1
    assert API_PORT.match(published["api"][0]), published["api"]
    for name, svc in _services().items():
        assert svc.get("network_mode") != "host", f"{name} uses the host network"


# --- criterion 4: fail closed, no committed secret -------------------------- #


def test_no_development_only_bypass_is_set() -> None:
    for name in PYTHON_SERVICES:
        env = _env(name)
        assert env.get("FORGE_ENVIRONMENT") == "production", name
        assert not DEV_ONLY & set(env), f"{name} sets {sorted(DEV_ONLY & set(env))}"


def test_no_secret_value_is_in_the_file() -> None:
    """A secret is required at start (``${VAR:?}``) or optional and empty (``${VAR:-}``)."""
    secrets = 0
    for name, svc in _services().items():
        for key, value in (svc.get("environment") or {}).items():
            if not SECRET_NAME.search(key):
                continue
            secrets += 1
            pattern = rf"\$\{{{key}(:\?[^}}]*|:-)\}}"
            assert re.fullmatch(pattern, str(value)), f"{name} {key}: {value!r}"
    assert secrets, "no secret-named variable found; the name pattern is stale"
    assert _env("api")["FORGE_SECRET_KEY"].startswith("${FORGE_SECRET_KEY:?")
    # The database password also travels inside the connection URL.
    text = COMPOSE.read_text(encoding="utf-8")
    uses = re.findall(r"\$\{POSTGRES_PASSWORD([^}]*)\}", text)
    assert uses, "the database password is not read from the environment"
    assert all(use.startswith(":?") for use in uses), uses


def test_the_documented_secrets_file_is_never_committed() -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    result = subprocess.run(
        ["git", "check-ignore", "-q", "deploy/.env.single-user"],
        cwd=DEPLOY.parent,
        capture_output=True,
    )
    assert result.returncode == 0, "deploy/.env.single-user is not git-ignored"


# --- the rendered file (docker compose config) ------------------------------ #


def _render(**env: str) -> subprocess.CompletedProcess[str]:
    clean = {
        k: v for k, v in os.environ.items() if k not in {"FORGE_SECRET_KEY", "POSTGRES_PASSWORD"}
    }
    # An empty env file: a local deploy/.env must not supply the secrets.
    command = ["docker", "compose", "-f", str(COMPOSE), "--env-file", os.devnull]
    return subprocess.run(
        [*command, "config", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=str(DEPLOY.parent),
        env={**clean, **env},
    )


def test_docker_compose_renders_the_single_user_profile() -> None:
    if shutil.which("docker") is None:
        pytest.skip("PARKED: docker CLI unavailable in this sandbox")
    assert _render().returncode != 0, "the profile renders without its secrets"
    result = _render(
        FORGE_SECRET_KEY="compose-config-validation-key-000",
        POSTGRES_PASSWORD="compose-config-validation-password",
        SCF_API_PORT="9123",
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert set(rendered["services"]) == PROFILE_SERVICES
    ports = rendered["services"]["api"]["ports"]
    assert [(p["host_ip"], p["published"], p["target"]) for p in ports] == [
        ("127.0.0.1", "9123", 8000)
    ]
