"""Tests for ``scripts/test-services.sh`` and the ``make`` targets around it.

The script runs against a fake ``docker`` on ``PATH`` that records every call and
answers ``inspect`` from ``FAKE_DOCKER_STATE`` (``down`` plays an unreachable daemon) and
``FAKE_PG_BOUND``/``FAKE_REDIS_BOUND`` (a container's published port), so no Docker
daemon is needed.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test-services.sh"
NAMES = ("forge-test-pg", "forge-test-redis")

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = inspect ]; then
  case "$FAKE_DOCKER_STATE" in
    absent) echo "Error: No such object: ${@: -1}" >&2; exit 1 ;;
    down) echo "Cannot connect to the Docker daemon. Is the docker daemon running?" >&2; exit 1 ;;
  esac
  case "$*" in
    *PortBindings*forge-test-pg) echo "${FAKE_PG_BOUND:-$SCF_TEST_PG_PORT}" ;;
    *PortBindings*forge-test-redis) echo "${FAKE_REDIS_BOUND:-$SCF_TEST_REDIS_PORT}" ;;
    *) echo "$FAKE_DOCKER_STATE" ;;
  esac
fi
exit 0
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(
    tmp_path: Path, *args: str, state: str = "absent", **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the script with the fake docker; return the result and the docker calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_STATE": state,
        "SCF_TEST_PG_PORT": str(_free_port()),
        "SCF_TEST_REDIS_PORT": str(_free_port()),
        **env,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60
    )
    return result, log.read_text().splitlines()


def _verbs(calls: list[str], verb: str) -> list[str]:
    return [c for c in calls if c.split()[0] == verb]


def test_up_creates_absent_containers_on_loopback(tmp_path: Path) -> None:
    pg_port, redis_port = str(_free_port()), str(_free_port())
    result, calls = _run(tmp_path, "up", SCF_TEST_PG_PORT=pg_port, SCF_TEST_REDIS_PORT=redis_port)
    assert result.returncode == 0, result.stderr
    runs = _verbs(calls, "run")
    assert len(runs) == 2
    pg, redis = runs
    assert "--name forge-test-pg" in pg and f"-p 127.0.0.1:{pg_port}:5432" in pg
    assert "POSTGRES_USER=forge" in pg and "POSTGRES_DB=forge_test" in pg
    assert "--name forge-test-redis" in redis and f"-p 127.0.0.1:{redis_port}:6379" in redis
    assert not _verbs(calls, "start")
    # Waits for both services to accept connections.
    execs = _verbs(calls, "exec")
    assert any("forge-test-pg pg_isready" in c for c in execs)
    assert any("forge-test-redis redis-cli ping" in c for c in execs)


def test_up_starts_stopped_containers(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "up", state="false")
    assert result.returncode == 0, result.stderr
    assert _verbs(calls, "start") == [f"start {name}" for name in NAMES]
    assert not _verbs(calls, "run")


def test_up_leaves_running_containers_alone(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "up", state="true")
    assert result.returncode == 0, result.stderr
    assert not _verbs(calls, "run") and not _verbs(calls, "start")
    assert {c.split()[0] for c in calls} <= {"inspect", "exec"}


def test_up_names_a_taken_port_and_its_override(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = str(busy.getsockname()[1])
        result, calls = _run(tmp_path, "up", SCF_TEST_PG_PORT=port)
    assert result.returncode != 0
    assert port in result.stderr and "SCF_TEST_PG_PORT" in result.stderr
    assert not _verbs(calls, "run")


@pytest.mark.parametrize("state", ["true", "false"])
def test_up_refuses_an_existing_container_published_on_another_port(
    tmp_path: Path, state: str
) -> None:
    # The container keeps the port it was created with; env would export the override.
    result, calls = _run(tmp_path, "up", state=state, FAKE_PG_BOUND="5432")
    assert result.returncode != 0
    assert "127.0.0.1:5432" in result.stderr and "SCF_TEST_PG_PORT=5432" in result.stderr
    assert "down" in result.stderr
    assert not _verbs(calls, "start") and not _verbs(calls, "run")


def test_up_names_the_override_when_a_stopped_containers_port_is_taken(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = str(busy.getsockname()[1])
        result, calls = _run(tmp_path, "up", state="false", SCF_TEST_PG_PORT=port)
    assert result.returncode != 0
    assert port in result.stderr and "SCF_TEST_PG_PORT" in result.stderr
    assert "down" in result.stderr
    assert not _verbs(calls, "start")


@pytest.mark.parametrize("command", ["up", "down", "status"])
def test_an_unreachable_docker_daemon_fails_instead_of_reading_absent(
    tmp_path: Path, command: str
) -> None:
    result, calls = _run(tmp_path, command, state="down")
    assert result.returncode != 0
    assert "Cannot connect to the Docker daemon" in result.stderr
    assert "absent" not in result.stdout
    assert not _verbs(calls, "rm") and not _verbs(calls, "run")


def test_down_removes_only_the_two_containers(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "down", state="true")
    assert result.returncode == 0, result.stderr
    assert _verbs(calls, "rm") == [f"rm -f -v {name}" for name in NAMES]
    assert {c.split()[0] for c in calls} <= {"inspect", "rm"}


def test_down_is_a_no_op_when_absent(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "down")
    assert result.returncode == 0, result.stderr
    assert not _verbs(calls, "rm")


def test_status_reports_each_container(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, "status", state="false")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"{name}: stopped" for name in NAMES]


def test_env_prints_only_the_two_exports_with_port_overrides(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "env", SCF_TEST_PG_PORT="15432", SCF_TEST_REDIS_PORT="16379")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "export FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@127.0.0.1:15432/forge_test",
        "export FORGE_REDIS_URL=redis://127.0.0.1:16379/0",
    ]
    assert calls == []


def test_env_defaults_to_the_standard_ports(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, "env", SCF_TEST_PG_PORT="", SCF_TEST_REDIS_PORT="")
    assert "@127.0.0.1:5432/" in result.stdout and "127.0.0.1:6379/0" in result.stdout


def test_unknown_command_fails_with_usage(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, "bogus")
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_script_images_match_the_ci_services() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    services = ci["jobs"]["python"]["services"]
    for var, service in (("PG_IMAGE", "postgres"), ("REDIS_IMAGE", "redis")):
        match = re.search(rf'^{var}="([^"]+)"$', script, re.MULTILINE)
        assert match, f"{var} not found in {SCRIPT.name}"
        assert match.group(1) == services[service]["image"], (
            f"{SCRIPT.name} {var} differs from the {service} service of the CI python job"
        )


def test_makefile_has_the_test_service_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    phony = " ".join(re.findall(r"^\.PHONY:((?:.*\\\n)*.*)$", makefile, re.MULTILINE))
    for target in ("test-services-up", "test-services-down", "test-full"):
        assert re.search(rf"^{target}:", makefile, re.MULTILINE), target
        assert target in phony.split(), f"{target} missing from .PHONY"
    recipe = makefile.split("\ntest-full:", 1)[1].split("\n\n", 1)[0]
    # The eval is what exports the URLs; printing them alone leaves pytest unconfigured.
    assert "test-services-up" in recipe and 'eval "$$(scripts/test-services.sh env)"' in recipe
    assert "uv run pytest" in recipe
