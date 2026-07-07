"""Task dependency graph + cycle detection (plan Task 1.5).

The graph is a plain adjacency mapping ``edges[a] = {b, ...}`` where an edge
``a -> b`` means *"task ``a`` depends on task ``b``"* (``a`` is blocked-by ``b``).
A dependency set is valid iff it is acyclic; ``BoardService.dependency_add`` uses
:func:`would_create_cycle` to reject edges that would close a cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

from forge_contracts import CycleError

Edges = Mapping[uuid.UUID, "Iterable[uuid.UUID]"]


def is_reachable(edges: Edges, start: uuid.UUID, target: uuid.UUID) -> bool:
    """Return True if ``target`` is reachable from ``start`` following edges."""
    stack: list[uuid.UUID] = [start]
    seen: set[uuid.UUID] = set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


def would_create_cycle(edges: Edges, src: uuid.UUID, dst: uuid.UUID) -> bool:
    """True if adding edge ``src -> dst`` (src depends on dst) closes a cycle.

    A self-edge is always a cycle. Otherwise the edge is unsafe iff ``src`` is
    already reachable from ``dst`` (i.e. ``dst -> ... -> src`` exists), which the
    new edge would complete into ``src -> dst -> ... -> src``.
    """
    if src == dst:
        return True
    return is_reachable(edges, dst, src)


def has_cycle(edges: Edges) -> bool:
    """True if the directed graph described by ``edges`` contains any cycle."""
    visiting: set[uuid.UUID] = set()
    done: set[uuid.UUID] = set()

    def _visit(node: uuid.UUID) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for nxt in edges.get(node, ()):
            if _visit(nxt):
                return True
        visiting.discard(node)
        done.add(node)
        return False

    return any(_visit(node) for node in edges)


def topological_order(edges: Edges, nodes: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Return nodes ordered dependencies-first (``b`` before ``a`` for ``a -> b``).

    Raises :class:`CycleError` if the graph is cyclic.
    """
    order: list[uuid.UUID] = []
    done: set[uuid.UUID] = set()
    visiting: set[uuid.UUID] = set()

    def _visit(node: uuid.UUID) -> None:
        if node in done:
            return
        if node in visiting:
            raise CycleError(f"dependency cycle detected at {node}")
        visiting.add(node)
        for nxt in edges.get(node, ()):
            _visit(nxt)
        visiting.discard(node)
        done.add(node)
        order.append(node)

    for node in nodes:
        _visit(node)
    return order


__all__ = [
    "Edges",
    "has_cycle",
    "is_reachable",
    "topological_order",
    "would_create_cycle",
]
