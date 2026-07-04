"""Validation against the merged tree, not subagent agreement (AC 13)."""

from __future__ import annotations

from forge_contracts import AcceptanceCriterion, MergeResult
from forge_coordinator import aggregate_confidence, validate_acceptance


def test_ac_unsatisfied_when_merged_tree_lacks_evidence() -> None:
    criteria = [AcceptanceCriterion(id="ac1", text="adds endpoint", spec_ref="app/api.py")]
    merge = MergeResult(integration_branch="forge/int", changed_files=["app/other.py"])
    checks = validate_acceptance(criteria=criteria, merge=merge, reviewer_ok=True)
    assert checks[0].satisfied is False  # even if subagents self-reported success


def test_ac_satisfied_when_expected_path_present() -> None:
    criteria = [AcceptanceCriterion(id="ac1", text="adds endpoint", spec_ref="app/api.py")]
    merge = MergeResult(integration_branch="forge/int", changed_files=["app/api.py"])
    checks = validate_acceptance(criteria=criteria, merge=merge, reviewer_ok=True)
    assert checks[0].satisfied is True
    assert checks[0].evidence == "app/api.py"


def test_confidence_clamped_below_threshold_on_reviewer_reject() -> None:
    agg = aggregate_confidence(
        required_confidences=[0.95, 0.9], reviewer_rejected=True, threshold=0.72
    )
    assert agg < 0.72


def test_confidence_is_min_of_required() -> None:
    agg = aggregate_confidence(
        required_confidences=[0.95, 0.8], reviewer_rejected=False, threshold=0.72
    )
    assert agg == 0.8
