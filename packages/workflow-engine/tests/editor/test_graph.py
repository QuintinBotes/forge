"""Graph round-trip + node-kind tests (F28 AC 1, AC 2)."""

from __future__ import annotations

import pytest

from forge_workflow.default_workflow import default_feature_definition
from forge_workflow.editor.graph import (
    auto_layout,
    definition_to_graph,
    graph_to_definition,
    graph_to_yaml,
    yaml_to_graph,
)
from forge_workflow.incident.definition import default_incident_definition


@pytest.fixture
def default_graph():
    return definition_to_graph(default_feature_definition(), title="Default Feature")


def test_definition_graph_round_trip_default() -> None:
    """AC 1: definition -> graph -> definition reproduces the definition."""
    defn = default_feature_definition()
    graph = definition_to_graph(defn, title="Default Feature")
    assert graph_to_definition(graph) == defn


def test_definition_graph_round_trip_incident() -> None:
    """AC 1: round-trip holds for the incident definition too."""
    defn = default_incident_definition()
    graph = definition_to_graph(defn, title="Incident")
    assert graph_to_definition(graph) == defn


def test_yaml_graph_fixed_point_default(default_graph) -> None:
    """AC 1: graph -> yaml -> graph is a fixed point."""
    reparsed = yaml_to_graph(graph_to_yaml(default_graph), title="Default Feature")
    assert reparsed == default_graph


def test_yaml_graph_fixed_point_incident() -> None:
    """AC 1: yaml fixed point holds for incident."""
    graph = definition_to_graph(default_incident_definition(), title="Incident")
    reparsed = yaml_to_graph(graph_to_yaml(graph), title="Incident")
    assert reparsed == graph


def test_node_kinds_default(default_graph) -> None:
    """AC 2: created=initial; terminals; human gates; others normal."""
    kinds = {n.id: n.kind for n in default_graph.nodes}
    assert kinds["created"] == "initial"
    for terminal in ("closed",):
        assert kinds[terminal] == "terminal"
    for gate in ("spec_review", "plan_review", "awaiting_review", "needs_human_input"):
        assert kinds[gate] == "human_gate"
    assert kinds["executing"] == "normal"


def test_auto_layout_deterministic(default_graph) -> None:
    """auto_layout is deterministic: same input -> same output."""
    first = auto_layout(default_graph.nodes, default_graph.edges)
    second = auto_layout(default_graph.nodes, default_graph.edges)
    assert first == second
    assert set(first) == {n.id for n in default_graph.nodes}
