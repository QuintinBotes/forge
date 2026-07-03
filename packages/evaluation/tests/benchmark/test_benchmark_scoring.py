"""F35 unit tests — composite scoring math (AC3/AC4/AC5)."""

from __future__ import annotations

from forge_eval.benchmark import (
    BenchmarkCaseResult,
    BenchmarkReport,
    BenchmarkScoring,
    MetricAggregate,
    compute_benchmark_score,
)
from forge_eval.golden import GoldenCase


def _report(aggregate: dict[str, float]) -> BenchmarkReport:
    return BenchmarkReport(
        aggregate={m: MetricAggregate(mean=v, count=3) for m, v in aggregate.items()},
        results=[],
    )


def test_composite_hand_computed() -> None:
    """AC3: 0.5*0.8 + 0.3*0.6 + 0.2*1.0 == 0.78 exactly."""
    scoring = BenchmarkScoring(
        metric_weights={
            "agent.requirement_satisfaction_rate": 0.5,
            "retrieval.ndcg_at_k": 0.3,
            "spec.completeness": 0.2,
        }
    )
    report = _report(
        {
            "agent.requirement_satisfaction_rate": 0.8,
            "retrieval.ndcg_at_k": 0.6,
            "spec.completeness": 1.0,
        }
    )
    score = compute_benchmark_score(report, scoring, [])
    assert score.composite == 0.78


def test_composite_recompute_identical() -> None:
    """AC3/AC11: recomputation yields a byte-identical BenchmarkScore."""
    scoring = BenchmarkScoring(
        metric_weights={"retrieval.recall_at_k": 0.7, "retrieval.mrr": 0.3}
    )
    report = _report({"retrieval.recall_at_k": 0.61, "retrieval.mrr": 0.44})
    first = compute_benchmark_score(report, scoring, [])
    second = compute_benchmark_score(report, scoring, [])
    assert first.model_dump_json() == second.model_dump_json()


def test_direction_lower_is_better_and_missing_metric_zero() -> None:
    """AC4: lower_is_better contributes 1-x clamped; missing metric -> 0.0."""
    scoring = BenchmarkScoring(
        metric_weights={"cost.tokens_norm": 0.5, "retrieval.recall_at_k": 0.5},
        direction={"cost.tokens_norm": "lower_is_better"},
    )
    # cost 0.2 -> contributes 0.8; recall missing -> contributes 0.0.
    report = _report({"cost.tokens_norm": 0.2})
    score = compute_benchmark_score(report, scoring, [])
    assert score.composite == 0.4
    assert score.per_metric["retrieval.recall_at_k"] == 0.0

    # Out-of-range lower_is_better values clamp to [0, 1].
    clamped = compute_benchmark_score(
        _report({"cost.tokens_norm": 3.0, "retrieval.recall_at_k": 1.0}), scoring, []
    )
    assert clamped.composite == 0.5


def test_weights_are_normalized() -> None:
    """Weights that do not sum to 1 are normalized at scoring time."""
    scoring = BenchmarkScoring(
        metric_weights={"retrieval.recall_at_k": 2.0, "retrieval.mrr": 2.0}
    )
    report = _report({"retrieval.recall_at_k": 1.0, "retrieval.mrr": 0.0})
    assert compute_benchmark_score(report, scoring, []).composite == 0.5


def test_per_category_covers_all_cases_once() -> None:
    """AC5: every case lands in exactly one category; weights sum to 1."""
    scoring = BenchmarkScoring(metric_weights={"retrieval.recall_at_k": 1.0})
    cases = [
        GoldenCase(id="r1", query="q", expected_ids=["x"], tags=["retrieval"]),
        GoldenCase(id="r2", query="q", expected_ids=["x"], tags=["retrieval", "extra"]),
        GoldenCase(id="s1", query="q", expected_ids=["x"], tags=["spec"]),
        # No tags at all -> falls back to the case kind.
        GoldenCase(id="t1", query="q", expected_ids=["x"], kind="agent_task", tags=[]),
    ]
    report = BenchmarkReport(
        aggregate={"retrieval.recall_at_k": MetricAggregate(mean=0.5, count=4)},
        results=[
            BenchmarkCaseResult(case_id="r1", score=1.0),
            BenchmarkCaseResult(case_id="r2", score=0.5),
            BenchmarkCaseResult(case_id="s1", score=0.25),
            BenchmarkCaseResult(case_id="t1", score=0.0),
        ],
    )
    score = compute_benchmark_score(report, scoring, cases)
    by_category = {c.category: c for c in score.per_category}
    assert set(by_category) == {"retrieval", "spec", "agent_task"}
    assert sum(c.case_count for c in score.per_category) == len(cases)
    assert sum(c.weight for c in score.per_category) == 1.0
    assert by_category["retrieval"].case_count == 2
    assert by_category["retrieval"].score == 0.75
    assert by_category["agent_task"].score == 0.0
    assert score.total_cases == 4
