"""F35 unit tests — deterministic replay + verification (AC8/9/10/11)."""

from __future__ import annotations

import socket

import pytest

from forge_eval.benchmark import (
    BenchmarkScoring,
    ReplayBundle,
    compute_benchmark_score,
    compute_bundle_hash,
    make_bundle,
    replay_bundles,
    verify_submission,
)
from forge_eval.golden import GoldenCase

EPSILON = 0.005

SCORING = BenchmarkScoring(
    metric_weights={"retrieval.recall_at_k": 0.7, "retrieval.mrr": 0.3}, k=5
)

CASES = [
    GoldenCase(id="c1", query="q1", expected_ids=["a", "b"], tags=["retrieval"]),
    GoldenCase(id="c2", query="q2", expected_ids=["x"], tags=["retrieval"]),
]


def _faithful_bundles() -> list[ReplayBundle]:
    return [
        make_bundle("c1", ["a", "b", "z"]),  # recall 1.0, mrr 1.0
        make_bundle("c2", ["y", "x"]),  # recall 1.0, mrr 0.5
    ]


def _claimed_from(bundles: list[ReplayBundle]):
    report = replay_bundles(bundles, CASES, SCORING)
    return compute_benchmark_score(report, SCORING, CASES)


def test_verify_accepts_within_epsilon() -> None:
    """AC8: a faithful submission verifies."""
    bundles = _faithful_bundles()
    claimed = _claimed_from(bundles)
    result = verify_submission(
        claimed=claimed,
        reproduced_report=replay_bundles(bundles, CASES, SCORING),
        reproduced_bundles=bundles,
        claimed_bundle_hashes=[b.content_hash for b in bundles],
        scoring=SCORING,
        cases=CASES,
        epsilon=EPSILON,
    )
    assert result.verified is True
    assert result.bundle_hash_matches is True
    assert result.score_delta == 0.0
    assert result.reasons == []


def test_verify_rejects_score_delta() -> None:
    """AC9: a claimed composite inflated beyond epsilon is rejected with a reason."""
    bundles = _faithful_bundles()
    claimed = _claimed_from(bundles).model_copy(update={"composite": 0.999999})
    result = verify_submission(
        claimed=claimed,
        reproduced_report=replay_bundles(bundles, CASES, SCORING),
        reproduced_bundles=bundles,
        claimed_bundle_hashes=[b.content_hash for b in bundles],
        scoring=SCORING,
        cases=CASES,
        epsilon=EPSILON,
    )
    assert result.verified is False
    assert result.bundle_hash_matches is True
    assert result.score_delta > EPSILON
    assert any("epsilon" in reason for reason in result.reasons)


def test_verify_rejects_bundle_hash_mismatch() -> None:
    """AC10: a tampered bundle fails regardless of score proximity."""
    bundles = _faithful_bundles()
    claimed = _claimed_from(bundles)
    claimed_hashes = [b.content_hash for b in bundles]
    # Tamper: mutate the recorded outputs after hashing (hash no longer matches).
    tampered = [
        bundles[0].model_copy(update={"output_ids": ["a", "b", "tampered"]}),
        bundles[1],
    ]
    result = verify_submission(
        claimed=claimed,
        reproduced_report=replay_bundles(tampered, CASES, SCORING),
        reproduced_bundles=tampered,
        claimed_bundle_hashes=claimed_hashes,
        scoring=SCORING,
        cases=CASES,
        epsilon=1.0,  # generous epsilon: hash mismatch alone must reject
    )
    assert result.verified is False
    assert result.bundle_hash_matches is False
    assert any("c1" in reason for reason in result.reasons)


def test_missing_bundle_scores_zero_not_gain() -> None:
    """A submission cannot gain by omitting a case's bundle."""
    bundles = [_faithful_bundles()[0]]
    report = replay_bundles(bundles, CASES, SCORING)
    missing = next(r for r in report.results if r.case_id == "c2")
    assert missing.error == "missing replay bundle"
    assert missing.score == 0.0
    assert len(report.results) == len(CASES)


def test_replay_deterministic_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC11: replay performs zero network calls and is run-to-run identical."""

    def _blocked(*_args, **_kwargs):  # pragma: no cover - trips only on regression
        raise AssertionError("network access attempted during offline replay")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    bundles = _faithful_bundles()
    first = replay_bundles(bundles, CASES, SCORING)
    second = replay_bundles(bundles, CASES, SCORING)
    assert first.model_dump_json() == second.model_dump_json()
    v1 = verify_submission(
        claimed=compute_benchmark_score(first, SCORING, CASES),
        reproduced_report=first,
        reproduced_bundles=bundles,
        claimed_bundle_hashes=[b.content_hash for b in bundles],
        scoring=SCORING,
        cases=CASES,
        epsilon=EPSILON,
    )
    v2 = verify_submission(
        claimed=compute_benchmark_score(second, SCORING, CASES),
        reproduced_report=second,
        reproduced_bundles=bundles,
        claimed_bundle_hashes=[b.content_hash for b in bundles],
        scoring=SCORING,
        cases=CASES,
        epsilon=EPSILON,
    )
    assert v1.model_dump_json() == v2.model_dump_json()


def test_bundle_hash_is_canonical() -> None:
    assert compute_bundle_hash("c1", ["a", "b"]) == compute_bundle_hash("c1", ["a", "b"])
    assert compute_bundle_hash("c1", ["a", "b"]) != compute_bundle_hash("c1", ["b", "a"])
    assert make_bundle("c1", ["a"]).content_hash.startswith("sha256:")
