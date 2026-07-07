"""Graph model + DSL/YAML round-trip for the workflow visual editor (F28).

States become :class:`StateNode`s, transitions become :class:`TransitionEdge`s.
The edge shape conforms to the **foundation** DSL
(:class:`forge_contracts.WorkflowTransition`: ``action``/``when``/``condition``/
``preconditions``/``checks``/``record``/``skill`` with ``from``/``to``), *not* the
idealized ``on``/``guards``/``effects``/``priority`` shape from the slice doc —
see this slice's notes for the deviation.

Round-trip guarantees (AC 1):

* ``graph_to_definition(definition_to_graph(defn)) == defn`` (semantic equality),
* ``yaml_to_graph(graph_to_yaml(graph)) == graph`` (fixed point — layout is
  deterministic via :func:`auto_layout` and edge ids are positional, so the
  parsed-back graph reproduces the original exactly).
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from forge_contracts import (
    EscalationPolicy,
    RetryPolicy,
    WorkflowDefinition,
    WorkflowTransition,
)
from forge_workflow.exceptions import WorkflowDefinitionError

#: States that complete a run (no forward transition). Matches both bundled
#: definitions (``default_feature`` + ``incident``).
TERMINAL_STATES: frozenset[str] = frozenset({"closed", "failed", "cancelled"})

#: States that block on a human decision, badged in the canvas. Union of the
#: ``default_feature`` and ``incident`` human gates (the foundation exports
#: ``HUMAN_GATE_EVENTS`` only for the feature engine; F28 derives node kinds from
#: this state set — see slice notes).
HUMAN_GATE_STATES: frozenset[str] = frozenset(
    {
        "spec_review",
        "plan_review",
        "awaiting_review",
        "needs_human_input",
        "awaiting_approval",
    }
)

#: Conventional initial states (have no inbound edge in the bundled graphs).
_INITIAL_STATES: frozenset[str] = frozenset({"created", "alert_received"})


class NodeLayout(BaseModel):
    """Canvas position of a node (UI-only; persisted in ``graph_json``)."""

    x: float
    y: float


class StateNode(BaseModel):
    """A workflow state rendered as a graph node."""

    id: str
    label: str | None = None
    kind: Literal["normal", "initial", "terminal", "human_gate"] = "normal"
    layout: NodeLayout


class TransitionEdge(BaseModel):
    """A workflow transition rendered as a graph edge.

    Field shape mirrors :class:`forge_contracts.WorkflowTransition`. ``id`` is a
    stable, positional client id (``e0``, ``e1`` …) — deterministic so round-trips
    are fixed points; it is not persisted into the DSL.
    """

    id: str
    from_state: str
    to_state: str
    action: str | None = None
    when: str | list[str] | None = None
    condition: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    record: str | None = None
    skill: str | None = None


class WorkflowGraph(BaseModel):
    """The full editable graph: metadata + nodes + edges (+ layout)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    version: str = "1"
    title: str
    description: str | None = None
    modes: dict[str, Any] = Field(default_factory=lambda: {"default": "single_agent"})
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    escalation_policy: EscalationPolicy = Field(default_factory=EscalationPolicy)
    nodes: list[StateNode] = Field(default_factory=list)
    edges: list[TransitionEdge] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Triggers / kind derivation                                                   #
# --------------------------------------------------------------------------- #


def edge_triggers(edge: TransitionEdge) -> set[str]:
    """Every event token that can trigger ``edge`` (``action`` + ``when``)."""
    tokens: set[str] = set()
    if edge.action:
        tokens.add(edge.action)
    if isinstance(edge.when, str):
        tokens.add(edge.when)
    elif isinstance(edge.when, list):
        tokens.update(edge.when)
    return tokens


def derive_kind(
    state: str, *, inbound: int
) -> Literal["normal", "initial", "terminal", "human_gate"]:
    """Derive a node kind from membership + inbound-edge count."""
    if state in TERMINAL_STATES:
        return "terminal"
    if state in HUMAN_GATE_STATES:
        return "human_gate"
    if inbound == 0 or state in _INITIAL_STATES:
        return "initial"
    return "normal"


# --------------------------------------------------------------------------- #
# Layout                                                                        #
# --------------------------------------------------------------------------- #


def auto_layout(nodes: list[StateNode], edges: list[TransitionEdge]) -> dict[str, NodeLayout]:
    """Deterministic layered layout (server-side fallback).

    Layers are assigned by longest forward distance from an initial state; the
    same input always yields the same output.
    """
    ids = [n.id for n in nodes]
    id_set = set(ids)
    adjacency: dict[str, list[str]] = {i: [] for i in ids}
    indegree: dict[str, int] = dict.fromkeys(ids, 0)
    for e in edges:
        if e.from_state in id_set and e.to_state in id_set and e.from_state != e.to_state:
            adjacency[e.from_state].append(e.to_state)
            indegree[e.to_state] += 1

    layer: dict[str, int] = dict.fromkeys(ids, 0)
    # Longest-path layering over a deterministic order; tolerant of cycles by
    # capping iterations at the node count.
    for _ in range(len(ids)):
        changed = False
        for src in ids:
            for dst in adjacency[src]:
                if layer[dst] < layer[src] + 1:
                    layer[dst] = layer[src] + 1
                    changed = True
        if not changed:
            break

    by_layer: dict[int, list[str]] = {}
    for i in ids:
        by_layer.setdefault(layer[i], []).append(i)

    positions: dict[str, NodeLayout] = {}
    x_gap, y_gap = 240.0, 120.0
    for lvl in sorted(by_layer):
        for row, state in enumerate(sorted(by_layer[lvl])):
            positions[state] = NodeLayout(x=lvl * x_gap, y=row * y_gap)
    return positions


