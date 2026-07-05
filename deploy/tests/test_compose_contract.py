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
    proc = subprocess.run(["bash", str(PREFLIGHT)], env=env, capture_output=True, text=True)
    return proc.returncode


# --------------------------------------------------------------------------- #
# F25 — Temporal profile services (AC18)                                       #
# --------------------------------------------------------------------------- #

_TEMPORAL_SERVICES = ("temporal", "temporal-ui", "temporal-worker")


def test_temporal_services_are_profile_gated() -> None:
    """AC18 — every Temporal service is opt-in behind the `temporal` profile."""
    for name in _TEMPORAL_SERVICES:
        assert "temporal" in _service(name).get("profiles", []), f"{name} not profile-gated"


def test_temporal_services_publish_no_host_ports() -> None:
    """AC18 — none of the Temporal services publish host ports (UI via Caddy)."""
    for name in _TEMPORAL_SERVICES:
        assert "ports" not in _service(name), f"{name} must not publish ports"


def test_temporal_services_inherit_hardening_matrix() -> None:
    """AC18 — digest/tag-pinned image, capped logs, resource limits, autoheal."""
    for name in _TEMPORAL_SERVICES:
        svc = _service(name)
        assert svc["logging"]["options"]["max-size"] == "100m"
        assert svc["logging"]["options"]["max-file"] == "5"
        assert "limits" in svc["deploy"]["resources"]
        assert "autoheal=true" in svc["labels"]


def test_temporal_services_on_internal_networks_only() -> None:
    """AC18 — Temporal reaches Postgres over the internal `data` network; the
    server/worker never sit on `edge`; only the UI is bridged to `edge` (Caddy)."""
    assert "data" in _service("temporal")["networks"]
    assert "edge" not in _service("temporal")["networks"]
    assert "edge" not in _service("temporal-worker")["networks"]
    # The Web UI is fronted by Caddy, so it is the only one on `edge`.
    assert "edge" in _service("temporal-ui")["networks"]


def test_temporal_services_have_healthchecks() -> None:
    for name in _TEMPORAL_SERVICES:
        assert "healthcheck" in _service(name), f"{name} missing healthcheck"


def test_temporal_worker_runs_temporal_entrypoint() -> None:
    """AC18 — temporal-worker runs the F25 entrypoint with the temporal backend."""
    worker = _service("temporal-worker")
    assert worker["command"] == ["python", "-m", "forge_worker.temporal_main"]
    assert worker["environment"]["WORKFLOW_ENGINE_BACKEND"] == "temporal"


def test_caddy_exposes_admin_gated_temporal_route() -> None:
    """AC18 — the Temporal Web UI is reachable only via Caddy's gated /_temporal."""
    caddy = (DEPLOY / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "/_temporal" in caddy
    assert "basic_auth" in caddy
    assert "temporal-ui:8080" in caddy


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_rejects_old_engine(tmp_path: Path) -> None:
    """AC17 — preflight.sh fails on Docker Engine < 26.1 (stubbed version)."""
    assert _run_preflight_with_stub_docker(tmp_path, "25.0.4") != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_accepts_new_engine(tmp_path: Path) -> None:
    assert _run_preflight_with_stub_docker(tmp_path, "26.1.4") == 0


# --------------------------------------------------------------------------- #
# F34 — kernel-boundary sandbox runtimes (gVisor / Kata-Firecracker)           #
# --------------------------------------------------------------------------- #

INSTALL_RUNTIMES = DEPLOY / "scripts" / "install-runtimes.sh"


def _stub_docker_with_runtimes(tmp_path: Path, runtimes_json: str) -> dict[str, str]:
    """A stub ``docker`` handling both `version` and `info` (F34 preflight)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "docker"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            case "$1" in
              version) echo "26.1.4" ;;
              info) echo '{runtimes_json}' ;;
            esac
            """
        )
    )
    stub.chmod(0o755)
    return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")


