"""Pure tests for the shadow governance gate: every branch (spec §3.8)."""

from __future__ import annotations

from forge_world import Precedent, gate

HUMAN_AGREE = Precedent(human=True, decision="accept")
HUMAN_CONTRA = Precedent(human=True, decision="reject")
BOT_AGREE = Precedent(human=False, decision="accept")


def test_type_conflict_always_holds() -> None:
    decision = gate("accept", 0.99, type_conflict=True, precedents=[HUMAN_AGREE])
    assert decision.decision == "hold"
    assert decision.threshold is None


def test_contradicting_human_precedent_holds() -> None:
    decision = gate("accept", 0.99, type_conflict=False, precedents=[HUMAN_CONTRA])
    assert decision.decision == "hold"
    # And the mirrored direction.
    decision = gate("reject", 0.99, type_conflict=False, precedents=[HUMAN_AGREE])
    assert decision.decision == "hold"


def test_agreeing_human_precedent_lowers_threshold() -> None:
    decision = gate("accept", 0.80, type_conflict=False, precedents=[HUMAN_AGREE])
    assert decision.decision == "auto"
    assert decision.threshold == 0.75
    decision = gate("accept", 0.74, type_conflict=False, precedents=[HUMAN_AGREE])
    assert decision.decision == "hold"


def test_default_threshold_is_stricter() -> None:
    decision = gate("accept", 0.84, type_conflict=False)
    assert decision.decision == "hold"
    assert decision.threshold == 0.85
    decision = gate("accept", 0.85, type_conflict=False)
    assert decision.decision == "auto"


def test_non_human_precedents_do_not_count() -> None:
    decision = gate("accept", 0.80, type_conflict=False, precedents=[BOT_AGREE])
    assert decision.decision == "hold"  # threshold stays 0.85
    assert decision.threshold == 0.85


def test_indecisive_or_missing_verdict_holds() -> None:
    assert gate("unsure", 0.99, type_conflict=False).decision == "hold"
    assert gate(None, 0.99, type_conflict=False).decision == "hold"
    assert gate("accept", None, type_conflict=False).decision == "hold"


def test_gate_dict_shape_is_stored_shape() -> None:
    payload = gate("accept", 0.9, type_conflict=False).to_dict()
    assert set(payload) == {"decision", "threshold", "reasons"}
    assert payload["decision"] == "auto"
