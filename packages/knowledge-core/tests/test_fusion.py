"""Tests for ``forge_knowledge.fusion`` — Reciprocal Rank Fusion (Task 1.3, spine).

RRF combines several independent rankings into one with the parameter-free
formula fixed in the plan's Global Constraints and the spec's pipeline diagram::

    score(d) = Σ_i 1 / (k + rank_i(d)),  k = 60

These tests pin the math against a hand-computed example so the implementation
cannot silently drift (e.g. 0-based ranks, wrong ``k``, or averaging instead of
summing). Pure functions, no I/O.
"""

from __future__ import annotations

import math
from itertools import pairwise

from forge_contracts.constants import RRF_K
from forge_contracts.dtos import Ranked, RetrievedChunk
from forge_knowledge.fusion import fuse


def _ranking(ids: list[str]) -> list[Ranked]:
    """Build a ranking (1-based ranks) from an ordered list of chunk ids."""
    return [
        Ranked(
            chunk_id=cid,
            score=1.0 / rank,
            rank=rank,
            chunk=RetrievedChunk(id=cid, content=f"content for {cid}"),
        )
        for rank, cid in enumerate(ids, start=1)
    ]


def test_fuse_default_k_is_the_spec_constant() -> None:
    # Guard against the constant drifting away from the frozen RRF_K = 60.
    assert RRF_K == 60


def test_rrf_matches_hand_computed_example() -> None:
    # Ranking A: d1 > d2 > d3 ;  Ranking B: d2 > d3 > d4
    ranking_a = _ranking(["d1", "d2", "d3"])
    ranking_b = _ranking(["d2", "d3", "d4"])

    fused = fuse([ranking_a, ranking_b], k=60)

    scores = {r.chunk_id: r.score for r in fused}
    expected = {
        "d1": 1 / 61,
        "d2": 1 / 62 + 1 / 61,
        "d3": 1 / 63 + 1 / 62,
        "d4": 1 / 63,
    }
    for cid, value in expected.items():
        assert math.isclose(scores[cid], value, rel_tol=1e-12), cid

    # Order: d2 > d3 > d1 > d4 (verified from the hand-computed scores).
    assert [r.chunk_id for r in fused] == ["d2", "d3", "d1", "d4"]


def test_fused_results_are_sorted_descending_with_reassigned_ranks() -> None:
    fused = fuse([_ranking(["a", "b", "c"]), _ranking(["c", "b", "a"])])
    assert all(a.score >= b.score for a, b in pairwise(fused))
    # Ranks are reassigned 1..N over the fused ordering.
    assert [r.rank for r in fused] == list(range(1, len(fused) + 1))


def test_fuse_uses_default_k_when_omitted() -> None:
    # With the default k=60 a single ranking's top item scores 1/(60+1).
    fused = fuse([_ranking(["only"])])
    assert math.isclose(fused[0].score, 1 / 61, rel_tol=1e-12)


def test_fuse_preserves_chunk_payload_for_attribution() -> None:
    fused = fuse([_ranking(["x"])])
    assert fused[0].chunk is not None
    assert fused[0].chunk.content == "content for x"


def test_fuse_dedupes_across_rankings() -> None:
    fused = fuse([_ranking(["a", "b"]), _ranking(["a", "b"])])
    assert sorted(r.chunk_id for r in fused) == ["a", "b"]


def test_fuse_empty_input_returns_empty() -> None:
    assert fuse([]) == []
    assert fuse([[], []]) == []


def test_fuse_falls_back_to_position_when_rank_unset() -> None:
    # Entries with rank=0 (unset) should be ranked by list position (1-based).
    a = [
        Ranked(chunk_id="p", score=0.9, rank=0),
        Ranked(chunk_id="q", score=0.8, rank=0),
    ]
    fused = fuse([a], k=60)
    scores = {r.chunk_id: r.score for r in fused}
    assert math.isclose(scores["p"], 1 / 61, rel_tol=1e-12)
    assert math.isclose(scores["q"], 1 / 62, rel_tol=1e-12)
