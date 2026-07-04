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
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    requirement_satisfaction,
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


def test_ndcg_single_relevant_rank_one_is_perfect() -> None:
    # One relevant id at rank 1: DCG = 1/log2(2) = 1; IDCG = 1 → nDCG = 1.0.
    assert ndcg_at_k(["a", "b", "c"], {"a"}, k=5) == 1.0


def test_ndcg_single_relevant_rank_three_matches_hand_computed() -> None:
    # Relevant id at rank 3, k=5: nDCG = 1/log2(4) = 0.5 (IDCG places it at rank 1).
    assert math.isclose(ndcg_at_k(["x", "y", "a"], {"a"}, k=5), 1.0 / math.log2(4))
    assert math.isclose(ndcg_at_k(["x", "y", "a"], {"a"}, k=5), 0.5)


def test_ndcg_relevant_beyond_k_is_zero() -> None:
    # The only relevant id is at rank 4, outside the k=3 window → 0.0.
    assert ndcg_at_k(["x", "y", "z", "a"], {"a"}, k=3) == 0.0


def test_ndcg_empty_relevant_is_vacuously_one() -> None:
    assert ndcg_at_k(["a", "b"], set(), k=2) == 1.0


def test_ndcg_no_hit_in_window_is_zero() -> None:
    assert ndcg_at_k(["x", "y"], {"a"}, k=2) == 0.0


def test_ndcg_graded_gains_change_the_score() -> None:
    # Two relevant ids; the higher-gain one is ranked *second*, so nDCG < 1.0 and
    # differs from the binary case. Ideal ordering puts gain 3 first, gain 1 second.
    retrieved = ["a", "b", "c"]
    relevant = {"a", "b"}
    gains = {"a": 1.0, "b": 3.0}
    dcg = 1.0 / math.log2(2) + 3.0 / math.log2(3)
    idcg = 3.0 / math.log2(2) + 1.0 / math.log2(3)
    assert math.isclose(ndcg_at_k(retrieved, relevant, 3, gains=gains), dcg / idcg)
    # Binary (default gains) scores this ordering as perfect (both in top-2).
    assert ndcg_at_k(retrieved, relevant, 3) == 1.0


def test_ndcg_perfect_ordering_with_gains_is_one() -> None:
    retrieved = ["b", "a", "c"]
    gains = {"a": 1.0, "b": 3.0}
    assert math.isclose(ndcg_at_k(retrieved, {"a", "b"}, 3, gains=gains), 1.0)


def test_requirement_satisfaction_full() -> None:
    assert requirement_satisfaction({"R1", "R2"}, {"R1", "R2"}) == 1.0


def test_requirement_satisfaction_partial() -> None:
    # 1 of 2 expected requirements satisfied → 0.5.
    assert requirement_satisfaction({"R1", "extra"}, {"R1", "R2"}) == 0.5


def test_requirement_satisfaction_none_expected_is_vacuously_one() -> None:
    assert requirement_satisfaction(set(), set()) == 1.0


def test_requirement_satisfaction_miss_is_zero() -> None:
    assert requirement_satisfaction(set(), {"R1"}) == 0.0
