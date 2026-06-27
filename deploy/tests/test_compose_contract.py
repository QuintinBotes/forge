"""F19 compose/network hardening contract tests (AC15, AC17).

These assert the structural guarantees of the sandbox additions to
``deploy/docker-compose.yml`` (no parsing of a live daemon): the worker never
mounts the raw Docker socket, the socket-proxy is the sole socket holder and
exposes only the minimal verbs, the sandbox networks are ``internal: true``, the
egress proxy is hardened, and ``preflight.sh`` gates Docker Engine < 26.1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parent.parent
COMPOSE = DEPLOY / "docker-compose.yml"
PREFLIGHT = DEPLOY / "scripts" / "preflight.sh"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _service(name: str) -> dict:
    return _compose()["services"][name]


def _volume_mounts(service: dict) -> list[str]:
    return [v if isinstance(v, str) else v.get("source", "") for v in service.get("volumes", [])]


def test_worker_has_no_docker_socket() -> None:
    """AC15 — the worker never mounts the raw Docker socket."""
    worker = _service("worker")
    for mount in _volume_mounts(worker):
        assert "docker.sock" not in mount


def test_worker_uses_socket_proxy() -> None:
    """AC15 — worker reaches the daemon via the proxy on the control network."""
    worker = _service("worker")
    assert worker["environment"]["DOCKER_HOST"] == "tcp://docker-proxy:2375"
    assert "sandbox_ctl" in worker["networks"]


def test_docker_proxy_is_sole_socket_holder_and_readonly() -> None:
    """AC15 — only docker-proxy mounts the socket, read-only (besides autoheal)."""
    compose = _compose()
    socket_holders = {}
    for name, svc in compose["services"].items():
        for v in svc.get("volumes", []) or []:
            target = v if isinstance(v, str) else f"{v.get('source')}:{v.get('target')}"
            if "docker.sock" in target:
                socket_holders[name] = target
    # docker-proxy mounts it read-only; autoheal is F14's pre-existing exception.
    assert "docker-proxy" in socket_holders
    assert socket_holders["docker-proxy"].endswith(":ro")
    assert set(socket_holders) <= {"docker-proxy", "autoheal"}


def test_docker_proxy_minimal_verbs() -> None:
    """AC15 — proxy enables only CONTAINERS/IMAGES/POST/EXEC/INFO; rest denied."""
    env = _service("docker-proxy")["environment"]
    for verb in ("CONTAINERS", "IMAGES", "POST", "EXEC", "INFO"):
        assert env[verb] == "1", f"{verb} must be enabled"
    for denied in ("NETWORKS", "VOLUMES", "SWARM", "SERVICES", "SECRETS", "BUILD"):
        assert env[denied] == "0", f"{denied} must be denied"


def test_sandbox_networks_internal() -> None:
    """AC17 — both sandbox networks are internal (no internet route)."""
    networks = _compose()["networks"]
    assert networks["sandbox_ctl"]["internal"] is True
    assert networks["sandbox_egress"]["internal"] is True


def test_sandbox_proxy_hardened() -> None:
    """AC17 — egress proxy: no published ports, autoheal label, capped logs, limits."""
    proxy = _service("sandbox-proxy")
    assert "ports" not in proxy, "sandbox-proxy must not publish ports"
    assert "autoheal=true" in proxy["labels"]
    assert proxy["logging"]["options"]["max-size"] == "100m"
    assert "limits" in proxy["deploy"]["resources"]
    assert "sandbox_egress" in proxy["networks"]


def test_docker_proxy_hardened() -> None:
    """AC17 — socket-proxy: no published ports, healthcheck, capped logs, limits."""
    proxy = _service("docker-proxy")
    assert "ports" not in proxy
    assert "healthcheck" in proxy
    assert proxy["logging"]["options"]["max-file"] == "5"
    assert "limits" in proxy["deploy"]["resources"]


def test_worktree_volume_declared_and_mounted() -> None:
    """The worktree named volume exists and the worker mounts it."""
    compose = _compose()
    assert "forge_repos" in compose["volumes"]
    assert any("forge_repos" in m for m in _volume_mounts(_service("worker")))


def test_preflight_engine_version_gate() -> None:
    """AC17 — preflight.sh enforces Docker Engine >= 26.1."""
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert "MIN_ENGINE_MAJOR=26" in text
    assert "MIN_ENGINE_MINOR=1" in text
    assert "Server.Version" in text


def _run_preflight_with_stub_docker(tmp_path: Path, reported_version: str) -> int:
    """Run preflight.sh with a stub ``docker`` on PATH reporting ``reported_version``."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # stub docker: only `docker version --format ...` is exercised.
            echo "{reported_version}"
            """
        )
    )
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    proc = subprocess.run(
        ["bash", str(PREFLIGHT)], env=env, capture_output=True, text=True
    )
    return proc.returncode


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_rejects_old_engine(tmp_path: Path) -> None:
    """AC17 — preflight.sh fails on Docker Engine < 26.1 (stubbed version)."""
    assert _run_preflight_with_stub_docker(tmp_path, "25.0.4") != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_accepts_new_engine(tmp_path: Path) -> None:
    assert _run_preflight_with_stub_docker(tmp_path, "26.1.4") == 0
