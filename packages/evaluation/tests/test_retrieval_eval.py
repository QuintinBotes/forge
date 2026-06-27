"""Golden retrieval eval over the real hybrid pipeline (plan Task 1.4, spine).

These tests are the spine's quality proof in ``forge_eval``: they index the fixed
sample corpus through the genuine :class:`~forge_knowledge.KnowledgeService`
(semantic + keyword -> RRF -> rerank) and score the golden retrieval set with the
:mod:`forge_eval.runner`, asserting a recall@k / MRR regression threshold and
printing the scorecard for the morning report.

Hermetic: in-memory SQLite, deterministic embedding, fixture reranker. No network.
"""

from __future__ import annotations

import pytest

from forge_eval.report import format_scorecard
from forge_eval.retrieval_eval import (
    DEFAULT_K,
    DEFAULT_RECALL_THRESHOLD,
    SAMPLE_CORPUS,
    build_indexed_service,
    load_golden_retrieval,
    make_retrieve_fn,
    run_retrieval_eval,
)

MIN_GOLDEN_PAIRS = 15
MRR_THRESHOLD = 0.80


def test_golden_set_has_enough_pairs_referencing_real_corpus() -> None:
    cases = load_golden_retrieval()
    assert len(cases) >= MIN_GOLDEN_PAIRS
    # Every ground-truth id is a real file in the sample corpus.
    for case in cases:
        assert case.expected_ids, f"{case.id} has no expected_ids"
        for path in case.expected_ids:
            assert path in SAMPLE_CORPUS, f"{case.id} references unknown chunk {path!r}"
    # Ids are unique (the loader also enforces this).
    assert len({c.id for c in cases}) == len(cases)


def test_retrieval_eval_meets_recall_and_mrr_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    card = run_retrieval_eval(k=DEFAULT_K, recall_threshold=DEFAULT_RECALL_THRESHOLD)

    # Surface the numbers for the morning report.
    with capsys.disabled():
        print("\n" + format_scorecard(card))
        print(
            f"[RAG golden eval] cases={card.num_cases} "
            f"recall@{card.k}={card.mean_recall_at_k:.3f} "
            f"MRR={card.mean_mrr:.3f} hit_rate={card.hit_rate:.3f}"
        )

    assert card.num_cases >= MIN_GOLDEN_PAIRS
    assert card.mean_recall_at_k >= DEFAULT_RECALL_THRESHOLD
    assert card.mean_mrr >= MRR_THRESHOLD
    assert card.passed
    # The gate must not raise when the threshold is met.
    card.assert_threshold()


def test_exact_identifier_queries_are_recovered() -> None:
    """The keyword (BM25) leg must recover exact identifiers a dense vector dilutes."""
    service, scope = build_indexed_service()
    retrieve = make_retrieve_fn(service, scope)

    for case in load_golden_retrieval():
        if "identifier" in case.tags:
            top = list(retrieve(case))
            assert top, f"{case.id} returned nothing"
            assert top[0] in case.expected_ids, (
                f"{case.id} ({case.query!r}) expected {case.expected_ids} at rank 1, "
                f"got {top[:3]}"
            )


def test_regression_gate_trips_below_threshold() -> None:
    """An impossible threshold must fail the gate (proves the gate is real)."""
    card = run_retrieval_eval(k=DEFAULT_K, recall_threshold=1.01)
    assert not card.passed
    with pytest.raises(AssertionError):
        card.assert_threshold()
