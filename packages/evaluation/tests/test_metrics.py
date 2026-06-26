"""Unit tests for the retrieval evaluation metrics (Task 0.6 scaffold).

These pin the exact numeric behaviour of the metric primitives that the golden
retrieval eval (Task 1.4) and the golden task harness (Task 1.16) build on. The
formulas are hand-computed here so a regression in the metric implementation is
caught immediately.
"""

from __future__ import annotations

import math

from forge_eval.metrics import (
    hit_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_recall_at_k_full_hit() -> None:
    retrieved = ["a", "b", "c", "d"]
    relevant = {"a", "c"}
    assert recall_at_k(retrieved, relevant, k=4) == 1.0


def test_recall_at_k_partial_within_cutoff() -> None:
    # Only "a" is inside the top-2 window; "c" is at rank 3 → missed.
    retrieved = ["a", "x", "c"]
    relevant = {"a", "c"}
    assert recall_at_k(retrieved, relevant, k=2) == 0.5


def test_recall_at_k_no_relevant_is_vacuously_one() -> None:
    assert recall_at_k(["a", "b"], set(), k=2) == 1.0


def test_recall_at_k_miss_is_zero() -> None:
    assert recall_at_k(["x", "y"], {"a"}, k=2) == 0.0


def test_precision_at_k() -> None:
    # 1 relevant in the top-2 window → precision 0.5.
    retrieved = ["a", "x", "c"]
    relevant = {"a", "c"}
    assert precision_at_k(retrieved, relevant, k=2) == 0.5


def test_reciprocal_rank_first_position() -> None:
    assert reciprocal_rank(["a", "b"], {"a"}) == 1.0


def test_reciprocal_rank_third_position() -> None:
    assert math.isclose(reciprocal_rank(["x", "y", "a"], {"a"}), 1 / 3)


def test_reciprocal_rank_no_hit() -> None:
    assert reciprocal_rank(["x", "y"], {"a"}) == 0.0


def test_hit_at_k() -> None:
    assert hit_at_k(["x", "a", "y"], {"a"}, k=2) is True
    assert hit_at_k(["x", "y", "a"], {"a"}, k=2) is False
