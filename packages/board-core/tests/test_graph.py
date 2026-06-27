"""Tests for the dependency graph + cycle detection (plan Task 1.5)."""

from __future__ import annotations

import uuid

import pytest

from forge_board.graph import (
    has_cycle,
    is_reachable,
    topological_order,
    would_create_cycle,
)
from forge_contracts import CycleError


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_would_create_cycle_on_empty_graph_is_false() -> None:
    a, b = _ids(2)
    assert not would_create_cycle({}, a, b)


def test_self_dependency_is_a_cycle() -> None:
    (a,) = _ids(1)
    assert would_create_cycle({}, a, a)


def test_back_edge_creates_cycle() -> None:
    a, b = _ids(2)
    # a depends on b (a -> b). Adding b -> a closes a cycle.
    edges = {a: {b}}
    assert would_create_cycle(edges, b, a)


def test_transitive_back_edge_creates_cycle() -> None:
    a, b, c = _ids(3)
    # a -> b -> c. Adding c -> a closes a 3-node cycle.
    edges = {a: {b}, b: {c}}
    assert would_create_cycle(edges, c, a)


def test_parallel_edge_does_not_create_cycle() -> None:
    a, b, c = _ids(3)
    # a -> b, a -> c is a valid DAG; adding b -> c stays acyclic.
    edges = {a: {b, c}}
    assert not would_create_cycle(edges, b, c)


def test_is_reachable() -> None:
    a, b, c = _ids(3)
    edges = {a: {b}, b: {c}}
    assert is_reachable(edges, a, c)
    assert not is_reachable(edges, c, a)


def test_has_cycle_detects_existing_cycle() -> None:
    a, b, c = _ids(3)
    assert has_cycle({a: {b}, b: {c}, c: {a}})
    assert not has_cycle({a: {b}, b: {c}})


def test_topological_order_of_dag() -> None:
    a, b, c = _ids(3)
    # a -> b -> c means c must come before b before a (dependencies first).
    order = topological_order({a: {b}, b: {c}}, [a, b, c])
    assert order.index(c) < order.index(b) < order.index(a)


def test_topological_order_raises_on_cycle() -> None:
    a, b = _ids(2)
    with pytest.raises(CycleError):
        topological_order({a: {b}, b: {a}}, [a, b])
