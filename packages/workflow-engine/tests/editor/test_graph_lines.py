"""DSL v2 lines in the editor graph (spec K §4.3), and the v1 byte-identity guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from forge_contracts import parse_line
from forge_workflow.default_workflow import default_feature_definition
from forge_workflow.editor.graph import (
    WorkflowGraph,
    definition_to_graph,
    graph_to_yaml,
    line_graph_from_yaml,
    yaml_to_graph,
)
from forge_workflow.exceptions import WorkflowDefinitionError
from forge_workflow.incident.definition import default_incident_definition

from .conftest import IMPLEMENTATION_LOOP, REVIEW_LINE

GOLDEN = Path(__file__).parent / "golden"


@pytest.mark.parametrize(
    ("name", "defn", "title"),
    [
        ("default_feature", default_feature_definition(), "Default Feature"),
        ("incident", default_incident_definition(), "Incident"),
    ],
)
def test_v1_bundled_graph_and_yaml_are_byte_identical(name, defn, title) -> None:
    """The golden files were written from the editor before DSL v2 support."""
    graph = definition_to_graph(defn, title=title)
    dumped = json.dumps(graph.model_dump(mode="json"), indent=2) + "\n"
    assert dumped == (GOLDEN / f"{name}.graph.json").read_text()
    assert graph_to_yaml(graph) == (GOLDEN / f"{name}.yaml").read_text()


@pytest.mark.parametrize("text", [IMPLEMENTATION_LOOP, REVIEW_LINE], ids=["impl_loop", "review"])
def test_v2_yaml_round_trips_without_loss(text: str) -> None:
    graph = yaml_to_graph(text)
    out = graph_to_yaml(graph)
    assert parse_line(yaml.safe_load(out)) == parse_line(yaml.safe_load(text))
    # And the graph is a fixed point, through its JSON form too.
    assert yaml_to_graph(out) == graph
    assert WorkflowGraph.model_validate(graph.model_dump(mode="json")) == graph


def test_v2_node_kinds_and_stations_come_from_the_line() -> None:
    graph = yaml_to_graph(REVIEW_LINE)
    nodes = {n.id: n for n in graph.nodes}
    assert {s: n.kind for s, n in nodes.items()} == {
        "draft": "agent",
        "checks": "service",
        "review": "human",
        "shipped": "terminal",
        "failed": "terminal",
    }
    assert graph.nodes[0].id == "draft"  # the initial state leads
    assert nodes["review"].station == {
        "kind": "human",
        "gate": "pr",
        "sla_seconds": 3600,
        "required_approvals": 2,
        "summary": "Hi {{task.key}}",
    }
    assert nodes["shipped"].station is None
    assert (graph.dsl_version, graph.initial, graph.terminals) == (
        "2",
        "draft",
        {"shipped": "succeeded"},
    )
    assert [e.id for e in graph.edges] == [f"e{i}" for i in range(6)]


def test_v2_node_order_ignores_mapping_order() -> None:
    reordered = yaml.safe_load(REVIEW_LINE)
    reordered["stations"] = dict(reversed(list(reordered["stations"].items())))
    assert yaml_to_graph(yaml.safe_dump(reordered)) == yaml_to_graph(REVIEW_LINE)


def test_v2_malformed_line_raises_definition_error() -> None:
    bad = yaml.safe_load(REVIEW_LINE)
    del bad["stations"]["review"]["sla_seconds"]
    with pytest.raises(WorkflowDefinitionError, match="invalid line definition"):
        yaml_to_graph(yaml.safe_dump(bad))


def test_line_graph_from_yaml_for_the_installer() -> None:
    graph_json, normalized = line_graph_from_yaml(IMPLEMENTATION_LOOP, title="Implementation loop")
    graph = WorkflowGraph.model_validate(graph_json)
    assert graph.title == "Implementation loop"
    assert graph.dsl_version == "2"
    # auto_layout placed every node, and the initial state is in the first layer.
    assert len({(n.layout.x, n.layout.y) for n in graph.nodes}) == len(graph.nodes)
    assert next(n for n in graph.nodes if n.id == "prepare").layout.x == 0
    assert parse_line(yaml.safe_load(normalized)) == parse_line(yaml.safe_load(IMPLEMENTATION_LOOP))


def test_line_graph_from_yaml_refuses_v1() -> None:
    with pytest.raises(WorkflowDefinitionError, match="dsl_version '2'"):
        line_graph_from_yaml((GOLDEN / "default_feature.yaml").read_text())


V1_TINY = "workflow: tiny\nversion: '1'\ntransitions:\n  - {from: created, to: closed}\n"


@pytest.mark.parametrize("value", [None, "1", 1], ids=["null", "str1", "int1"])
def test_yaml_to_graph_reads_v1_dsl_versions_as_v1(value) -> None:
    raw = yaml.safe_load(V1_TINY)
    raw["dsl_version"] = value
    assert yaml_to_graph(yaml.safe_dump(raw)).dsl_version is None


@pytest.mark.parametrize("value", [2, "3", True, 1.0], ids=["int2", "str3", "true", "float1"])
def test_yaml_to_graph_refuses_other_dsl_versions(value) -> None:
    """rp1ce repro_intver2: an unquoted ``dsl_version: 2`` is not silently a v1 graph."""
    raw = yaml.safe_load(REVIEW_LINE)
    raw["dsl_version"] = value
    with pytest.raises(WorkflowDefinitionError, match="unsupported dsl_version"):
        yaml_to_graph(yaml.safe_dump(raw))


@pytest.mark.parametrize("key", ["stations", "terminals", "budget", "initial"])
def test_yaml_to_graph_refuses_v2_keys_on_a_v1_document(key: str) -> None:
    raw = yaml.safe_load(V1_TINY)
    raw[key] = yaml.safe_load(REVIEW_LINE)[key]
    with pytest.raises(WorkflowDefinitionError, match=f"cannot carry {key}"):
        yaml_to_graph(yaml.safe_dump(raw))