def _run_preflight(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(PREFLIGHT)], env=env, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_gvisor_runtime_check(tmp_path: Path) -> None:
    """AC12 — FORGE_SANDBOX_KIND=gvisor fails without runsc, with the hint."""
    env = _stub_docker_with_runtimes(tmp_path, '{"runc":{"path":"runc"}}')
    env["FORGE_SANDBOX_KIND"] = "gvisor"
    proc = _run_preflight(env)
    assert proc.returncode != 0
    assert "runsc" in proc.stderr
    assert "install-runtimes.sh" in proc.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_gvisor_passes_when_registered(tmp_path: Path) -> None:
    env = _stub_docker_with_runtimes(
        tmp_path, '{"runc":{"path":"runc"},"runsc":{"path":"/usr/local/bin/runsc"}}'
    )
    env["FORGE_SANDBOX_KIND"] = "gvisor"
    assert _run_preflight(env).returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_microvm_kvm_check(tmp_path: Path) -> None:
    """AC13 — FORGE_SANDBOX_KIND=microvm fails when /dev/kvm is absent."""
    env = _stub_docker_with_runtimes(tmp_path, '{"runc":{"path":"runc"},"kata-fc":{"path":"kata"}}')
    env["FORGE_SANDBOX_KIND"] = "microvm"
    env["FORGE_KVM_DEVICE"] = str(tmp_path / "no-such-kvm")
    proc = _run_preflight(env)
    assert proc.returncode != 0
    assert "kvm" in proc.stderr.lower()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_microvm_passes_with_runtime_and_kvm(tmp_path: Path) -> None:
    env = _stub_docker_with_runtimes(tmp_path, '{"runc":{"path":"runc"},"kata-fc":{"path":"kata"}}')
    kvm = tmp_path / "kvm"
    kvm.write_bytes(b"")
    env["FORGE_SANDBOX_KIND"] = "microvm"
    env["FORGE_KVM_DEVICE"] = str(kvm)
    assert _run_preflight(env).returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_preflight_microvm_missing_runtime_names_installer(tmp_path: Path) -> None:
    env = _stub_docker_with_runtimes(tmp_path, '{"runc":{"path":"runc"}}')
    env["FORGE_SANDBOX_KIND"] = "microvm"
    proc = _run_preflight(env)
    assert proc.returncode != 0
    assert "kata-fc" in proc.stderr
    assert "--firecracker" in proc.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_install_runtimes_merges_daemon_json(tmp_path: Path) -> None:
    """F34 — install-runtimes.sh merges the runtimes block idempotently."""
    import json

    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"log-driver": "json-file"}')
    env = dict(
        os.environ,
        FORGE_DAEMON_JSON=str(daemon_json),
        FORGE_SKIP_INSTALL="1",
        FORGE_SKIP_RESTART="1",
        FORGE_SKIP_VERIFY="1",
    )
    proc = subprocess.run(
        ["bash", str(INSTALL_RUNTIMES), "--gvisor", "--firecracker"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    config = json.loads(daemon_json.read_text())
    assert config["log-driver"] == "json-file"  # pre-existing keys preserved
    assert config["runtimes"]["runsc"]["path"] == "/usr/local/bin/runsc"
    assert "kata-fc" in config["runtimes"]

    # Idempotent: a second run leaves the file byte-identical.
    before = daemon_json.read_text()
    proc2 = subprocess.run(
        ["bash", str(INSTALL_RUNTIMES), "--gvisor", "--firecracker"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc2.returncode == 0, proc2.stderr
    assert daemon_json.read_text() == before


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_install_runtimes_requires_a_flag(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["bash", str(INSTALL_RUNTIMES)],
        env=dict(os.environ, FORGE_DAEMON_JSON=str(tmp_path / "d.json")),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


def test_shipped_daemon_json_documents_runtimes() -> None:
    """F34 — deploy/docker/daemon.json ships the runsc + kata-fc registrations."""
    import json

    config = json.loads((DEPLOY / "docker" / "daemon.json").read_text(encoding="utf-8"))
    assert config["runtimes"]["runsc"]["path"]
    assert config["runtimes"]["kata-fc"]["path"]
    # No runtime is the daemon default: HostConfig.Runtime selects it per-container.
    assert "default-runtime" not in config


def test_worker_env_carries_f34_sandbox_settings() -> None:
    """F34 — compose passes the kernel-runtime settings through to the worker."""
    env = _service("worker")["environment"]
    assert "FORGE_SANDBOX_GVISOR_RUNTIME" in env
    assert "FORGE_SANDBOX_MICROVM_RUNTIME" in env
    assert "FORGE_SANDBOX_REQUIRE_KVM" in env
    assert "FORGE_SANDBOX_JAILER_ROOT" in env


def test_docker_proxy_verbs_unchanged_by_f34() -> None:
    """AC15 — runtime selection is a create-body field; no new proxy verbs."""
    env = _service("docker-proxy")["environment"]
    expected = {"CONTAINERS", "IMAGES", "POST", "EXEC", "INFO"}
    assert {verb for verb in expected if env.get(verb) == "1"} == expected
    for denied in ("NETWORKS", "VOLUMES", "SWARM", "SERVICES", "SECRETS", "BUILD"):
        assert env[denied] == "0"
