"""HARD-10 AC16 — the `observability` compose profile is hardened.

Structural contract over ``deploy/docker-compose.yml`` (parsed as YAML — no live
daemon, hermetic): the six observability services exist under the profile, each
pinned ``@sha256``, non-root, with a healthcheck, CPU/mem limits, capped logs,
the ``autoheal=true`` label, on the ``internal: true`` ``observability`` network,
and none publishes a host port. A separate docker-gated test asserts the same
via ``docker compose --profile observability config`` when a daemon is present.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parent.parent
COMPOSE = DEPLOY / "docker-compose.yml"

#: The six services the profile must bring up (spec §3.5 / AC16).
OBS_SERVICES = ("otel-collector", "prometheus", "grafana", "loki", "tempo", "alertmanager")

_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _service(name: str) -> dict:
    return _compose()["services"][name]


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_service_is_in_observability_profile(name: str) -> None:
    svc = _service(name)
    assert svc.get("profiles") == ["observability"], f"{name} must be gated on the profile"


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_image_is_digest_pinned(name: str) -> None:
    image = _service(name)["image"]
    assert _DIGEST_RE.search(image), f"{name} image must be pinned @sha256: {image!r}"


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_runs_non_root(name: str) -> None:
    user = str(_service(name).get("user", ""))
    assert user and not user.startswith("0") and user != "root", (
        f"{name} must run as an explicit non-root user (got {user!r})"
    )


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_has_healthcheck(name: str) -> None:
    hc = _service(name).get("healthcheck")
    assert hc and hc.get("test"), f"{name} must define a healthcheck"


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_has_resource_limits(name: str) -> None:
    limits = _service(name).get("deploy", {}).get("resources", {}).get("limits", {})
    assert limits.get("cpus"), f"{name} must set a CPU limit"
    assert limits.get("memory"), f"{name} must set a memory limit"


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_has_capped_logging(name: str) -> None:
    opts = _service(name).get("logging", {}).get("options", {})
    assert opts.get("max-size"), f"{name} must cap log size"
    assert opts.get("max-file"), f"{name} must cap log file count"


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_has_autoheal_label(name: str) -> None:
    labels = _service(name).get("labels", [])
    assert "autoheal=true" in labels, f"{name} must carry the autoheal=true label"


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_on_internal_observability_network(name: str) -> None:
    assert "observability" in _service(name).get("networks", []), (
        f"{name} must join the observability network"
    )


@pytest.mark.parametrize("name", OBS_SERVICES)
def test_publishes_no_host_port(name: str) -> None:
    """No observability service is directly published — Grafana is via Caddy."""
    assert not _service(name).get("ports"), f"{name} must not publish a host port"


def test_observability_network_is_internal() -> None:
    net = _compose()["networks"]["observability"]
    assert net.get("internal") is True, "the observability network must be internal: true"


def test_collector_and_prometheus_reach_backend() -> None:
    """The collector receives OTLP and Prometheus scrapes the apps (on backend)."""
    assert "backend" in _service("otel-collector")["networks"]
    assert "backend" in _service("prometheus")["networks"]


def test_grafana_reachable_via_caddy_edge() -> None:
    """Grafana shares the edge network with Caddy so /grafana can proxy to it."""
    assert "edge" in _service("grafana")["networks"]


def test_named_volumes_declared() -> None:
    volumes = _compose()["volumes"]
    for vol in ("prometheus-data", "grafana-data", "loki-data", "tempo-data", "alertmanager-data"):
        assert vol in volumes, f"missing named volume {vol}"


def test_config_assets_exist() -> None:
    base = DEPLOY / "observability"
    for rel in (
        "otel-collector/config.yaml",
        "prometheus/prometheus.yml",
        "prometheus/rules/forge.rules.yml",
        "prometheus/rules/forge.alerts.yml",
        "loki/loki-config.yaml",
        "tempo/tempo.yaml",
        "alertmanager/alertmanager.yml",
        "grafana/provisioning/datasources/datasources.yaml",
        "grafana/provisioning/dashboards/dashboards.yaml",
    ):
        assert (base / rel).is_file(), f"missing config asset {rel}"


@pytest.mark.docker
def test_compose_config_validates_with_profile() -> None:
    """AC16 — `docker compose --profile observability config` validates."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    env = dict(os.environ)
    env.setdefault("FORGE_SECRET_KEY", "x" * 48)
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--profile", "observability", "config"],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0 and "Cannot connect to the Docker daemon" in proc.stderr:
        pytest.skip("docker daemon not reachable")
    assert proc.returncode == 0, proc.stderr
    for name in OBS_SERVICES:
        assert f"\n  {name}:" in proc.stdout or f"{name}:" in proc.stdout
