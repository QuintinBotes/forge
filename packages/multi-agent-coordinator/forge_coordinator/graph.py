"""The deterministic LangGraph supervisor graph (F27 §3.3).

``build_supervisor_graph`` wires the real :class:`forge_agent.StateGraph`
(LangGraph's Pregel runtime) with the coordinator's deterministic nodes:

    START -> select_pattern -> policy_gate -> dispatch
    dispatch  -(more ready)-> dispatch | merge | finalize(interrupt)
    merge     -> validate | finalize(interrupt: conflicts)
    validate  -> finalize -> END

NO node calls an LLM (AC 2): ``CoordinatorDeps`` carries an ``agent_factory`` (the
subagents) but never a ``model_factory``. ``build_resume_graph`` enters at
``dispatch`` so a resumed run continues from the supervision state without
re-selecting the pattern or re-running completed subagents.
"""

from __future__ import annotations

from forge_agent.graph import END, CompiledGraph, StateGraph
from forge_coordinator.deps import CoordinatorDeps
from forge_coordinator.nodes import (
    dispatch,
    finalize,
    merge_node,
    policy_gate_node,
    select_pattern,
    validate_node,
)
from forge_coordinator.routing import (
    router_after_dispatch,
    router_after_gate,
    router_after_merge,
)
from forge_coordinator.state import SupervisionState

__all__ = ["build_resume_graph", "build_supervisor_graph"]


def _wire(graph: StateGraph[SupervisionState], deps: CoordinatorDeps) -> None:
    graph.add_node("select_pattern", lambda s: select_pattern(s, deps))
    graph.add_node("policy_gate", lambda s: policy_gate_node(s, deps))
    graph.add_node("dispatch", lambda s: dispatch(s, deps))
    graph.add_node("merge", lambda s: merge_node(s, deps))
    graph.add_node("validate", lambda s: validate_node(s, deps))
    graph.add_node("finalize", lambda s: finalize(s, deps))

    graph.add_edge("select_pattern", "policy_gate")
    graph.add_conditional_edges(
        "policy_gate", router_after_gate, {"dispatch": "dispatch", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "dispatch",
        router_after_dispatch,
        {"dispatch": "dispatch", "merge": "merge", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "merge", router_after_merge, {"validate": "validate", "finalize": "finalize"}
    )
    graph.add_edge("validate", "finalize")
    graph.add_edge("finalize", END)


def build_supervisor_graph(deps: CoordinatorDeps) -> CompiledGraph[SupervisionState]:
    """Build the full supervisor graph (entry: ``select_pattern``)."""
    graph: StateGraph[SupervisionState] = StateGraph()
    _wire(graph, deps)
    graph.set_entry_point("select_pattern")
    return graph.compile()


def build_resume_graph(deps: CoordinatorDeps) -> CompiledGraph[SupervisionState]:
    """Build the resume graph (entry: ``dispatch``) for HITL continuation."""
    graph: StateGraph[SupervisionState] = StateGraph()
    _wire(graph, deps)
    graph.set_entry_point("dispatch")
    return graph.compile()
