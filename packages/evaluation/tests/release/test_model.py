"""Bar ordering, status enum, verdict aggregation, and manifest validation."""

from __future__ import annotations

import pytest

from forge_eval.release.model import (
    MET_STATUSES,
    Bar,
    Gate,
    GateResult,
    ManifestError,
    Status,
    bar_met,
    gates_for_bar,
    parse_gates,
)


def _gate(gid: str, bar: Bar, kind: str = "evidence") -> Gate:
    check: dict = {"kind": kind}
    if kind == "command":
        check["run"] = "true"
    elif kind == "manual":
        check["attestation"] = f"release/attestations/{gid}.yaml"
    else:
        check["artifact"] = "pyproject.toml"
        check["predicate"] = {"type": "exists"}
    return Gate(id=gid, bar=bar, workstream="ws", title="t", check=check)


def _result(gate: Gate, status: Status) -> GateResult:
    return GateResult(gate=gate, status=status)


def test_bar_is_ordered_and_cumulative() -> None:
    assert Bar.ALPHA < Bar.BETA < Bar.PRODUCTION
    assert Bar.parse("beta") is Bar.BETA
    assert Bar.parse("PRODUCTION") is Bar.PRODUCTION
    assert Bar.parse(Bar.ALPHA) is Bar.ALPHA
    with pytest.raises(ValueError):
        Bar.parse("nope")


def test_only_green_and_attested_are_met_statuses() -> None:
    assert {Status.GREEN, Status.MANUAL_ATTESTED} == MET_STATUSES
    for bad in (
        Status.RED,
        Status.SKIPPED_NO_CREDS,
        Status.STALE,
        Status.MANUAL_PENDING,
        Status.MISSING_EVIDENCE,
    ):
        assert bad not in MET_STATUSES


def test_bar_met_selects_at_or_below_target() -> None:
    beta_gate = _gate("G-BETA", Bar.BETA)
    prod_gate = _gate("G-PROD", Bar.PRODUCTION)
    results = [_result(beta_gate, Status.GREEN), _result(prod_gate, Status.MANUAL_PENDING)]
    # Beta ignores the failing production gate.
    assert bar_met(results, Bar.BETA) is True
    # Production includes it → NOT MET.
    assert bar_met(results, Bar.PRODUCTION) is False


def test_bar_met_manual_attested_counts_but_pending_does_not() -> None:
    g = _gate("G-PENTEST", Bar.PRODUCTION, kind="manual")
    assert bar_met([_result(g, Status.MANUAL_ATTESTED)], Bar.PRODUCTION) is True
    assert bar_met([_result(g, Status.MANUAL_PENDING)], Bar.PRODUCTION) is False


def test_empty_selection_is_vacuously_met() -> None:
    # No alpha gates ⇒ alpha is the (already-met) baseline.
    assert bar_met([], Bar.ALPHA) is True


def test_gates_for_bar_preserves_order() -> None:
    gates = [_gate("A", Bar.BETA), _gate("B", Bar.PRODUCTION), _gate("C", Bar.BETA)]
    assert [g.id for g in gates_for_bar(gates, Bar.BETA)] == ["A", "C"]


def test_parse_gates_rejects_bad_kind() -> None:
    with pytest.raises(ManifestError):
        parse_gates({"gates": [{"id": "X", "bar": "beta", "check": {"kind": "bogus"}}]})


def test_parse_gates_rejects_command_without_run() -> None:
    with pytest.raises(ManifestError):
        parse_gates({"gates": [{"id": "X", "bar": "beta", "check": {"kind": "command"}}]})


def test_parse_gates_rejects_duplicate_ids() -> None:
    entry = {"id": "X", "bar": "beta", "check": {"kind": "evidence", "artifact": "a"}}
    with pytest.raises(ManifestError):
        parse_gates({"gates": [entry, dict(entry)]})


def test_parse_gates_happy_path() -> None:
    gates = parse_gates(
        {
            "gates": [
                {
                    "id": "G-DB",
                    "bar": "beta",
                    "blocker": 6,
                    "workstream": "HARD-01",
                    "title": "db",
                    "check": {"kind": "command", "run": "pytest", "required_env": ["X"]},
                }
            ]
        }
    )
    assert gates[0].id == "G-DB"
    assert gates[0].bar is Bar.BETA
    assert gates[0].kind == "command"
    assert gates[0].evidence_ref == "pytest"
