"""The shipped release/gates.yaml covers every spec gate + the two asterisks (AC5)."""

from __future__ import annotations

from pathlib import Path

from forge_eval.release.model import load_gates

REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST = REPO_ROOT / "release" / "gates.yaml"

# The 18 lettered gates in the production readiness model (release/gates.yaml).
SPEC_GATES = {
    "G-DB",
    "G-MODEL",
    "G-RAG-REAL",
    "G-GH",
    "G-MCP",
    "G-SLACK",
    "G-BUILD",
    "G-TYPES",
    "G-SEC-AUTOMATED",
    "G-CRYPTO",
    "G-IMG-PINNED",
    "G-PARKED-CLOSED",
    "G-PERF",
    "G-MIGRATE",
    "G-SOAK",
    "G-COVERAGE",
    "G-SEC-EVIDENCE",
    "G-FWD-COMPAT",
}
HUMAN_ONLY = {"G-PENTEST", "G-SOAK-FLEET"}


def test_manifest_parses() -> None:
    gates = load_gates(MANIFEST)
    assert len(gates) >= len(SPEC_GATES) + len(HUMAN_ONLY)


def test_manifest_covers_every_spec_gate() -> None:
    ids = {g.id for g in load_gates(MANIFEST)}
    missing = SPEC_GATES - ids
    assert not missing, f"gates.yaml is missing spec gates: {sorted(missing)}"


def test_human_only_gates_present_and_manual() -> None:
    by_id = {g.id: g for g in load_gates(MANIFEST)}
    for gid in HUMAN_ONLY:
        assert gid in by_id, f"missing human-only gate {gid}"
        assert by_id[gid].kind == "manual", f"{gid} must be a manual (never auto-green) gate"
