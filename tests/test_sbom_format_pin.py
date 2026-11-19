"""The SBOMs SCF commits declare CycloneDX at one pinned version (#214, spec 007 FR-022).

Reads the committed JSON directly, so it needs no Syft and runs in the default lane.
The Syft-gated generation test stays in ``tests/test_source_sbom.py``. Upgrading
CycloneDX is an explicit change: regenerate the files and change the pin here and in
ADR-0016 together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The one CycloneDX version SCF's SBOMs declare (ADR-0016).
CYCLONEDX_SPEC_VERSION = "1.7"

SOURCE_SBOM = REPO_ROOT / "release" / "sbom" / "forge-source.cdx.json"
IMAGE_SBOMS = sorted(p for p in (REPO_ROOT / "deploy" / "sbom").iterdir() if p.is_file())
SBOMS = [SOURCE_SBOM, *IMAGE_SBOMS]


def _declared(path: Path) -> tuple[object, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("bomFormat"), data.get("specVersion")


def test_the_source_sbom_and_the_image_sboms_exist() -> None:
    assert SOURCE_SBOM.is_file()
    assert {p.name for p in IMAGE_SBOMS} >= {
        f"{image}.cdx.json" for image in ("api", "worker", "web", "mcp-gateway")
    }


@pytest.mark.parametrize("path", SBOMS, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_each_committed_sbom_declares_the_pinned_cyclonedx_version(path: Path) -> None:
    assert _declared(path) == ("CycloneDX", CYCLONEDX_SPEC_VERSION)


def test_a_file_at_another_version_fails_the_pin(tmp_path: Path) -> None:
    other = tmp_path / "other.cdx.json"
    other.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}))
    assert _declared(other) != ("CycloneDX", CYCLONEDX_SPEC_VERSION)
