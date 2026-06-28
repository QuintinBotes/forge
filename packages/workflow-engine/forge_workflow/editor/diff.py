"""Revision diffing for the workflow visual editor (F28 AC 15)."""

from __future__ import annotations

from forge_workflow.editor.graph import TransitionEdge, WorkflowGraph, edge_triggers
from forge_workflow.editor.schemas import DefinitionDiff, TransitionDiff


def _trigger_label(edge: TransitionEdge) -> str:
    if edge.action:
        return edge.action
    if isinstance(edge.when, str):
        return edge.when
    if isinstance(edge.when, list) and edge.when:
        return "+".join(edge.when)
    return ""


def _key(edge: TransitionEdge) -> tuple[str, str, frozenset[str]]:
    return (edge.from_state, edge.to_state, frozenset(edge_triggers(edge)))


def _comparable(edge: TransitionEdge) -> dict[str, object]:
    """Edge fields that matter for a semantic comparison (ignores UI ``id``)."""
    data = edge.model_dump(exclude={"id"})
    if isinstance(data.get("when"), list):
        data["when"] = sorted(data["when"])
    return data


def definition_diff(
    from_graph: WorkflowGraph,
    to_graph: WorkflowGraph,
    *,
    from_revision: int,
    to_revision: int,
) -> DefinitionDiff:
    """Compute the transition/state/policy diff between two graph revisions."""
    from_edges = {_key(e): e for e in from_graph.edges}
    to_edges = {_key(e): e for e in to_graph.edges}

    transition_diffs: list[TransitionDiff] = []
    for key, edge in to_edges.items():
        if key not in from_edges:
            transition_diffs.append(
                TransitionDiff(
                    change="added",
                    from_state=edge.from_state,
                    on=_trigger_label(edge),
                    to=edge.to_state,
                    after=edge,
                )
            )
        elif _comparable(from_edges[key]) != _comparable(edge):
            transition_diffs.append(
                TransitionDiff(
                    change="changed",
                    from_state=edge.from_state,
                    on=_trigger_label(edge),
                    to=edge.to_state,
                    before=from_edges[key],
                    after=edge,
                )
            )
    for key, edge in from_edges.items():
        if key not in to_edges:
            transition_diffs.append(
                TransitionDiff(
                    change="removed",
                    from_state=edge.from_state,
                    on=_trigger_label(edge),
                    to=edge.to_state,
                    before=edge,
                )
            )

    from_states = {n.id for n in from_graph.nodes}
    to_states = {n.id for n in to_graph.nodes}

    policy_changed = (
        from_graph.retry_policy != to_graph.retry_policy
        or from_graph.escalation_policy != to_graph.escalation_policy
    )

    return DefinitionDiff(
        name=to_graph.name,
        from_revision=from_revision,
        to_revision=to_revision,
        transition_diffs=transition_diffs,
        states_added=sorted(to_states - from_states),
        states_removed=sorted(from_states - to_states),
        policy_changed=policy_changed,
    )


__all__ = ["definition_diff"]
