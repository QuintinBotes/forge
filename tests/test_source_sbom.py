"""Source-tree SBOM generation (HARD-12, AC12).

Skips cleanly (never fakes) when Syft is not installed, mirroring the Postgres
skip pattern. When Syft is present it runs release/scripts/source-sbom.sh for real
and asserts the output is valid CycloneDX spanning both Python (uv.lock) and Node
(pnpm-lock.yaml) dependencies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "release" / "scripts" / "source-sbom.sh"

pytestmark = pytest.mark.integration


@pytest.mark.skipif(shutil.which("syft") is None, reason="requires Syft — not available here")
def test_source_sbom_is_valid_cyclonedx_spanning_py_and_node(tmp_path: Path) -> None:
    out = tmp_path / "forge-source.cdx.json"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(out)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"source-sbom.sh failed: {result.stderr[-2000:]}"
    assert out.is_file()

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data.get("bomFormat") == "CycloneDX"
    components = data.get("components") or []
    assert len(components) >= 1

    ecosystems = set()
    for comp in components:
        purl = comp.get("purl", "")
        if purl.startswith("pkg:"):
            ecosystems.add(purl.split(":", 1)[1].split("/", 1)[0])
    assert "pypi" in ecosystems, f"SBOM has no Python packages; ecosystems={ecosystems}"
    assert "npm" in ecosystems, f"SBOM has no Node packages; ecosystems={ecosystems}"
