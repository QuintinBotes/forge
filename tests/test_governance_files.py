"""Governance files present + minimally valid (HARD-12, AC11)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    path = REPO_ROOT / name
    assert path.is_file(), f"required governance file missing: {name}"
    return path.read_text(encoding="utf-8")


def test_security_md_has_private_channel_versions_and_crosslink() -> None:
    text = _read("SECURITY.md")
    lowered = text.lower()
    # A private reporting channel.
    assert "security advisor" in lowered or "report a vulnerability" in lowered
    assert "do not open a public" in lowered
    # Supported-versions section referencing SemVer.
    assert "supported versions" in lowered
    assert "semver" in lowered or "semantic versioning" in lowered
    # Cross-link to the operator hardening/rotation runbook (HARD-09).
    assert "docs/self-hosting/security.md" in text


def test_contributing_has_green_gate_and_conventional_commits() -> None:
    text = _read("CONTRIBUTING.md")
    assert "ruff check" in text
    assert "pytest" in text
    assert "typecheck" in text
    assert "Conventional Commits" in text
    # The conventional-commit enforcement hook (make hooks / cz check).
    assert "cz check" in text or "make hooks" in text


def test_code_of_conduct_is_contributor_covenant() -> None:
    text = _read("CODE_OF_CONDUCT.md")
    assert "Contributor Covenant" in text
