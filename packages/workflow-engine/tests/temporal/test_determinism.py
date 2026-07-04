"""Pure-unit tests for the IO-free DSL transition evaluator (F25 AC2/5/7/8/12)."""

from __future__ import annotations

import pytest

from forge_contracts import WorkflowState
from forge_workflow.default_workflow import default_feature_definition
from forge_workflow.exceptions import (
    GuardFailedError,
    InvalidTransitionError,
    PreconditionError,
)
from forge_workflow.temporal.determinism import PureGuardContext, TransitionEvaluator


@pytest.fixture
def evaluator() -> TransitionEvaluator:
    return TransitionEvaluator(default_feature_definition())


def test_resolve_happy_path_action_edges(evaluator: TransitionEvaluator) -> None:
    d = evaluator.resolve("created", "generate_spec_draft")
    assert d.to_state == WorkflowState.SPEC_DRAFTING
    assert d.effects == ["generate_spec_draft"]
    assert d.skill == "spec-analyst"

    d = evaluator.resolve("spec_review", "spec_approved_by_human")
    assert d.to_state == WorkflowState.SPEC_APPROVED
    assert d.record == "approval_event"


def test_invalid_event_raises_with_no_rule(evaluator: TransitionEvaluator) -> None:
    with pytest.raises(InvalidTransitionError):
        evaluator.resolve("spec_review", "plan_approved_by_human")


def test_allowed_events_lists_gate_options(evaluator: TransitionEvaluator) -> None:
    assert set(evaluator.allowed_events("spec_review")) == {
        "spec_approved_by_human",
        "spec_changes_requested",
    }


def test_pure_retry_guards(evaluator: TransitionEvaluator) -> None:
    failing = {"lint": True, "type_check": True, "tests": False, "coverage": True}
    # budget remaining -> loops back to executing
    d = evaluator.resolve(
        "verifying",
        "checks_failed",
        pure_guard_ctx=PureGuardContext(retry_count=1, max_retries=3, checks=failing),
    )
    assert d.to_state == WorkflowState.EXECUTING

    # budget exhausted -> needs_human_input
    d = evaluator.resolve(
        "verifying",
        "checks_failed",
        pure_guard_ctx=PureGuardContext(retry_count=3, max_retries=3, checks=failing),
    )
    assert d.to_state == WorkflowState.NEEDS_HUMAN_INPUT


def test_all_checks_passed_opens_pr(evaluator: TransitionEvaluator) -> None:
    passing = {"lint": True, "type_check": True, "tests": True, "coverage": True}
    d = evaluator.resolve(
        "verifying", "all_checks_passed", pure_guard_ctx=PureGuardContext(checks=passing)
    )
    assert d.to_state == WorkflowState.PR_OPENED
    assert d.effects == ["open_pr_with_spec_traceability"]


def test_merge_gate_guard_failure(evaluator: TransitionEvaluator) -> None:
    # ci not green + spec not validated -> GuardFailedError listing the unmet signals
    with pytest.raises(GuardFailedError) as ei:
        evaluator.resolve(
            "awaiting_review",
            "review_approved_by_human",
            pure_guard_ctx=PureGuardContext(
                merge_signals={
                    "review_approved_by_human": True,
                    "ci_status_green": False,
                    "spec_validated": False,
                }
            ),
        )
    assert "ci_status_green" in ei.value.unmet
    assert "spec_validated" in ei.value.unmet


def test_merge_gate_passes_with_all_signals(evaluator: TransitionEvaluator) -> None:
    d = evaluator.resolve(
        "awaiting_review",
        "review_approved_by_human",
        pure_guard_ctx=PureGuardContext(
            merge_signals={
                "review_approved_by_human": True,
                "ci_status_green": True,
                "spec_validated": True,
            }
        ),
    )
    assert d.to_state == WorkflowState.MERGED


def test_execute_preconditions_block(evaluator: TransitionEvaluator) -> None:
    with pytest.raises(PreconditionError) as ei:
        evaluator.resolve(
            "task_ready",
            "start_agent_run",
            pure_guard_ctx=PureGuardContext(
                preconditions={
                    "repo_target_set": True,
                    "policy_loaded": False,
                    "skill_profile_set": True,
                    "knowledge_synced": False,
                }
            ),
        )
    assert ei.value.unmet_preconditions == ["policy_loaded", "knowledge_synced"]


def test_execute_proceeds_when_preconditions_met(evaluator: TransitionEvaluator) -> None:
    d = evaluator.resolve(
        "task_ready",
        "start_agent_run",
        pure_guard_ctx=PureGuardContext(
            preconditions={
                "repo_target_set": True,
                "policy_loaded": True,
                "skill_profile_set": True,
                "knowledge_synced": True,
            }
        ),
    )
    assert d.to_state == WorkflowState.EXECUTING


def test_determinism_module_is_io_free() -> None:
    """The evaluator's module must import no clock/random/DB/network module."""
    from pathlib import Path

    import forge_workflow.temporal.determinism as mod

    source = mod.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    for forbidden in ("import random", "import os", "import time", "datetime", "sqlalchemy"):
        assert forbidden not in text, f"determinism core must not reference {forbidden!r}"
