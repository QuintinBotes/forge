"""Retrieval evaluation metric primitives.

Pure, deterministic functions over ranked id lists. No I/O, no network — these
are the numeric core that the golden retrieval eval (Task 1.4) and the golden
task harness (Task 1.16) build on. ``retrieved`` is an *ordered* sequence of ids
(best first); ``relevant`` is the unordered set of ground-truth ids.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

__all__ = [
    "average_precision",
    "hit_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
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