# --------------------------------------------------------------------------- #
# Definition <-> graph                                                          #
# --------------------------------------------------------------------------- #


def _ordered_states(transitions: list[WorkflowTransition]) -> list[str]:
    """Distinct states in first-appearance order (from before to)."""
    seen: list[str] = []
    for t in transitions:
        for state in (t.from_state, t.to_state):
            if state and state not in seen:
                seen.append(state)
    return seen


def definition_to_graph(
    defn: WorkflowDefinition,
    *,
    title: str,
    description: str | None = None,
    layout: dict[str, NodeLayout] | None = None,
) -> WorkflowGraph:
    """Build a :class:`WorkflowGraph` from a parsed definition.

    One :class:`StateNode` per distinct state, one :class:`TransitionEdge` per
    rule (positional ids). Node kinds are derived; positions come from ``layout``
    or :func:`auto_layout`.
    """
    edges: list[TransitionEdge] = []
    for index, t in enumerate(defn.transitions):
        edges.append(
            TransitionEdge(
                id=f"e{index}",
                from_state=t.from_state,
                to_state=t.to_state,
                action=t.action,
                when=t.when,
                condition=t.condition,
                preconditions=list(t.preconditions),
                checks=list(t.checks),
                record=t.record,
                skill=t.skill,
            )
        )

    states = _ordered_states(defn.transitions)
    inbound: dict[str, int] = dict.fromkeys(states, 0)
    for t in defn.transitions:
        if t.to_state != t.from_state:
            inbound[t.to_state] = inbound.get(t.to_state, 0) + 1

    placeholder = [
        StateNode(id=s, kind=derive_kind(s, inbound=inbound[s]), layout=NodeLayout(x=0, y=0))
        for s in states
    ]
    positions = layout or auto_layout(placeholder, edges)

    nodes = [
        StateNode(
            id=s,
            kind=derive_kind(s, inbound=inbound[s]),
            layout=positions.get(s, NodeLayout(x=0, y=0)),
        )
        for s in states
    ]

    return WorkflowGraph(
        name=defn.name,
        version=defn.version,
        title=title,
        description=description,
        modes=dict(defn.modes) or {"default": "single_agent"},
        retry_policy=defn.retry_policy,
        escalation_policy=defn.escalation_policy,
        nodes=nodes,
        edges=edges,
    )


def graph_to_definition(graph: WorkflowGraph) -> WorkflowDefinition:
    """Map a graph back to a :class:`WorkflowDefinition` (pure; structural only).

    Layout and edge ids (UI-only) are dropped.
    """
    transitions = [
        WorkflowTransition.model_validate(
            {
                "from": e.from_state,
                "to": e.to_state,
                "action": e.action,
                "when": e.when,
                "condition": e.condition,
                "preconditions": list(e.preconditions),
                "checks": list(e.checks),
                "record": e.record,
                "skill": e.skill,
            }
        )
        for e in graph.edges
    ]
    return WorkflowDefinition(
        name=graph.name,
        version=graph.version,
        modes=dict(graph.modes),
        transitions=transitions,
        retry_policy=graph.retry_policy,
        escalation_policy=graph.escalation_policy,
    )


# --------------------------------------------------------------------------- #
# YAML <-> graph                                                                #
# --------------------------------------------------------------------------- #


def _transition_to_dict(t: WorkflowTransition) -> dict[str, Any]:
    """Canonical, minimal DSL mapping for a transition (omits empty fields)."""
    out: dict[str, Any] = {"from": t.from_state, "to": t.to_state}
    if t.action is not None:
        out["action"] = t.action
    if t.when is not None:
        out["when"] = t.when
    if t.condition is not None:
        out["condition"] = t.condition
    if t.preconditions:
        out["preconditions"] = list(t.preconditions)
    if t.checks:
        out["checks"] = list(t.checks)
    if t.record is not None:
        out["record"] = t.record
    if t.skill is not None:
        out["skill"] = t.skill
    return out


def graph_to_yaml(graph: WorkflowGraph) -> str:
    """Serialize a graph to canonical DSL YAML (round-trips with :func:`yaml_to_graph`)."""
    defn = graph_to_definition(graph)
    document: dict[str, Any] = {
        "workflow": defn.name,
        "version": defn.version,
    }
    if defn.modes:
        document["modes"] = dict(defn.modes)
    document["transitions"] = [_transition_to_dict(t) for t in defn.transitions]
    document["retry_policy"] = defn.retry_policy.model_dump()
    document["escalation_policy"] = defn.escalation_policy.model_dump()
    return yaml.safe_dump(document, sort_keys=True, default_flow_style=False)


def yaml_to_graph(yaml_text: str, *, title: str | None = None) -> WorkflowGraph:
    """Structural YAML parse to a graph (no registry/name validation).

    Raises :class:`WorkflowDefinitionError` on malformed YAML / shape.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise WorkflowDefinitionError(f"invalid workflow YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkflowDefinitionError("workflow definition must be a mapping")

    data: dict[str, Any] = dict(raw)
    if "name" not in data and "workflow" in data:
        data["name"] = data["workflow"]
    try:
        defn = WorkflowDefinition.model_validate(data)
    except Exception as exc:
        raise WorkflowDefinitionError(f"invalid workflow definition: {exc}") from exc

    resolved_title = title or data.get("title") or defn.name
    return definition_to_graph(defn, title=resolved_title)


__all__ = [
    "HUMAN_GATE_STATES",
    "TERMINAL_STATES",
    "NodeLayout",
    "StateNode",
    "TransitionEdge",
    "WorkflowGraph",
    "auto_layout",
    "definition_to_graph",
    "derive_kind",
    "edge_triggers",
    "graph_to_definition",
    "graph_to_yaml",
    "yaml_to_graph",
]
