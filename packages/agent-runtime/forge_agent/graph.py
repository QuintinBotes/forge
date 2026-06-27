"""A minimal, dependency-free ``StateGraph`` engine.

This mirrors the small slice of the LangGraph ``StateGraph`` surface that the
Forge single-agent loop needs: typed nodes, plain edges, conditional edges, a
terminal sentinel, and a bounded ``invoke``. It deliberately avoids a hard
dependency on ``langgraph`` so the agent runtime stays installable in the locked
Phase-0 environment (``uv sync`` is frozen, and ``langgraph`` is not pinned).

# PARKED: swap this in-house engine for ``langgraph.graph.StateGraph`` once
# ``langgraph`` is added to the workspace lock. The node/edge wiring in
# ``forge_agent.runtime`` maps 1:1 onto the LangGraph builder API, so the swap is
# mechanical. See MORNING_REPORT for the parked dependency.
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["END", "CompiledGraph", "GraphError", "NodeFn", "RouterFn", "StateGraph"]

#: Terminal sentinel: routing to ``END`` stops execution.
END = "__end__"

type NodeFn[S] = Callable[[S], S]
type RouterFn[S] = Callable[[S], str]


class GraphError(Exception):
    """Raised for invalid graph construction or a runaway execution loop."""


class StateGraph[S]:
    """A tiny mutable-state graph builder.

    Nodes are callables ``state -> state``. Edges are either unconditional
    (``add_edge``) or conditional (``add_conditional_edges`` with a router that
    returns a key into a destination mapping).
    """

    def __init__(self) -> None:
        self._nodes: dict[str, NodeFn[S]] = {}
        self._edges: dict[str, str] = {}
        self._conditional: dict[str, tuple[RouterFn[S], dict[str, str]]] = {}
        self._entry: str | None = None

    def add_node(self, name: str, fn: NodeFn[S]) -> None:
        if name == END:
            raise GraphError(f"'{END}' is reserved and cannot be a node name")
        if name in self._nodes:
            raise GraphError(f"duplicate node: {name}")
        self._nodes[name] = fn

    def set_entry_point(self, name: str) -> None:
        self._entry = name

    def add_edge(self, src: str, dst: str) -> None:
        if src in self._conditional:
            raise GraphError(f"node '{src}' already has conditional edges")
        self._edges[src] = dst

    def add_conditional_edges(
        self, src: str, router: RouterFn[S], mapping: dict[str, str]
    ) -> None:
        if src in self._edges:
            raise GraphError(f"node '{src}' already has an unconditional edge")
        self._conditional[src] = (router, dict(mapping))

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
        for src, (_router, mapping) in self._conditional.items():
            if src not in self._nodes:
                raise GraphError(f"conditional source '{src}' is not a node")
            for dst in mapping.values():
                if dst != END and dst not in self._nodes:
                    raise GraphError(f"conditional target '{dst}' is not a node")
        return CompiledGraph(
            nodes=dict(self._nodes),
            edges=dict(self._edges),
            conditional=dict(self._conditional),
            entry=self._entry,
        )


class CompiledGraph[S]:
    """An executable graph produced by :meth:`StateGraph.compile`."""

    def __init__(
        self,
        *,
        nodes: dict[str, NodeFn[S]],
        edges: dict[str, str],
        conditional: dict[str, tuple[RouterFn[S], dict[str, str]]],
        entry: str,
    ) -> None:
        self._nodes = nodes
        self._edges = edges
        self._conditional = conditional
        self._entry = entry

    def invoke(self, state: S, *, max_steps: int = 10_000) -> S:
        """Run the graph from the entry node until ``END``.

        ``max_steps`` is a runaway backstop (the design's bounded-loops rule);
        exceeding it raises :class:`GraphError`.
        """
        current = self._entry
        steps = 0
        while current != END:
            steps += 1
            if steps > max_steps:
                raise GraphError(f"graph exceeded max_steps={max_steps} (runaway loop)")
            state = self._nodes[current](state)
            current = self._next(current, state)
        return state

    def _next(self, current: str, state: S) -> str:
        if current in self._conditional:
            router, mapping = self._conditional[current]
            key = router(state)
            if key not in mapping:
                raise GraphError(f"router for '{current}' returned unmapped key: {key!r}")
            return mapping[key]
        return self._edges.get(current, END)
