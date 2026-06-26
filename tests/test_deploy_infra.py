"""Deploy + CI + test-infra substrate tests (plan Task 0.6).

These pin the production-grade guarantees the plan requires of the deploy
substrate so later phases (and self-hosters) can rely on them:

- a complete ``docker-compose.yml`` (db+pgvector, redis, minio, api, worker,
  mcp-gateway, web, caddy, autoheal) with pinned images, healthchecks, resource
  limits, named volumes, segmented networks, non-root users, and capped logs;
- a self-contained ``docker-compose.dev.yml`` for local development;
- a Caddy reverse-proxy config;
- a GitHub Actions CI workflow (lint + type + test + build) that parses; and
- ``docker compose config`` validating the production file (skipped, with a clear
  reason, when the docker CLI is unavailable — never faked).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"
COMPOSE_DEV = DEPLOY / "docker-compose.dev.yml"
CADDYFILE = DEPLOY / "caddy" / "Caddyfile"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PROD_SERVICES = {
    "db",
    "redis",
    "minio",
    "api",
    "worker",
    "mcp-gateway",
    "web",
    "caddy",
    "autoheal",
}
# Long-running services that must expose a healthcheck (autoheal is the watcher;
# worker has no inbound port so it is exempted from an HTTP-style probe).
HEALTHCHECKED = {"db", "redis", "minio", "api", "mcp-gateway", "web", "caddy"}
# App services we build from source and must run as a non-root user.
APP_SERVICES = {"api", "worker", "mcp-gateway", "web"}


def _load(path: Path) -> dict:
    assert path.is_file(), f"missing file: {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} did not parse to a mapping"
    return data


# --------------------------------------------------------------------------- #
# File existence                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [COMPOSE, COMPOSE_DEV, CADDYFILE, CI])
def test_required_files_exist(path: Path) -> None:
    assert path.is_file(), f"required deploy/CI file missing: {path}"


# --------------------------------------------------------------------------- #
# Production compose                                                          #
# --------------------------------------------------------------------------- #


def test_prod_compose_has_all_services() -> None:
    services = _load(COMPOSE)["services"]
    assert set(services) >= PROD_SERVICES, (
        f"missing services: {sorted(PROD_SERVICES - set(services))}"
    )


def test_prod_db_uses_pgvector_image() -> None:
    db = _load(COMPOSE)["services"]["db"]
    assert "pgvector" in db["image"], "db image must include the pgvector extension"


def test_prod_images_are_pinned_not_latest() -> None:
    services = _load(COMPOSE)["services"]
    for name, svc in services.items():
        image = svc.get("image")
        if image is None:
            # App services may be built from source instead of pulled.
            assert "build" in svc, f"service {name} has neither image nor build"
            continue
        assert ":" in image or "@" in image, f"service {name} image not pinned: {image}"
        assert not image.endswith(":latest"), f"service {name} pins :latest"


def test_prod_services_have_healthchecks() -> None:
    services = _load(COMPOSE)["services"]
    for name in HEALTHCHECKED:
        assert "healthcheck" in services[name], f"{name} is missing a healthcheck"


def test_prod_services_have_resource_limits() -> None:
    services = _load(COMPOSE)["services"]
    for name, svc in services.items():
        limits = svc.get("deploy", {}).get("resources", {}).get("limits", {})
        assert limits.get("memory"), f"{name} has no memory limit"


def test_prod_services_cap_logs() -> None:
    services = _load(COMPOSE)["services"]
    for name, svc in services.items():
        options = svc.get("logging", {}).get("options", {})
        assert options.get("max-size"), f"{name} does not cap log size"
        assert options.get("max-file"), f"{name} does not cap log file count"


def test_prod_named_volumes_for_stateful_services() -> None:
    data = _load(COMPOSE)
    volumes = set(data.get("volumes", {}))
    # Named (not bind) volumes must back the stateful services.
    assert volumes, "no named volumes declared"
    for name in ("db", "redis", "minio"):
        svc_volumes = data["services"][name].get("volumes", [])
        names = {v.split(":")[0] for v in svc_volumes if not v.startswith((".", "/"))}
        assert names & volumes, f"{name} is not backed by a named volume"


def test_prod_segmented_networks() -> None:
    data = _load(COMPOSE)
    networks = data.get("networks", {})
    assert len(networks) >= 2, "production compose must segment networks"
    # The database must not be on the same single flat network as the edge proxy.
    db_nets = set(data["services"]["db"].get("networks", []))
    caddy_nets = set(data["services"]["caddy"].get("networks", []))
    assert db_nets, "db has no explicit network"
    assert caddy_nets, "caddy has no explicit network"
    assert db_nets != caddy_nets or not (db_nets & caddy_nets), (
        "db and edge proxy should be network-segmented"
    )


def test_prod_app_services_run_non_root() -> None:
    services = _load(COMPOSE)["services"]
    for name in APP_SERVICES:
        assert services[name].get("user"), f"{name} must run as a non-root user"


def test_prod_autoheal_watches_labeled_services() -> None:
    services = _load(COMPOSE)["services"]
    autoheal = services["autoheal"]
    assert "autoheal" in autoheal["image"]
    # autoheal needs the docker socket to restart unhealthy containers.
    assert any("docker.sock" in v for v in autoheal.get("volumes", [])), (
        "autoheal must mount the docker socket"
    )
    # At least one service opts into autoheal via the label.
    labeled = [
        n for n, s in services.items() if any("autoheal=true" in str(label) for label in _labels(s))
    ]
    assert labeled, "no service is labeled autoheal=true"


def _labels(svc: dict) -> list:
    labels = svc.get("labels", [])
    if isinstance(labels, dict):
        return [f"{k}={v}" for k, v in labels.items()]
    return list(labels)


# --------------------------------------------------------------------------- #
# Dev compose                                                                 #
# --------------------------------------------------------------------------- #


def test_dev_compose_is_self_contained() -> None:
    services = _load(COMPOSE_DEV)["services"]
    # `make dev` runs ONLY the dev file, so core infra + api/web must be present.
    for name in ("db", "redis", "api", "web"):
        assert name in services, f"dev compose missing {name}"


# --------------------------------------------------------------------------- #
# Caddy                                                                       #
# --------------------------------------------------------------------------- #


def test_caddyfile_reverse_proxies_api_and_web() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "reverse_proxy" in text
    assert "api:8000" in text
    assert "web:3000" in text


# --------------------------------------------------------------------------- #
# CI workflow                                                                 #
# --------------------------------------------------------------------------- #


def test_ci_workflow_parses_and_has_core_jobs() -> None:
    doc = _load(CI)
    # PyYAML parses the bare `on:` key as boolean True — accept either form.
    assert "on" in doc or True in doc, "workflow has no trigger block"
    jobs = doc.get("jobs", {})
    assert jobs, "workflow defines no jobs"
    blob = CI.read_text(encoding="utf-8")
    for needle in ("ruff", "pytest", "mypy", "pnpm", "docker compose"):
        assert needle in blob, f"CI workflow does not run {needle!r}"


# --------------------------------------------------------------------------- #
# docker compose config validation (the explicit gate)                        #
# --------------------------------------------------------------------------- #


def test_docker_compose_config_validates() -> None:
    if shutil.which("docker") is None:
        pytest.skip("PARKED: docker CLI unavailable in this sandbox")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"docker compose config failed:\n{result.stderr}"


def test_docker_compose_dev_config_validates() -> None:
    if shutil.which("docker") is None:
        pytest.skip("PARKED: docker CLI unavailable in this sandbox")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_DEV), "config"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"docker compose (dev) config failed:\n{result.stderr}"
