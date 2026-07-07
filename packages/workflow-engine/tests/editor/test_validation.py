"""Multi-issue validation + parity + protected-invariant tests (F28 AC 4-7)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from forge_workflow.dsl import load_definition
from forge_workflow.editor.catalog import Vocabulary
from forge_workflow.editor.graph import (
    NodeLayout,
    StateNode,
    TransitionEdge,
    WorkflowGraph,
    graph_to_yaml,
)
from forge_workflow.editor.validation import (
    FEATURE_INVARIANTS,
    IssueCode,
    Severity,
    collect_validation_issues,
    error_count,
)
from forge_workflow.exceptions import WorkflowDefinitionError


def _node(state: str) -> StateNode:
    return StateNode(id=state, layout=NodeLayout(x=0, y=0))


def _find_edge(graph: WorkflowGraph, frm: str, to: str) -> TransitionEdge:
    for edge in graph.edges:
        if edge.from_state == frm and edge.to_state == to:
            return edge
    raise AssertionError(f"no edge {frm} -> {to}")


def test_bundled_graph_has_no_errors(bundled_default_graph, vocabulary) -> None:
    """The unmodified bundled definition is publishable (zero ERRORs)."""
    issues = collect_validation_issues(
        bundled_default_graph,
        vocabulary=vocabulary,
        base_bundled_name="default_feature",
        invariants=FEATURE_INVARIANTS,
    )
    assert error_count(issues) == 0


def test_multi_issue_not_fail_fast(vocabulary: Vocabulary) -> None:
    """AC 4: four distinct problems yield four distinct issues in one pass."""
    graph = WorkflowGraph(
        name="custom",
        title="Custom",
        nodes=[_node("created"), _node("B"), _node("C")],
        edges=[
            TransitionEdge(
                id="e0", from_state="created", to_state="B", action="generate_spec_draft"
            ),
            TransitionEdge(
                id="e1", from_state="created", to_state="ghost", action="gather_clarifications"
            ),
            TransitionEdge(
                id="e2",
                from_state="B",
                to_state="C",
                action="submit_spec_for_review",
                condition="bogus_guard",
            ),
            TransitionEdge(id="e3", from_state="B", to_state="C", action="bogus_effect"),
        ],
    )
    issues = collect_validation_issues(graph, vocabulary=vocabulary)
    codes = {i.code for i in issues}
    assert IssueCode.UNKNOWN_STATE in codes
    assert IssueCode.UNREGISTERED_GUARD in codes
    assert IssueCode.UNREGISTERED_EFFECT in codes
    assert IssueCode.DEAD_END_STATE in codes
    assert error_count(issues) >= 4


def test_unregistered_precondition(vocabulary: Vocabulary, bundled_default_graph) -> None:
    graph = bundled_default_graph.model_copy(deep=True)
    edge = _find_edge(graph, "task_ready", "executing")
    edge.preconditions = [*edge.preconditions, "bogus_precondition"]
    issues = collect_validation_issues(graph, vocabulary=vocabulary)
    assert any(i.code is IssueCode.UNREGISTERED_PRECONDITION for i in issues)


def test_unknown_skill_is_warning(vocabulary: Vocabulary, bundled_default_graph) -> None:
    graph = bundled_default_graph.model_copy(deep=True)
    edge = _find_edge(graph, "created", "spec_drafting")
    edge.skill = "no-such-skill"
    issues = collect_validation_issues(graph, vocabulary=vocabulary, skill_names={"spec-analyst"})
    skill_issues = [i for i in issues if i.code is IssueCode.UNKNOWN_SKILL]
    assert len(skill_issues) == 1
    assert skill_issues[0].severity is Severity.WARNING


def test_duplicate_edge_error(vocabulary: Vocabulary) -> None:
    graph = WorkflowGraph(
        name="dup",
        title="Dup",
        nodes=[_node("created"), _node("closed")],
        edges=[
            TransitionEdge(id="e0", from_state="created", to_state="closed", action="close_task"),
            TransitionEdge(id="e1", from_state="created", to_state="closed", action="close_task"),
        ],
    )
    issues = collect_validation_issues(graph, vocabulary=vocabulary)
    assert any(i.code is IssueCode.DUPLICATE_EDGE for i in issues)


def test_nondeterministic_rules(vocabulary: Vocabulary) -> None:
    # Two edges from `created` share the trigger `checks_failed` with the same
    # (None) condition but distinct trigger sets, so they are not an exact
    # duplicate yet both would fire on `checks_failed`.
    graph = WorkflowGraph(
        name="nd",
        title="ND",
        nodes=[_node("created"), _node("a"), _node("closed")],
        edges=[
            TransitionEdge(
                id="e0",
                from_state="created",
                to_state="a",
                when=["checks_failed", "ci_status_green"],
            ),
            TransitionEdge(id="e1", from_state="created", to_state="closed", when="checks_failed"),
        ],
    )
    issues = collect_validation_issues(graph, vocabulary=vocabulary)
    assert any(i.code is IssueCode.NONDETERMINISTIC_RULES for i in issues)


def test_unreachable_state_warning(vocabulary: Vocabulary) -> None:
    # `x` and `y` form a cycle disconnected from the only initial (`created`),
    # so both have an inbound edge (not initial) yet are unreachable.
    graph = WorkflowGraph(
        name="ur",
        title="UR",
        nodes=[_node("created"), _node("x"), _node("y"), _node("closed")],
        edges=[
            TransitionEdge(id="e0", from_state="created", to_state="closed", action="close_task"),
            TransitionEdge(id="e1", from_state="x", to_state="y", action="run_checks"),
            TransitionEdge(id="e2", from_state="y", to_state="x", action="request_reviews"),
        ],
    )
    issues = collect_validation_issues(graph, vocabulary=vocabulary)
    warns = {i.node_id for i in issues if i.code is IssueCode.UNREACHABLE_STATE}
    assert {"x", "y"} <= warns


# --------------------------------------------------------------------------- #
# AC 5 — parity with the foundation loader                                     #
# --------------------------------------------------------------------------- #


def test_parity_valid_graph_loads(bundled_default_graph, vocabulary) -> None:
    issues = collect_validation_issues(bundled_default_graph, vocabulary=vocabulary)
    assert error_count(issues) == 0
    # zero ERRORs => load_definition accepts the canonical YAML.
    load_definition(graph_to_yaml(bundled_default_graph))


def test_parity_duplicate_both_reject(vocabulary: Vocabulary) -> None:
    graph = WorkflowGraph(
        name="dup",
        title="Dup",
        nodes=[_node("created"), _node("closed")],
        edges=[
            TransitionEdge(id="e0", from_state="created", to_state="closed", action="close_task"),
            TransitionEdge(id="e1", from_state="created", to_state="closed", action="close_task"),
        ],
    )
    assert error_count(collect_validation_issues(graph, vocabulary=vocabulary)) > 0
    with pytest.raises(WorkflowDefinitionError):
        load_definition(graph_to_yaml(graph))


def test_parity_no_transitions_both_reject(vocabulary: Vocabulary) -> None:
    graph = WorkflowGraph(name="empty", title="Empty", nodes=[], edges=[])
    assert error_count(collect_validation_issues(graph, vocabulary=vocabulary)) > 0
    with pytest.raises(WorkflowDefinitionError):
        load_definition(graph_to_yaml(graph))


# --------------------------------------------------------------------------- #
# AC 6 / AC 7 — protected invariants                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fork() -> Callable[[WorkflowGraph], WorkflowGraph]:
    def _clone(graph: WorkflowGraph) -> WorkflowGraph:
        return graph.model_copy(deep=True)

    return _clone


def _violations(graph, vocabulary):
    return [
        i
        for i in collect_validation_issues(
            graph,
            vocabulary=vocabulary,
            base_bundled_name="default_feature",
            invariants=FEATURE_INVARIANTS,
        )
        if i.code is IssueCode.PROTECTED_INVARIANT_VIOLATION
    ]


def test_merge_gate_signal_removed(bundled_default_graph, vocabulary, fork) -> None:
    """AC 6: removing the human-approval signal from the merge edge violates."""
    graph = fork(bundled_default_graph)
    edge = _find_edge(graph, "awaiting_review", "merged")
    edge.when = [s for s in edge.when if s != "review_approved_by_human"]
    violations = _violations(graph, vocabulary)
    assert any(v.invariant_id == "merge_human_gate" for v in violations)


def test_merge_gate_edge_deleted(bundled_default_graph, vocabulary, fork) -> None:
    """AC 6: deleting the merge edge violates the invariant."""
    graph = fork(bundled_default_graph)
    edge = _find_edge(graph, "awaiting_review", "merged")
    graph.edges = [e for e in graph.edges if e.id != edge.id]
    violations = _violations(graph, vocabulary)
    assert any(v.invariant_id == "merge_human_gate" for v in violations)


def test_spec_gate_signal_removed(bundled_default_graph, vocabulary, fork) -> None:
    """AC 7: removing the spec-approval signal violates the spec gate."""
    graph = fork(bundled_default_graph)
    edge = _find_edge(graph, "spec_review", "spec_approved")
    edge.when = None
    violations = _violations(graph, vocabulary)
    assert any(v.invariant_id == "spec_gate" for v in violations)


def test_invariants_skipped_for_custom_base(bundled_default_graph, vocabulary) -> None:
    """A custom (non-default_feature) definition is not bound by the invariants."""
    graph = bundled_default_graph.model_copy(deep=True)
    edge = _find_edge(graph, "awaiting_review", "merged")
    edge.when = [s for s in edge.when if s != "review_approved_by_human"]
    violations = _violations_for(graph, vocabulary, base=None)
    assert not violations


def _violations_for(graph, vocabulary, base):
    return [
        i
        for i in collect_validation_issues(
            graph,
            vocabulary=vocabulary,
            base_bundled_name=base,
            invariants=FEATURE_INVARIANTS,
        )
        if i.code is IssueCode.PROTECTED_INVARIANT_VIOLATION
    ]
