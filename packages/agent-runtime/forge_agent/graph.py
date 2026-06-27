"""A thin ``StateGraph`` adapter over :mod:`langgraph`.

Forge's single-agent loop is expressed as a small state machine: typed nodes,
plain edges, conditional edges, a terminal sentinel, and a bounded ``invoke``.
This module provides exactly that surface, but the execution engine underneath
is the real :class:`langgraph.graph.StateGraph` (LangGraph's Pregel runtime) —
not a hand-rolled FSM. The public names (``StateGraph``, ``CompiledGraph``,
``END``, ``GraphError``) are preserved so the rest of ``forge_agent`` and its
tests are unchanged.

State is carried opaquely: the whole Forge state object lives in a single
LangGraph channel (``value``). Each node is a ``state -> state`` callable; the
adapter mutates that one channel per super-step (the Forge loop is strictly
sequential, so there is never more than one update per step). Construction-time
mistakes raise :class:`GraphError`; a runaway loop surfaces LangGraph's
``GraphRecursionError`` as :class:`GraphError` (the design's bounded-loops rule).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict, cast

from langgraph.errors import GraphRecursionError
from langgraph.graph import END as _LG_END
from langgraph.graph import StateGraph as _LangGraphStateGraph

__all__ = ["END", "CompiledGraph", "GraphError", "NodeFn", "RouterFn", "StateGraph"]

#: Terminal sentinel: routing to ``END`` stops execution. Equal to LangGraph's
#: own ``END`` ("__end__"), so the two are interchangeable.
END = _LG_END

type NodeFn[S] = Callable[[S], S]
type RouterFn[S] = Callable[[S], str]


class GraphError(Exception):
    """Raised for invalid graph construction or a runaway execution loop."""


class _Carrier(TypedDict):
    """The single LangGraph channel holding the opaque Forge state object."""

    value: Any


def _wrap_node[S](fn: NodeFn[S]) -> Callable[[_Carrier], _Carrier]:
    def node(carrier: _Carrier) -> _Carrier:
        return {"value": fn(cast("S", carrier["value"]))}

    return node


def _wrap_router[S](router: RouterFn[S]) -> Callable[[_Carrier], str]:
    def route(carrier: _Carrier) -> str:
        return router(cast("S", carrier["value"]))

    return route


class StateGraph[S]:
    """A tiny mutable-state graph builder backed by ``langgraph``.

    Nodes are callables ``state -> state``. Edges are either unconditional
    (``add_edge``) or conditional (``add_conditional_edges`` with a router that
    returns a key into a destination mapping). The builder mirrors the slice of
    the LangGraph builder API the Forge loop uses, while keeping deterministic
    :class:`GraphError` semantics for construction mistakes.
    """

    def __init__(self) -> None:
        # ``langgraph``'s ``StateGraph`` builder is generic over node/state
        # schemas whose overloads a dynamic ``str -> callable`` adapter cannot
        # satisfy; hold it as ``Any`` (the adapter's *public* surface stays
        # fully typed via the ``[S]`` parameter on this class).
        self._builder: Any = _LangGraphStateGraph(_Carrier)
        self._nodes: set[str] = set()
        self._edges: dict[str, str] = {}
        self._conditional: dict[str, dict[str, str]] = {}
        self._entry: str | None = None

    def add_node(self, name: str, fn: NodeFn[S]) -> None:
        if name == END:
            raise GraphError(f"'{END}' is reserved and cannot be a node name")
        if name in self._nodes:
            raise GraphError(f"duplicate node: {name}")
        self._nodes.add(name)
        self._builder.add_node(name, _wrap_node(fn))

    def set_entry_point(self, name: str) -> None:
        self._entry = name

    def add_edge(self, src: str, dst: str) -> None:
        if src in self._conditional:
            raise GraphError(f"node '{src}' already has conditional edges")
        self._edges[src] = dst
        self._builder.add_edge(src, dst)

    def add_conditional_edges(
        self, src: str, router: RouterFn[S], mapping: dict[str, str]
    ) -> None:
        if src in self._edges:
            raise GraphError(f"node '{src}' already has an unconditional edge")
        self._conditional[src] = dict(mapping)
        self._builder.add_conditional_edges(src, _wrap_router(router), dict(mapping))

    def compile(self) -> CompiledGraph[S]:
        if self._entry is None:
            raise GraphError("no entry point set")
        if self._entry not in self._nodes:
            raise GraphError(f"entry point '{self._entry}' is not a node")
        for src, dst in self._edges.items():
            if src not in self._nodes:
                raise GraphError(f"edge source '{src}' is not a node")
            if dst != END and dst not in self._nodes:
                raise GraphError(f"edge target '{dst}' is not a node")
        for src, mapping in self._conditional.items():
            if src not in self._nodes:
                raise GraphError(f"conditional source '{src}' is not a node")
            for dst in mapping.values():
                if dst != END and dst not in self._nodes:
                    raise GraphError(f"conditional target '{dst}' is not a node")
        self._builder.set_entry_point(self._entry)
        try:
            compiled = self._builder.compile()
        except ValueError as exc:  # pragma: no cover - guarded by checks above
            raise GraphError(str(exc)) from exc
        return CompiledGraph(compiled)


class CompiledGraph[S]:
    """An executable graph produced by :meth:`StateGraph.compile`."""

    def __init__(self, compiled: Any) -> None:
        self._compiled = compiled

    def invoke(self, state: S, *, max_steps: int = 10_000) -> S:
        """Run the graph from the entry node until ``END``.

        ``max_steps`` maps to LangGraph's ``recursion_limit`` and acts as the
        runaway backstop (the design's bounded-loops rule); exceeding it raises
        :class:`GraphError`.
        """
        try:
            result = self._compiled.invoke(
                {"value": state}, config={"recursion_limit": max_steps}
            )
        except GraphRecursionError as exc:
            raise GraphError(
                f"graph exceeded max_steps={max_steps} (runaway loop)"
            ) from exc
        return cast("S", result["value"])
