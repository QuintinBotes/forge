"""Reciprocal Rank Fusion (RRF) for hybrid retrieval (plan Task 1.3, spine).

RRF merges several independent rankings (here: the pgvector *semantic* leg and
the BM25 *keyword* leg) into a single ranking without any score normalisation or
tuning. It is the parameter-free fusion fixed in the plan's Global Constraints
and the spec's retrieval pipeline::

    score(d) = Σ_i 1 / (k + rank_i(d)),  k = 60

where ``rank_i(d)`` is the 1-based rank of document ``d`` in ranking ``i`` (a
document missing from a ranking contributes nothing). The constant ``k`` damps
the influence of low-ranked items; ``k = 60`` is the canonical value from the
original Cormack et al. RRF paper and the spec.

This module is pure (no I/O): it is unit-tested against a hand-computed example
so the formula cannot silently drift.
"""

from __future__ import annotations

from forge_contracts.constants import RRF_K
from forge_contracts.dtos import Ranked, RetrievedChunk

__all__ = ["fuse"]


def fuse(rankings: list[list[Ranked]], k: int = RRF_K) -> list[Ranked]:
    """Fuse ``rankings`` into one RRF-scored ranking, best first.

    Args:
        rankings: independent rankings to combine. Each entry's ``rank`` field is
            the 1-based rank used in the RRF formula; when ``rank`` is unset
            (``0``/falsey) the entry's position in its list (1-based) is used.
        k: the RRF constant (defaults to the frozen ``RRF_K`` = 60).

    Returns:
        A single ``Ranked`` list sorted by descending fused score, with ``rank``
        reassigned 1..N over the fused order and the best-available
        ``RetrievedChunk`` preserved for source attribution.
    """
    fused_scores: dict[str, float] = {}
    chunks: dict[str, RetrievedChunk | None] = {}

    for ranking in rankings:
        for position, entry in enumerate(ranking, start=1):
            rank = entry.rank if entry.rank else position
            fused_scores[entry.chunk_id] = (
                fused_scores.get(entry.chunk_id, 0.0) + 1.0 / (k + rank)
            )
            # Keep the first non-null chunk payload we see for this id.
            if chunks.get(entry.chunk_id) is None and entry.chunk is not None:
                chunks[entry.chunk_id] = entry.chunk

    ordered = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
    return [
        Ranked(chunk_id=chunk_id, score=score, rank=rank, chunk=chunks.get(chunk_id))
        for rank, (chunk_id, score) in enumerate(ordered, start=1)
    ]
