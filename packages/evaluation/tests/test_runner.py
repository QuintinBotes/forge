"""Unit tests for the golden-set eval runner scaffold (Task 0.6).

The runner loads golden cases, runs a caller-supplied retrieval function
(a deterministic fake in tests — no network), computes per-case + aggregate
metrics, emits a scorecard, and enforces a regression threshold gate.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from forge_eval.golden import GoldenCase, load_golden_set
from forge_eval.report import format_scorecard
from forge_eval.runner import Scorecard, evaluate_retrieval, run_golden_eval

EXAMPLE = (
    Path(__file__).resolve().parent.parent / "forge_eval" / "golden" / "example_retrieval.yaml"
)


def _perfect_retriever(case: GoldenCase) -> Sequence[str]:
    """A fake pipeline that returns exactly the expected ids (recall = 1.0)."""
    return list(case.expected_ids)


def _useless_retriever(case: GoldenCase) -> Sequence[str]:
    """A fake pipeline that never returns a relevant id (recall = 0.0)."""
    return ["__none__"]


# --------------------------------------------------------------------------- #
# Golden set loading                                                          #
# --------------------------------------------------------------------------- #


def test_load_golden_set_json_roundtrip(tmp_path: Path) -> None:
    payload = [
        {"id": "Q1", "query": "auth middleware", "expected_ids": ["chunk-1", "chunk-2"]},
        {"id": "Q2", "query": "pagination", "expected_ids": ["chunk-9"], "tags": ["board"]},
    ]
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(payload), encoding="utf-8")

    cases = load_golden_set(f)
    assert [c.id for c in cases] == ["Q1", "Q2"]
    assert cases[0].expected_ids == ["chunk-1", "chunk-2"]
    assert cases[1].tags == ["board"]


def test_load_golden_set_rejects_duplicate_ids(tmp_path: Path) -> None:
    f = tmp_path / "dupe.json"
    f.write_text(
        json.dumps(
            [
                {"id": "Q1", "query": "a", "expected_ids": ["x"]},
                {"id": "Q1", "query": "b", "expected_ids": ["y"]},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_golden_set(f)


def test_load_golden_set_requires_expected_ids(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([{"id": "Q1", "query": "a", "expected_ids": []}]), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_ids"):
        load_golden_set(f)


def test_example_golden_set_loads() -> None:
    cases = load_golden_set(EXAMPLE)
    assert len(cases) >= 3
    assert all(c.expected_ids for c in cases)


# --------------------------------------------------------------------------- #
# Evaluation + scorecard                                                       #
# --------------------------------------------------------------------------- #


def test_evaluate_retrieval_perfect_pipeline_passes() -> None:
    cases = load_golden_set(EXAMPLE)
    card = evaluate_retrieval(cases, _perfect_retriever, k=10, recall_threshold=0.9)
    assert isinstance(card, Scorecard)
    assert card.num_cases == len(cases)
    assert card.mean_recall_at_k == 1.0
    assert card.mean_mrr == 1.0
    assert card.num_passed == card.num_cases
    assert card.passed is True


def test_evaluate_retrieval_useless_pipeline_fails_gate() -> None:
    cases = load_golden_set(EXAMPLE)
    card = evaluate_retrieval(cases, _useless_retriever, k=10, recall_threshold=0.5)
    assert card.mean_recall_at_k == 0.0
    assert card.num_passed == 0
    assert card.passed is False


def test_scorecard_assert_threshold_raises_on_regression() -> None:
    cases = load_golden_set(EXAMPLE)
    card = evaluate_retrieval(cases, _useless_retriever, k=10, recall_threshold=0.5)
    with pytest.raises(AssertionError, match="recall"):
        card.assert_threshold()


def test_scorecard_assert_threshold_passes_when_met() -> None:
    cases = load_golden_set(EXAMPLE)
    card = evaluate_retrieval(cases, _perfect_retriever, k=10, recall_threshold=0.9)
    card.assert_threshold()  # should not raise


def test_run_golden_eval_from_path() -> None:
    card = run_golden_eval(EXAMPLE, _perfect_retriever, k=5, recall_threshold=0.8)
    assert card.passed is True
    assert card.k == 5


def test_format_scorecard_is_readable() -> None:
    cases = load_golden_set(EXAMPLE)
    card = evaluate_retrieval(cases, _perfect_retriever, k=10, recall_threshold=0.9)
    report = format_scorecard(card)
    assert "recall@" in report
    assert "MRR" in report
    assert "PASS" in report
