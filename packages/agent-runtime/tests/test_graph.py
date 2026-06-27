"""Unit tests for the dependency-free StateGraph engine."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from forge_agent.graph import END, GraphError, StateGraph


@dataclass
class _Counter:
    value: int = 0
    log: list[str] | None = None


def test_linear_graph_runs_to_end() -> None:
    g: StateGraph[_Counter] = StateGraph()

    def inc(state: _Counter) -> _Counter:
        state.value += 1
        return state

    g.add_node("a", inc)
    g.add_node("b", inc)
    g.set_entry_point("a")
    g.add_edge("a", "b")
    g.add_edge("b", END)

    out = g.compile().invoke(_Counter())
    assert out.value == 2


def test_conditional_loop_terminates() -> None:
    g: StateGraph[_Counter] = StateGraph()

    def step(state: _Counter) -> _Counter:
        state.value += 1
        return state

    def route(state: _Counter) -> str:
        return "done" if state.value >= 3 else "loop"

    g.add_node("step", step)
    g.set_entry_point("step")
    g.add_conditional_edges("step", route, {"loop": "step", "done": END})

    out = g.compile().invoke(_Counter())
    assert out.value == 3


def test_compile_requires_entry_point() -> None:
    g: StateGraph[_Counter] = StateGraph()
    g.add_node("a", lambda s: s)
    with pytest.raises(GraphError):
        g.compile()


def test_unknown_edge_target_rejected() -> None:
    g: StateGraph[_Counter] = StateGraph()
    g.add_node("a", lambda s: s)
    g.set_entry_point("a")
    g.add_edge("a", "missing")
    with pytest.raises(GraphError):
        g.compile()


def test_runaway_loop_is_bounded() -> None:
    g: StateGraph[_Counter] = StateGraph()
    g.add_node("a", lambda s: s)
    g.set_entry_point("a")
    g.add_edge("a", "a")  # infinite loop on purpose
    compiled = g.compile()
    with pytest.raises(GraphError):
        compiled.invoke(_Counter(), max_steps=50)
