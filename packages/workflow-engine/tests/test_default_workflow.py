"""Tests for the built-in default feature workflow (plan Task 1.8).

Pins that the shipped default matches the spec's "Default Feature Workflow
States" and "Workflow DSL" sections, and that every state it references is a
valid ``WorkflowState`` enum member (so engine transitions are typed).
"""

from __future__ import annotations

from itertools import pairwise

from forge_contracts import WorkflowState
from forge_workflow.default_workflow import (
    DEFAULT_WORKFLOW_NAME,
    default_feature_definition,
)
from forge_workflow.fsm import TransitionGraph

# The spec's documented happy-path chain.
SPEC_HAPPY_PATH = [
    "created",
    "spec_drafting",
    "clarification",
    "spec_review",
    "spec_approved",
    "plan_drafting",
    "plan_review",
    "task_generation",
    "task_ready",
    "executing",
    "verifying",
    "pr_opened",
    "awaiting_review",
    "merged",
    "closed",
]


def test_default_name() -> None:
    assert default_feature_definition().name == DEFAULT_WORKFLOW_NAME == "default_feature"


def test_every_state_is_a_workflow_state_enum() -> None:
    graph = TransitionGraph.from_definition(default_feature_definition())
    valid = {s.value for s in WorkflowState}
    for state in graph.states:
        assert state in valid, f"{state} is not a WorkflowState member"


def test_happy_path_states_are_all_present() -> None:
    graph = TransitionGraph.from_definition(default_feature_definition())
    for state in SPEC_HAPPY_PATH:
        assert state in graph.states


def test_happy_path_chain_is_walkable() -> None:
    graph = TransitionGraph.from_definition(default_feature_definition())
    # Every adjacent pair in the spec chain has a connecting transition.
    for src, dst in pairwise(SPEC_HAPPY_PATH):
        targets = {t.to_state for t in graph.transitions_from(src)}
        assert dst in targets, f"no transition {src} -> {dst}"


def test_error_paths_present() -> None:
    graph = TransitionGraph.from_definition(default_feature_definition())
    assert "needs_human_input" in graph.states
