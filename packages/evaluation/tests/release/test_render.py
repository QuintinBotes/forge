"""Golden-ish assertions on the rendered RELEASE_READINESS.md (AC9)."""

from __future__ import annotations

from forge_eval.release.model import Bar, Gate, GateResult, Status
from forge_eval.release.render import COMPLIANCE_NOTE, HONEST_ASTERISK, render_markdown


def _gate(gid: str, bar: Bar, kind: str = "evidence") -> Gate:
    check = (
        {"kind": "command", "run": "make typecheck"}
        if kind == "command"
        else {"kind": "manual", "attestation": f"release/attestations/{gid}.yaml"}
    )
    return Gate(id=gid, bar=bar, workstream="HARD-12", title="t", check=check, blocker=6)


def _report(*results: GateResult, target: Bar = Bar.PRODUCTION) -> str:
    return render_markdown(
        list(results),
        target=target,
        git_sha="deadbeef",
        cz_version="0.1.0",
        generated_at="2026-07-04T00:00:00Z",
    )


def test_render_has_required_columns_and_header() -> None:
    r = GateResult(
        gate=_gate("G-TYPES", Bar.BETA, "command"),
        status=Status.GREEN,
        checked_at="2026-07-04T00:00:00Z",
    )
    out = _report(r, target=Bar.BETA)
    for col in [
        "Gate",
        "Blocker",
        "Workstream",
        "Status",
        "Evidence (cmd/artifact)",
        "Last-checked",
    ]:
        assert col in out
    assert "`deadbeef`" in out  # git sha
    assert "`0.1.0`" in out  # cz version
    assert "2026-07-04T00:00:00Z" in out
    assert "`G-TYPES`" in out
    assert "make typecheck" in out  # evidence ref


def test_render_verbatim_honest_asterisk_footer() -> None:
    out = _report(
        GateResult(gate=_gate("G-PENTEST", Bar.PRODUCTION, "manual"), status=Status.MANUAL_PENDING)
    )
    assert HONEST_ASTERISK in out
    assert COMPLIANCE_NOTE in out
    # The exact spec wording, defended against drift.
    assert "pending an external human penetration test" in HONEST_ASTERISK
    assert "multi-week multi-tenant fleet soak" in HONEST_ASTERISK


def test_render_verdict_met_when_all_green() -> None:
    r = GateResult(gate=_gate("G-TYPES", Bar.BETA, "command"), status=Status.GREEN)
    out = _report(r, target=Bar.BETA)
    assert "is **MET**" in out
    assert "is **NOT MET**" not in out
    assert "✅ **MET**" in out


def test_render_verdict_not_met_lists_failures() -> None:
    pending = GateResult(
        gate=_gate("G-PENTEST", Bar.PRODUCTION, "manual"),
        status=Status.MANUAL_PENDING,
        detail="awaiting a signed human attestation",
    )
    out = _report(pending)
    assert "NOT MET" in out
    assert "G-PENTEST" in out
    assert "awaiting a signed human attestation" in out
