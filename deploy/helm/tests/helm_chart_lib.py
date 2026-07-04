"""Shared helpers + constants for the Helm chart static test suite (F24).

Pure module (no pytest fixtures) so both ``conftest.py`` and the test modules can
import it under pytest's default (prepend) import mode.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[1] / "forge"
VALUES_DEFAULT = CHART_DIR / "values.yaml"
VALUES_EXAMPLE = CHART_DIR / "values.example.yaml"
VALUES_PRODUCTION = CHART_DIR / "values-production.yaml"

# Profile name -> extra `helm template` args.
PROFILES: dict[str, list[str]] = {
    "default": [],
    "example": ["-f", str(VALUES_EXAMPLE)],
    "production": ["-f", str(VALUES_PRODUCTION)],
}

# Every stateless workload; "every workload" assertions parametrize over this so
# they stay DRY as workloads are added.
WORKLOADS = ["forge-api", "forge-worker", "forge-web", "forge-mcp-gateway"]

HELM = shutil.which("helm")
KUBECONFORM = shutil.which("kubeconform")


def require_helm() -> str:
    """Return the helm path or skip (parked) when helm/subcharts are unavailable."""
    if HELM is None:
        pytest.skip("PARKED: `helm` not on PATH — chart render tests run in CI.")
    if not (CHART_DIR / "charts").is_dir():
        pytest.skip(
            "PARKED: chart dependencies not built — run "
            f"`helm dependency build {CHART_DIR}` (needs network) first."
        )
    return HELM


@cache
def _render_text(profile: str) -> str:
    helm = require_helm()
    args = [helm, "template", "forge", str(CHART_DIR), *PROFILES[profile]]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"helm template ({profile}) failed:\n{result.stderr}")
    return result.stdout


def render_docs(profile: str) -> list[dict]:
    """Return the parsed list of rendered Kubernetes docs for a values profile."""
    return [d for d in yaml.safe_load_all(_render_text(profile)) if d]
