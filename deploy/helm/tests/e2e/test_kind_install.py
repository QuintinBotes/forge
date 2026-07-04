"""kind/k3d-gated smoke install + upgrade/rollback of the Forge chart (AC18, AC19).

Marked ``@pytest.mark.kind``; SKIPS (parked, never faked) unless ``kind``,
``helm``, ``kubectl`` and a live Docker daemon are all available AND the four
locally-built ``forge/{api,worker,web,mcp-gateway}`` images are present (the
container-build workstream / ``make build-images`` produces them). On a networked
CI runner the fixture builds/loads them; in the hermetic sandbox the criterion is
reported as skipped, not passed.

The smoke drives the chart's EXTERNAL-datastore path (Journey B) against an
in-cluster official ``pgvector`` + ``redis`` (``datastores.yaml``) rather than the
bundled Bitnami subcharts, whose versioned images were withdrawn from Docker Hub
in Bitnami's 2025 catalog deprecation. It exercises the SAME migrate hook (the
real Alembic chain, incl. ``CREATE EXTENSION vector``, on real in-cluster
pgvector), the SAME four workloads to ``Available``, and the SAME ``helm test``
probe of the deployed API over its in-cluster Service. To run the bundled path
instead, override the three subchart images to ``bitnamilegacy/*`` (see
docs/self-hosting/kubernetes.md).

Run locally:
    make build-images                       # produce forge/{api,worker,web,mcp-gateway}:<ver>
    uv run pytest deploy/helm/tests/e2e -m kind
Reuse an existing cluster (skip create/delete):
    FORGE_KIND_CLUSTER=my-cluster uv run pytest deploy/helm/tests/e2e -m kind
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.kind

E2E_DIR = Path(__file__).resolve().parent
CHART_DIR = E2E_DIR.parents[1] / "forge"
VALUES_KIND = CHART_DIR / "tests" / "e2e" / "values.kind.yaml"
DATASTORES = CHART_DIR / "tests" / "e2e" / "datastores.yaml"

NAMESPACE = "forge"
RELEASE = "forge"

KIND = shutil.which("kind")
HELM = shutil.which("helm")
KUBECTL = shutil.which("kubectl")
DOCKER = shutil.which("docker")

APP_VERSION = str(yaml.safe_load((CHART_DIR / "Chart.yaml").read_text())["appVersion"])
FORGE_IMAGES = [f"forge/{c}:{APP_VERSION}" for c in ("api", "worker", "web", "mcp-gateway")]
# Datastore + test images pre-loaded so the cluster never has to pull them.
DATASTORE_IMAGES = ["pgvector/pgvector:pg16", "redis:7-alpine", "curlimages/curl:8.11.1"]
DEPLOYMENTS = ["forge-api", "forge-worker", "forge-web", "forge-mcp-gateway"]


def _image_present(image: str) -> bool:
    return subprocess.run(
        [DOCKER, "image", "inspect", image], capture_output=True, check=False
    ).returncode == 0


def _tooling_ready() -> bool:
    if not (KIND and HELM and KUBECTL and DOCKER):
        return False
    if subprocess.run([DOCKER, "info"], capture_output=True, check=False).returncode != 0:
        return False
    # The forge images must exist; datastore/test images are pulled on demand.
    return all(_image_present(img) for img in FORGE_IMAGES)


requires_kind = pytest.mark.skipif(
    not _tooling_ready(),
    reason=(
        "PARKED: kind/helm/kubectl/docker or the built forge/* images are not all "
        "available — the kind smoke runs on a Docker-enabled CI runner. Build the "
        "images with `make build-images` first."
    ),
)


def _run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)  # type: ignore[arg-type]


def _kubectl(ctx: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([KUBECTL, "--context", ctx, "-n", NAMESPACE, *args])


def _helm(ctx: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([HELM, *args, "-n", NAMESPACE, "--kube-context", ctx])


@pytest.fixture(scope="module")
def cluster() -> Iterator[str]:
    """Provide a ready kind context: cluster + loaded images + external datastores."""
    reuse = os.environ.get("FORGE_KIND_CLUSTER")
    name = reuse or f"forge-e2e-{uuid.uuid4().hex[:8]}"
    ctx = f"kind-{name}"
    created = False
    try:
        if not reuse:
            assert _run([KIND, "create", "cluster", "--name", name]).returncode == 0
            created = True
        # Pre-pull datastore/test images on the host, then load everything into kind.
        for img in DATASTORE_IMAGES:
            if not _image_present(img):
                _run([DOCKER, "pull", img])
        for img in FORGE_IMAGES + DATASTORE_IMAGES:
            load = _run([KIND, "load", "docker-image", "--name", name, img])
            assert load.returncode == 0, load.stderr
        _kubectl(ctx, "create", "namespace", NAMESPACE)  # ignore "already exists"
        apply = _run(
            [KUBECTL, "--context", ctx, "-n", NAMESPACE, "apply", "-f", str(DATASTORES)]
        )
        assert apply.returncode == 0, apply.stderr
        for dep in ("forge-ext-postgres", "forge-ext-redis"):
            wait = _kubectl(ctx, "rollout", "status", f"deploy/{dep}", "--timeout=180s")
            assert wait.returncode == 0, wait.stderr
        yield ctx
    finally:
        if created:
            _run([KIND, "delete", "cluster", "--name", name])


@requires_kind
def test_bundled_install_reaches_ready(cluster: str) -> None:
    """AC18: install brings the migrate hook to Completed, every Deployment to
    Available, and `helm test` proves the deployed API answers over its Service."""
    ctx = cluster
    # Idempotent for a reused cluster: clear any prior release + lingering hooks.
    _helm(ctx, "uninstall", RELEASE, "--wait")
    for kind_, obj in (("configmap", "forge-env"), ("secret", "forge-secret"), ("sa", "forge")):
        _kubectl(ctx, "delete", kind_, obj, "--ignore-not-found")

    install = _helm(
        ctx, "install", RELEASE, str(CHART_DIR),
        "-f", str(VALUES_KIND), "--wait", "--timeout", "10m",
    )
    # helm only returns 0 here if the pre-install migrate hook Completed AND every
    # Deployment reached Available under --wait.
    assert install.returncode == 0, install.stdout + install.stderr

    for dep in DEPLOYMENTS:
        avail = _kubectl(
            ctx, "get", "deploy", dep,
            "-o", "jsonpath={.status.availableReplicas}",
        )
        assert avail.stdout.strip() and int(avail.stdout) >= 1, f"{dep} not Available"

    # The vector extension + alembic head really applied on the in-cluster pgvector.
    pgpod = _kubectl(
        ctx, "get", "pod", "-l", "app=forge-ext-postgres",
        "-o", "jsonpath={.items[0].metadata.name}",
    ).stdout.strip()
    ext = _kubectl(
        ctx, "exec", pgpod, "--", "psql", "-U", "forge", "-d", "forge", "-tAc",
        "SELECT extname FROM pg_extension WHERE extname='vector';",
    )
    assert ext.stdout.strip() == "vector", ext.stdout + ext.stderr

    test = _helm(ctx, "test", RELEASE, "--logs")
    assert test.returncode == 0, test.stdout + test.stderr


@requires_kind
def test_upgrade_runs_migration_and_rollback(cluster: str) -> None:
    """AC19: upgrade re-runs the pre-upgrade hook before pods roll; rollback
    restores the prior revision healthy (mechanics only — populated-DB data
    preservation is HARD-13/G-MIGRATE)."""
    ctx = cluster
    upgrade = _helm(
        ctx, "upgrade", RELEASE, str(CHART_DIR),
        "-f", str(VALUES_KIND), "--set", "forge.logLevel=debug",
        "--wait", "--timeout", "10m",
    )
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

    for dep in DEPLOYMENTS:
        avail = _kubectl(
            ctx, "get", "deploy", dep,
            "-o", "jsonpath={.status.availableReplicas}",
        )
        assert avail.stdout.strip() and int(avail.stdout) >= 1, f"{dep} not Available after upgrade"

    rollback = _helm(ctx, "rollback", RELEASE, "1", "--wait", "--timeout", "5m")
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
