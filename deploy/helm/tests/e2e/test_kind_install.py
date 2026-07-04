"""kind-gated smoke install/upgrade of the Forge chart (AC18, AC19).

Marked ``@pytest.mark.kind``; SKIPS (parked) unless ``kind``, ``helm`` and a live
Docker daemon are all available. CI runs this on a Docker-enabled runner that has
loaded the four F14 images so digests/refs resolve. It is never faked: when the
tooling is absent the criterion is reported as skipped, not passed.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

CHART_DIR = Path(__file__).resolve().parents[2] / "forge"

pytestmark = pytest.mark.kind

KIND = shutil.which("kind")
HELM = shutil.which("helm")
KUBECTL = shutil.which("kubectl")
DOCKER = shutil.which("docker")


def _tooling_ready() -> bool:
    if not (KIND and HELM and KUBECTL and DOCKER):
        return False
    probe = subprocess.run([DOCKER, "info"], capture_output=True, text=True, check=False)
    return probe.returncode == 0


requires_kind = pytest.mark.skipif(
    not _tooling_ready(),
    reason="PARKED: kind/helm/kubectl/docker not all available — kind smoke runs in CI.",
)


@pytest.fixture(scope="module")
def kind_cluster() -> Iterator[str]:
    name = f"forge-e2e-{uuid.uuid4().hex[:8]}"
    subprocess.run([KIND, "create", "cluster", "--name", name], check=True)
    try:
        yield name
    finally:
        subprocess.run([KIND, "delete", "cluster", "--name", name], check=False)


def _kube_env(cluster: str) -> dict[str, str]:
    import os

    return {**os.environ, "KIND_CLUSTER": cluster}


@requires_kind
def test_bundled_install_reaches_ready(kind_cluster: str) -> None:
    env = _kube_env(kind_cluster)
    ctx = f"kind-{kind_cluster}"
    subprocess.run([HELM, "dependency", "build", str(CHART_DIR)], check=True, env=env)
    subprocess.run([KUBECTL, "--context", ctx, "create", "namespace", "forge"], check=True, env=env)
    install = subprocess.run(
        [
            HELM,
            "install",
            "forge",
            str(CHART_DIR),
            "--namespace",
            "forge",
            "--kube-context",
            ctx,
            "-f",
            str(CHART_DIR / "values.example.yaml"),
            "--wait",
            "--timeout",
            "15m",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert install.returncode == 0, install.stderr
    test = subprocess.run(
        [HELM, "test", "forge", "--namespace", "forge", "--kube-context", ctx],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert test.returncode == 0, test.stdout + test.stderr


@requires_kind
def test_upgrade_runs_migration_and_rollback(kind_cluster: str) -> None:
    ctx = f"kind-{kind_cluster}"
    env = _kube_env(kind_cluster)
    upgrade = subprocess.run(
        [
            HELM,
            "upgrade",
            "forge",
            str(CHART_DIR),
            "--namespace",
            "forge",
            "--kube-context",
            ctx,
            "-f",
            str(CHART_DIR / "values.example.yaml"),
            "--set",
            "forge.logLevel=debug",
            "--wait",
            "--timeout",
            "15m",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    rollback = subprocess.run(
        [HELM, "rollback", "forge", "1", "--namespace", "forge", "--kube-context", ctx],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert rollback.returncode == 0, rollback.stderr
