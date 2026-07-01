"""``forge marketplace package`` CLI round-trip (AC20)."""

from __future__ import annotations

from pathlib import Path

import yaml

from forge_api.cli_marketplace import main
from forge_marketplace.manifest import load_manifest


def test_package_roundtrips_reverifiable_content_hash(tmp_path: Path) -> None:
    artifact = tmp_path / "backend-tdd.yaml"
    artifact.write_text(
        yaml.safe_dump(
            {
                "name": "backend-tdd-strict",
                "description": "strict tdd",
                "min_test_coverage": 95,
                "verification_steps": ["lint", "unit_tests"],
            }
        )
    )
    out = tmp_path / "dist"
    code = main(
        [
            "package",
            str(artifact),
            "--kind",
            "skill_profile",
            "--slug",
            "backend-tdd-strict",
            "--name",
            "Backend TDD (strict)",
            "--version",
            "1.2.0",
            "--summary",
            "hardened",
            "--tag",
            "tdd",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    pkg_file = out / "forge-package.yaml"
    assert pkg_file.is_file()
    # load_manifest recomputes + asserts the content_hash — no raise == re-verified.
    manifest = load_manifest(pkg_file.read_text())
    assert manifest.slug == "backend-tdd-strict"
    assert manifest.tags == ["tdd"]
    assert manifest.content_hash.startswith("sha256:")


def test_package_rejects_invalid_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.yaml"
    artifact.write_text(yaml.safe_dump({"id": "c", "name": "C", "transport": "stdio"}))
    code = main(
        [
            "package",
            str(artifact),
            "--kind",
            "mcp_connector",
            "--slug",
            "bad-conn",
            "--name",
            "Bad",
            "--version",
            "1.0.0",
            "--summary",
            "x",
            "--out",
            str(tmp_path / "dist"),
        ]
    )
    assert code == 2  # stdio transport rejected by the security floor
