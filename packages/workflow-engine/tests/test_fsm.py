"""Tests for the validated FSM transition graph (plan Task 1.8).

The ``TransitionGraph`` is the pure, storage-free core of the engine: it knows
which transitions exist, evaluates guard conditions (including the built-in
retry-budget guards), and resolves a (state, event) pair to a single eligible
transition.
"""

from __future__ import annotations

import pytest

from forge_contracts import WorkflowDefinition, WorkflowTransition
from forge_workflow.default_workflow import default_feature_definition
from forge_workflow.exceptions import (
    AmbiguousTransitionError,
    InvalidTransitionError,
    WorkflowDefinitionError,
)
from forge_workflow.fsm import (
    RETRY_BUDGET_EXHAUSTED,
    RETRY_BUDGET_REMAINING,
    TransitionGraph,
    evaluate_guard,
)


def _graph() -> TransitionGraph:
    return TransitionGraph.from_definition(default_feature_definition())


def test_states_collects_every_from_and_to() -> None:
    graph = _graph()
    expected = ("created", "spec_drafting", "executing", "verifying", "merged", "needs_human_input")
    for state in expected:
        assert state in graph.states


def test_initial_state_is_created() -> None:
    assert _graph().initial_state == "created"


def test_transitions_from_returns_outgoing_edges() -> None:
    graph = _graph()
    outgoing = graph.transitions_from("verifying")
    targets = {t.to_state for t in outgoing}
    assert {"pr_opened", "executing", "needs_human_input"} <= targets


def test_find_matches_on_action() -> None:
    graph = _graph()
    t = graph.find("created", "generate_spec_draft", context={}, retry_count=0, max_retries=3)
    assert t.to_state == "spec_drafting"


def test_find_matches_on_when_string() -> None:
    graph = _graph()
    t = graph.find(
        "spec_review", "spec_approved_by_human", context={}, retry_count=0, max_retries=3
    )
    assert t.to_state == "spec_approved"


def test_find_unknown_event_raises_invalid_transition() -> None:
    graph = _graph()
    with pytest.raises(InvalidTransitionError):
        graph.find("created", "nonsense_event", context={}, retry_count=0, max_retries=3)


def test_retry_budget_guards_disambiguate() -> None:
    graph = _graph()
    # Budget remaining -> loop back to executing.
    remaining = graph.find("verifying", "checks_failed", context={}, retry_count=0, max_retries=3)
    assert remaining.to_state == "executing"
    # Budget exhausted -> escalate to a human.
    exhausted = graph.find("verifying", "checks_failed", context={}, retry_count=3, max_retries=3)
    assert exhausted.to_state == "needs_human_input"


def test_list_when_requires_all_conditions() -> None:
    graph = _graph()
    # Only the triggering event is satisfied; the others are missing -> no transition.
    with pytest.raises(InvalidTransitionError):
        graph.find(
            "awaiting_review",
            "review_approved_by_human",
            context={},
            retry_count=0,
            max_retries=3,
        )
    # With the remaining conditions present in context, the transition fires.
    t = graph.find(
        "awaiting_review",
        "review_approved_by_human",
        context={"ci_status_green": True, "spec_validated": True},
        retry_count=0,
        max_retries=3,
    )
    assert t.to_state == "merged"


def test_evaluate_guard_builtin_retry() -> None:
    assert evaluate_guard(RETRY_BUDGET_REMAINING, context={}, retry_count=0, max_retries=3) is True
    assert evaluate_guard(RETRY_BUDGET_REMAINING, context={}, retry_count=3, max_retries=3) is False
    assert evaluate_guard(RETRY_BUDGET_EXHAUSTED, context={}, retry_count=3, max_retries=3) is True
    assert evaluate_guard(RETRY_BUDGET_EXHAUSTED, context={}, retry_count=1, max_retries=3) is False


def test_evaluate_guard_context_flag() -> None:
    assert (
        evaluate_guard(
            "ci_status_green", context={"ci_status_green": True}, retry_count=0, max_retries=3
        )
        is True
    )
    assert evaluate_guard("ci_status_green", context={}, retry_count=0, max_retries=3) is False


def test_empty_definition_fails_validation() -> None:
    with pytest.raises(WorkflowDefinitionError):
        TransitionGraph.from_definition(WorkflowDefinition(name="empty", transitions=[]))


def test_duplicate_transition_fails_validation() -> None:
    dup = WorkflowDefinition(
        name="dup",
        transitions=[
            WorkflowTransition(**{"from": "a", "to": "b", "action": "go"}),
            WorkflowTransition(**{"from": "a", "to": "c", "action": "go"}),
        ],
    )
    with pytest.raises(WorkflowDefinitionError):
        TransitionGraph.from_definition(dup)


def test_ambiguous_runtime_match_raises() -> None:
    # Two transitions from the same state with the same event and no
    # distinguishing condition -> ambiguous at resolution time. Such a graph is
    # rejected at validation, so build it bypassing validation to test find().
    definition = WorkflowDefinition(
        name="amb",
        transitions=[
            WorkflowTransition(**{"from": "a", "to": "b", "when": "ev", "condition": "x"}),
            WorkflowTransition(**{"from": "a", "to": "c", "when": "ev", "condition": "y"}),
        ],
    )
    graph = TransitionGraph(definition=definition)  # no validation
    with pytest.raises(AmbiguousTransitionError):
        graph.find("a", "ev", context={"x": True, "y": True}, retry_count=0, max_retries=3)
