"""Pure-unit tests for the IO-free DSL transition evaluator (F25 AC2/5/7/8/12)."""

from __future__ import annotations

import pytest

from forge_contracts import WorkflowDefinition, WorkflowState
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


# -- spec K P2: the evaluator seam for station runs ------------------------- #

_LINE = WorkflowDefinition.model_validate(
    {
        "name": "triage_line",
        "transitions": [
            {"from": "triage", "to": "fix", "when": "succeeded", "condition": "triage.needs_fix"},
            {"from": "triage", "to": "done", "when": "succeeded"},
        ],
    }
)


def test_state_cast_str_routes_a_line_state() -> None:
    d = TransitionEvaluator(_LINE, state_cast=str).resolve(
        "triage",
        "succeeded",
        pure_guard_ctx=PureGuardContext(station_flags={"triage.needs_fix": True}),
    )
    assert d.to_state == "fix"
    assert type(d.to_state) is str
    assert d.guard_results == {"triage.needs_fix": True}


def test_station_flag_guard_falls_through_when_false() -> None:
    evaluator = TransitionEvaluator(_LINE, state_cast=str)
    off = PureGuardContext(station_flags={"triage.needs_fix": False})
    assert evaluator.resolve("triage", "succeeded", pure_guard_ctx=off).to_state == "done"
    # No flags at all: an unset station flag is false.
    assert evaluator.resolve("triage", "succeeded").to_state == "done"


def test_default_state_cast_is_unchanged() -> None:
    with pytest.raises(ValueError):  # a line state is not a WorkflowState
        TransitionEvaluator(_LINE).resolve("triage", "succeeded")
    d = TransitionEvaluator(default_feature_definition()).resolve("created", "generate_spec_draft")
    assert type(d.to_state) is WorkflowState
    assert PureGuardContext().as_flags() == {
        "all_checks_passed": False,
        "checks_failed": False,
        "low_confidence": False,
    }


@pytest.mark.parametrize("key", ["all_checks_passed", "ci_status_green", "triage.", ".needs_fix"])
def test_a_station_flag_must_name_its_state(key: str) -> None:
    """A bare key would overwrite a feature flag in ``as_flags`` (e.g. pass the merge guard)."""
    with pytest.raises(ValueError, match=r"<state>\.<key>"):
        PureGuardContext(
            checks={"lint": False},
            merge_signals={"ci_status_green": False},
            station_flags={key: True},
        )


def test_a_station_flag_never_shadows_a_feature_flag() -> None:
    flags = PureGuardContext(
        checks={"lint": False},
        merge_signals={"ci_status_green": False},
        station_flags={"triage.all_checks_passed": True},
    ).as_flags()

    assert (flags["all_checks_passed"], flags["ci_status_green"]) == (False, False)
    assert flags["triage.all_checks_passed"] is True
