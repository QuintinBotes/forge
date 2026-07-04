"""Retrieval evaluation metric primitives.

Pure, deterministic functions over ranked id lists. No I/O, no network — these
are the numeric core that the golden retrieval eval (Task 1.4) and the golden
task harness (Task 1.16) build on. ``retrieved`` is an *ordered* sequence of ids
(best first); ``relevant`` is the unordered set of ground-truth ids.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence

__all__ = [
    "average_precision",
    "hit_at_k",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "requirement_satisfaction",
]


def _top_k(retrieved: Sequence[str], k: int) -> list[str]:
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    return list(retrieved[:k])


def recall_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Fraction of relevant ids found within the top-``k`` retrieved ids.

    Returns ``1.0`` when there are no relevant ids (nothing to miss).
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    window = set(_top_k(retrieved, k))
    return len(relevant_set & window) / len(relevant_set)


def precision_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Fraction of the top-``k`` retrieved ids that are relevant."""
    relevant_set = set(relevant)
    window = _top_k(retrieved, k)
    if not window:
        return 0.0
    hits = sum(1 for cid in window if cid in relevant_set)
    return hits / len(window)


def hit_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> bool:
    """True if at least one relevant id appears within the top-``k``."""
    relevant_set = set(relevant)
    return any(cid in relevant_set for cid in _top_k(retrieved, k))


def reciprocal_rank(retrieved: Sequence[str], relevant: Collection[str]) -> float:
    """Reciprocal of the (1-indexed) rank of the first relevant id; 0 if none."""
    relevant_set = set(relevant)
    for index, cid in enumerate(retrieved, start=1):
        if cid in relevant_set:
            return 1.0 / index
    return 0.0


def average_precision(retrieved: Sequence[str], relevant: Collection[str]) -> float:
    """Average precision over the ranked list (the per-query term of MAP)."""
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    hits = 0
    score = 0.0
    for index, cid in enumerate(retrieved, start=1):
        if cid in relevant_set:
            hits += 1
            score += hits / index
    return score / len(relevant_set)


def ndcg_at_k(
    retrieved: Sequence[str],
    relevant: Collection[str],
    k: int,
    *,
    gains: Mapping[str, float] | None = None,
) -> float:
    """Normalised Discounted Cumulative Gain over the top-``k`` retrieved ids.

    ``DCG@k = Σ gain_i / log2(rank_i + 1)`` over the top-``k`` (rank is 1-indexed),
    where ``gain_i`` is ``gains[id]`` (default ``1.0``) when the id is relevant and
    ``0.0`` otherwise. ``IDCG@k`` is the same sum over the *ideal* ordering (the
    relevant ids sorted by gain, descending). Returns ``DCG/IDCG``.

    Edge cases mirror :func:`recall_at_k`: an empty ``relevant`` set returns
    ``1.0`` (nothing to rank, vacuously perfect); when none of the top-``k`` ids
    are relevant the numerator is ``0.0`` so the score is ``0.0``.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    gain_map = dict(gains or {})

    def gain(cid: str) -> float:
        return float(gain_map.get(cid, 1.0)) if cid in relevant_set else 0.0

    window = _top_k(retrieved, k)
    dcg = sum(gain(cid) / math.log2(rank + 1) for rank, cid in enumerate(window, start=1))

    ideal_gains = sorted((float(gain_map.get(cid, 1.0)) for cid in relevant_set), reverse=True)[:k]
    idcg = sum(g / math.log2(rank + 1) for rank, g in enumerate(ideal_gains, start=1))
    if idcg <= 0.0:
        return 0.0
    return dcg / idcg


def requirement_satisfaction(satisfied: Collection[str], expected: Collection[str]) -> float:
    """Fraction of expected requirement ids that were satisfied.

    This is the *spec-requirement satisfaction rate* (spec: Observability and
    Evaluation, "Agent quality") at the per-task level: set overlap, order
    independent. Returns ``1.0`` when nothing is expected (vacuously satisfied).
    """
    expected_set = set(expected)
    if not expected_set:
        return 1.0
    satisfied_set = set(satisfied)
    return len(expected_set & satisfied_set) / len(expected_set)
