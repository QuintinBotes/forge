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

import json
import re
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
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
NEXT_CONFIG = REPO_ROOT / "apps" / "web" / "next.config.mjs"
BUILD_MANIFEST = DEPLOY / "build-manifest.json"

# HARD-07 §4 image-reference contract: every *pulled* image is pinned by tag AND
# immutable digest; first-party built images keep `build:` + a `forge/<svc>` tag.
PINNED_IMAGE_RE = re.compile(r"^[^:@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

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


# --------------------------------------------------------------------------- #
# HARD-07 — image digest pinning + build hygiene (offline half of G-IMG-PINNED)#
# --------------------------------------------------------------------------- #


def _pulled_images(compose_path: Path) -> dict[str, str]:
    """Service -> image for every *pulled* (non-first-party) image reference."""
    services = _load(compose_path)["services"]
    pulled: dict[str, str] = {}
    for name, svc in services.items():
        image = svc.get("image")
        if image is None or image.startswith("forge/"):
            # First-party images are built locally (build: + forge/<svc> tag) —
            # their digests are recorded in deploy/build-manifest.json instead.
            continue
        pulled[name] = image
    return pulled


def _dockerfile_from_refs() -> dict[str, list[str]]:
    """Dockerfile name -> every `FROM` image reference (multi-stage included)."""
    refs: dict[str, list[str]] = {}
    for dockerfile in sorted((DEPLOY / "docker").glob("*.Dockerfile")):
        file_refs = []
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM "):
                # `FROM <ref> [AS <stage>]` — flags like --platform are not used.
                file_refs.append(line.split()[1])
        refs[dockerfile.name] = file_refs
    return refs


@pytest.mark.parametrize("compose_path", [COMPOSE, COMPOSE_DEV], ids=["prod", "dev"])
def test_prod_pulled_images_pinned_by_digest(compose_path: Path) -> None:
    pulled = _pulled_images(compose_path)
    assert pulled, f"{compose_path.name}: expected at least one pulled image"
    for name, image in pulled.items():
        assert PINNED_IMAGE_RE.match(image), (
            f"{compose_path.name}: service {name!r} image is not pinned "
            f"name:tag@sha256:<64-hex>: {image}"
        )
        tag = image.split("@", 1)[0]
        assert not tag.endswith(":latest"), f"{compose_path.name}: {name} pins :latest"


def test_dockerfile_base_images_pinned_by_digest() -> None:
    refs = _dockerfile_from_refs()
    assert refs, "no Dockerfiles found under deploy/docker/"
    for dockerfile, images in refs.items():
        assert images, f"{dockerfile}: no FROM lines found"
        for image in images:
            assert PINNED_IMAGE_RE.match(image), (
                f"{dockerfile}: FROM is not pinned name:tag@sha256:<64-hex>: {image}"
            )


def test_dockerignore_excludes_secrets_and_heavy_paths() -> None:
    assert DOCKERIGNORE.is_file(), "root .dockerignore missing (build context = repo root)"
    entries = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in (
        ".git",
        ".venv",
        "node_modules",
        "apps/web/.next",
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "deploy/secrets",
    ):
        assert required in entries, f".dockerignore is missing the {required!r} exclusion"


def test_web_next_config_emits_standalone() -> None:
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    assert 'output: "standalone"' in text, (
        'apps/web/next.config.mjs must set `output: "standalone"` so the web '
        "runtime image ships only the traced server (HARD-07 §3.4)"
    )


def test_build_manifest_covers_every_image() -> None:
    assert BUILD_MANIFEST.is_file(), "deploy/build-manifest.json missing"
    manifest = json.loads(BUILD_MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("forge_version"), "manifest missing forge_version"
    assert manifest.get("generated_at"), "manifest missing generated_at"
    images = manifest.get("images", {})
    assert images, "manifest lists no images"

    for ref, entry in images.items():
        kind = entry.get("kind")
        assert kind in {"pulled", "base", "built"}, f"{ref}: bad kind {kind!r}"
        assert DIGEST_RE.match(entry.get("digest", "")), f"{ref}: bad/missing digest"
        if kind == "built":
            sbom = entry.get("sbom")
            assert sbom, f"{ref}: built image missing sbom path"
            assert (REPO_ROOT / sbom).is_file(), f"{ref}: sbom file missing: {sbom}"

    # Coverage: every pulled compose ref and every Dockerfile base ref (digest
    # stripped) must be recorded, plus the 4 first-party built images.
    recorded = set(images)
    for compose_path in (COMPOSE, COMPOSE_DEV):
        for service, image in _pulled_images(compose_path).items():
            bare = image.split("@", 1)[0]
            assert bare in recorded, f"manifest missing pulled image {bare} ({service})"
    for dockerfile, refs in _dockerfile_from_refs().items():
        for image in refs:
            bare = image.split("@", 1)[0]
            assert bare in recorded, f"manifest missing base image {bare} ({dockerfile})"
    for svc in ("api", "worker", "mcp-gateway", "web"):
        matches = [r for r in recorded if r.startswith(f"forge/{svc}:")]
        assert matches, f"manifest missing built image forge/{svc}:<version>"
        assert all(images[m]["kind"] == "built" for m in matches)


def test_prod_app_healthchecks_use_python_not_curl() -> None:
    """The uv slim runtime base ships no curl/wget; the api and mcp-gateway
    healthchecks must use the stdlib urllib probe (latent-bug fix, HARD-07)."""
    services = _load(COMPOSE)["services"]
    for name in ("api", "mcp-gateway"):
        test_cmd = " ".join(str(part) for part in services[name]["healthcheck"]["test"])
        assert "curl" not in test_cmd, f"{name} healthcheck shells out to curl (absent in image)"
        assert "urllib.request" in test_cmd, f"{name} healthcheck must use python urllib"
    # web runs on node:*-slim (no wget either) — must probe via node fetch.
    web_cmd = " ".join(str(part) for part in services["web"]["healthcheck"]["test"])
    assert "wget" not in web_cmd, "web healthcheck shells out to wget (absent in image)"
    assert "fetch(" in web_cmd, "web healthcheck must use node fetch"
