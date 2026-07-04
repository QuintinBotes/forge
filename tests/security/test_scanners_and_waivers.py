"""HARD-09 — scanner-config, waiver-expiry, SBOM, and evidence-pack tests.

These prove the *tooling half* of the audit: the custom semgrep rules really
fire (positive + negative fixtures), the waiver file fails closed on expiry,
the committed SBOM covers the workspace, and the human-facing evidence pack
exists and names the residual human pentest.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_check_waivers():
    spec = importlib.util.spec_from_file_location(
        "check_waivers", REPO_ROOT / "scripts" / "security" / "check_waivers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Waivers (AC2)                                                                #
# --------------------------------------------------------------------------- #


def test_committed_waivers_are_valid() -> None:
    module = _load_check_waivers()
    errors = module.validate_waivers(REPO_ROOT / "security" / "waivers.yaml")
    assert errors == [], f"security/waivers.yaml is invalid: {errors}"


def test_expired_waiver_fails_closed(tmp_path: Path) -> None:
    module = _load_check_waivers()
    expired = tmp_path / "waivers.yaml"
    expired.write_text(
        """
waivers:
  - id: W-EXPIRED
    tool: bandit
    rule: B999
    location: nowhere.py:1
    finding: synthetic
    justification: synthetic expired waiver for the fail-closed test
    owner: forge-security
    created: 2025-01-01
    expires: 2025-06-01
"""
    )
    errors = module.validate_waivers(expired, today=dt.date(2026, 7, 4))
    assert any("EXPIRED" in error for error in errors), errors


def test_waiver_missing_fields_fail(tmp_path: Path) -> None:
    module = _load_check_waivers()
    incomplete = tmp_path / "waivers.yaml"
    incomplete.write_text(
        """
waivers:
  - id: W-INCOMPLETE
    tool: bandit
    expires: 2999-01-01
"""
    )
    errors = module.validate_waivers(incomplete)
    missing = {e.split("'")[1] for e in errors if "missing required field" in e}
    assert {"rule", "location", "finding", "justification", "owner", "created"} <= missing


# --------------------------------------------------------------------------- #
# Semgrep custom rules (AC3) — positive + negative fixtures                    #
# --------------------------------------------------------------------------- #

EXPECTED_SEMGREP_RULES = {
    "forge-no-subprocess-shell-true",
    "forge-no-tls-verify-false",
    "forge-no-unsafe-yaml-load",
    "forge-no-unsafe-yaml-loader-class",
    "forge-no-mcp-write-default",
    "forge-no-eval-exec",
    "forge-no-literal-cipher-key",
    "forge-no-secret-logging",
}


def test_semgrep_rules_fire_on_bad_and_not_on_good(tmp_path: Path) -> None:
    if shutil.which("semgrep") is None and not (REPO_ROOT / ".venv" / "bin" / "semgrep").exists():
        pytest.skip("PARKED: semgrep binary not installed in this environment")

    # Copy the fixtures out of the repo so .semgrepignore cannot mask them.
    target = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, target)
    semgrep = str(REPO_ROOT / ".venv" / "bin" / "semgrep")
    if not Path(semgrep).exists():
        semgrep = "semgrep"
    proc = subprocess.run(
        [
            semgrep,
            "--config",
            str(REPO_ROOT / ".semgrep" / "forge.yml"),
            "--metrics=off",
            "--disable-version-check",
            "--json",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr  # 1 = findings with some configs
    report = json.loads(proc.stdout)
    assert report.get("errors") == [], report.get("errors")

    fired: set[str] = set()
    for result in report["results"]:
        path = Path(result["path"])
        rule = result["check_id"].rsplit(".", 1)[-1]
        assert "bad" in path.parts, f"rule {rule} fired on the good fixture: {path}"
        fired.add(rule)
    assert fired == EXPECTED_SEMGREP_RULES, (
        f"missing: {EXPECTED_SEMGREP_RULES - fired}; unexpected: {fired - EXPECTED_SEMGREP_RULES}"
    )


# --------------------------------------------------------------------------- #
# Bandit config (AC1 support) — the planted-vuln tree must trip the SAST gate  #
# --------------------------------------------------------------------------- #


def test_bandit_flags_planted_shell_true(tmp_path: Path) -> None:
    bandit = REPO_ROOT / ".venv" / "bin" / "bandit"
    if not bandit.exists():
        pytest.skip("PARKED: bandit not installed in this environment")
    planted = tmp_path / "planted.py"
    planted.write_text("import subprocess\n\n\ndef f(cmd):\n    subprocess.run(cmd, shell=True)\n")
    proc = subprocess.run(
        [str(bandit), "-r", str(tmp_path), "--severity-level", "high", "-q"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode != 0, "bandit did not fail on a planted shell=True"
    assert "B602" in proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# SBOM (AC17)                                                                  #
# --------------------------------------------------------------------------- #


def test_committed_sbom_lists_workspace_packages() -> None:
    sbom_path = REPO_ROOT / "docs" / "security" / "evidence" / "sbom.cdx.json"
    assert sbom_path.exists(), "SBOM missing — run scripts/security/run.sh (sbom step)"
    sbom = json.loads(sbom_path.read_text())
    assert sbom.get("bomFormat") == "CycloneDX"
    names = {str(component.get("name", "")).lower() for component in sbom.get("components", [])}
    # Every first-party workspace member ships in the SBOM…
    expected_first_party = {
        "forge-contracts",
        "forge-db",
        "forge-api",
        "forge-worker",
        "forge-mcp",
        "forge-knowledge",
        "forge-policy",
    }
    missing = expected_first_party - names
    assert not missing, f"SBOM is missing workspace packages: {missing}"
    # …alongside third-party dependencies.
    assert {"fastapi", "sqlalchemy", "cryptography"} <= names


# --------------------------------------------------------------------------- #
# Evidence pack (AC18)                                                         #
# --------------------------------------------------------------------------- #


def test_evidence_pack_files_exist_and_name_the_residual() -> None:
    required = {
        REPO_ROOT / "SECURITY.md": "vulnerability",
        REPO_ROOT / "SECURITY_FINDINGS.md": "bandit",
        REPO_ROOT / "docs" / "security" / "threat-model.md": "STRIDE",
        REPO_ROOT / "docs" / "security" / "pentest-punch-list.md": "penetration test",
        REPO_ROOT / "docs" / "self-hosting" / "security.md": "rotation",
        REPO_ROOT / "docs" / "security" / "evidence" / "enforcement-matrix.md": "GENERATED",
        REPO_ROOT / "security" / "enforcement-matrix.yaml": "controls",
        REPO_ROOT / "security" / "waivers.yaml": "waivers",
    }
    for path, marker in required.items():
        assert path.exists(), f"evidence pack file missing: {path}"
        text = path.read_text()
        assert len(text) > 200, f"evidence pack file is a stub: {path}"
        assert marker.lower() in text.lower(), f"{path} lacks expected content {marker!r}"

    # The punch-list must state, honestly, that the human pentest is NOT done.
    punch = (REPO_ROOT / "docs" / "security" / "pentest-punch-list.md").read_text().lower()
    assert "has not been performed" in punch or "not yet been performed" in punch


def test_gitignore_covers_secret_material() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    for pattern in ("deploy/secrets/", ".env", "*.pem", "*.key"):
        assert pattern in gitignore, f".gitignore is missing {pattern!r}"
