"""Diff classification tests (F28 AC 15)."""

from __future__ import annotations

from forge_workflow.editor.diff import definition_diff
from forge_workflow.editor.graph import yaml_to_graph

from .conftest import REVIEW_LINE


def test_v1_diff_has_no_line_fields(bundled_default_graph) -> None:
    diff = definition_diff(
        bundled_default_graph, bundled_default_graph, from_revision=1, to_revision=2
    )
    assert not {"stations_changed", "header_changed"} & diff.model_dump().keys()


def test_v2_diff_reports_station_and_header_changes() -> None:
    before = yaml_to_graph(REVIEW_LINE)
    same = definition_diff(before, before, from_revision=1, to_revision=2)
    assert (same.stations_changed, same.header_changed) == ([], False)

    after = before.model_copy(deep=True)
    checks = next(n for n in after.nodes if n.id == "checks")
    checks.station = {**checks.station, "timeout_seconds": 900}
    diff = definition_diff(before, after, from_revision=1, to_revision=2)
    assert (diff.stations_changed, diff.header_changed, diff.transition_diffs) == (
        ["checks"],
        False,
        [],
    )

    for key, value in [
        ("initial", "checks"),
        ("terminals", {"shipped": "succeeded", "parked": "failed"}),
        ("budget", {"max_tokens": 5}),
        ("concurrency", {"max_active_runs": 9}),
    ]:
        changed = before.model_copy(update={key: value})
        assert definition_diff(before, changed, from_revision=1, to_revision=2).header_changed, key


def _edge(graph, frm, to):
    for e in graph.edges:
        if e.from_state == frm and e.to_state == to:
            return e
    raise AssertionError(f"no edge {frm} -> {to}")


def test_unchanged_has_no_diffs(bundled_default_graph) -> None:
    diff = definition_diff(
        bundled_default_graph, bundled_default_graph, from_revision=1, to_revision=2
    )
    assert diff.transition_diffs == []
    assert not diff.policy_changed
    assert diff.states_added == []
    assert diff.states_removed == []


def test_added_and_removed_transition(bundled_default_graph) -> None:
    modified = bundled_default_graph.model_copy(deep=True)
    # remove the merged->closed edge
    modified.edges = [
        e for e in modified.edges if not (e.from_state == "merged" and e.to_state == "closed")
    ]
    diff = definition_diff(bundled_default_graph, modified, from_revision=1, to_revision=2)
    removed = [d for d in diff.transition_diffs if d.change == "removed"]
    assert any(d.from_state == "merged" and d.to == "closed" for d in removed)


def test_changed_transition(bundled_default_graph) -> None:
    modified = bundled_default_graph.model_copy(deep=True)
    edge = _edge(modified, "created", "spec_drafting")
    edge.skill = "different-skill"
    diff = definition_diff(bundled_default_graph, modified, from_revision=1, to_revision=2)
    changed = [d for d in diff.transition_diffs if d.change == "changed"]
    assert any(d.from_state == "created" and d.to == "spec_drafting" for d in changed)


def test_policy_changed(bundled_default_graph) -> None:
    modified = bundled_default_graph.model_copy(deep=True)
    modified.retry_policy = modified.retry_policy.model_copy(update={"max_retries": 9})
    diff = definition_diff(bundled_default_graph, modified, from_revision=1, to_revision=2)
    assert diff.policy_changed
