"""HARD-07 — networked container build / bring-up / SBOM integration tests.

These exercise the deploy substrate against a REAL Docker daemon + registry
network (release blocker #3): `docker compose build` of all 4 first-party
images, a full `up -> healthy -> /health -> down -v` smoke under a distinct
compose project name, runtime non-root/healthcheck/limit inspection, and
per-image CycloneDX SBOM generation.

Gating (PARK-don't-fake): every test is `@pytest.mark.integration` and skips
with a clear reason when a Docker daemon is unavailable, and — mirroring the
F19 `FORGE_SANDBOX_DOCKER_TESTS` precedent — unless the operator opts in via
``FORGE_BUILD_INTEGRATION_TESTS=1`` (a compose build + full-stack bring-up is
far too heavy for the default hermetic suite). CI's networked build job and
the HARD-07 verification run set the env var explicitly:

    FORGE_BUILD_INTEGRATION_TESTS=1 uv run pytest -m integration tests/test_build_integration.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"
SBOM_DIR = REPO_ROOT / "deploy" / "sbom"
FORGE_VERSION = os.environ.get("FORGE_VERSION", "0.1.0")
PROJECT = "forge-prod-smoke"
BUILT_SERVICES = ("api", "worker", "mcp-gateway", "web")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("docker") is None,
        reason="requires a Docker daemon + build network — not available in this environment",
    ),
    pytest.mark.skipif(
        os.environ.get("FORGE_BUILD_INTEGRATION_TESTS") != "1",
        reason="opt-in via FORGE_BUILD_INTEGRATION_TESTS=1 (compose build + full-stack smoke)",
    ),
]


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args]


def test_compose_build_all_images() -> None:
    result = _run(["docker", "compose", "-f", str(COMPOSE), "build"])
    assert result.returncode == 0, f"docker compose build failed:\n{result.stderr[-4000:]}"
    for svc in BUILT_SERVICES:
        image = f"forge/{svc}:{FORGE_VERSION}"
        inspect = _run(["docker", "image", "inspect", image], timeout=60)
        assert inspect.returncode == 0, f"built image missing after build: {image}"


def test_compose_up_health_smoke() -> None:
    """Run the operator smoke script end-to-end (up -> healthy -> /health -> down -v)."""
    result = _run(["bash", str(REPO_ROOT / "deploy" / "scripts" / "smoke.sh")])
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"smoke.sh failed:\n{output[-6000:]}"
    assert "SMOKE PASS" in output, f"smoke.sh did not report PASS:\n{output[-6000:]}"
    compact = output.replace(" ", "")
    assert '"status":"ok"' in compact, "no /health status=ok evidence in smoke output"


def test_runtime_containers_nonroot_healthcheck_limits() -> None:
    """`docker inspect` evidence: non-root user, healthcheck present, memory limit
    enforced, and the healthcheck interpreter (python / node) present in-image."""
    up = _run(_compose("up", "-d", "--no-build", *BUILT_SERVICES), timeout=600)
    try:
        assert up.returncode == 0, f"compose up failed:\n{up.stderr[-4000:]}"
        for svc in BUILT_SERVICES:
            ps = _run(_compose("ps", "-q", svc), timeout=60)
            container_id = ps.stdout.strip().splitlines()[0]
            inspect = _run(["docker", "inspect", container_id], timeout=60)
            assert inspect.returncode == 0, f"docker inspect failed for {svc}"
            info = json.loads(inspect.stdout)[0]

            # The compose `user: "1000:1000"` directive governs every app
            # container (it overrides the image USER, including web's `node`,
            # which is the same uid/gid 1000).
            user = info["Config"]["User"]
            assert user == "1000:1000", f"{svc} runs as {user!r}, expected '1000:1000'"

            assert info["Config"].get("Healthcheck", {}).get("Test"), (
                f"{svc} container has no healthcheck configured"
            )
            assert info["HostConfig"]["Memory"] > 0, f"{svc} has no memory limit enforced"

        # The web IMAGE itself must still default to the non-root `node` user
        # (defense in depth if the compose `user:` line is ever dropped).
        web_image = f"forge/web:{FORGE_VERSION}"
        image_user = _run(
            ["docker", "image", "inspect", "--format", "{{.Config.User}}", web_image],
            timeout=60,
        )
        assert image_user.stdout.strip() == "node", (
            f"forge/web image USER is {image_user.stdout.strip()!r}, expected 'node'"
        )

        # Healthcheck interpreter must exist inside the runtime images (the uv /
        # node slim bases ship no curl/wget — the probes use python / node).
        for svc, probe in (
            ("api", ["python", "-c", "import urllib.request"]),
            ("mcp-gateway", ["python", "-c", "import urllib.request"]),
            ("web", ["node", "-e", "process.exit(0)"]),
        ):
            exec_res = _run(_compose("exec", "-T", svc, *probe), timeout=120)
            assert exec_res.returncode == 0, (
                f"{svc}: healthcheck interpreter missing in image:\n{exec_res.stderr[-1000:]}"
            )
    finally:
        _run(_compose("down", "-v", "--remove-orphans"), timeout=600)


def test_sbom_generated_for_each_image() -> None:
    if shutil.which("syft") is None:
        pytest.skip("PARKED: syft unavailable — install anchore syft to generate SBOMs")
    result = _run(["bash", str(REPO_ROOT / "deploy" / "scripts" / "sbom.sh")])
    assert result.returncode == 0, f"sbom.sh failed:\n{result.stderr[-4000:]}"
    for svc in BUILT_SERVICES:
        sbom_path = SBOM_DIR / f"{svc}.cdx.json"
        assert sbom_path.is_file(), f"missing SBOM: {sbom_path}"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        assert sbom.get("bomFormat") == "CycloneDX", f"{svc}: not a CycloneDX document"
        assert sbom.get("components"), f"{svc}: SBOM lists no components"
