"""Deterministic composite scoring over a benchmark report (F35 §4).

``compute_benchmark_score`` is a pure function: same report + rubric + cases in,
byte-identical :class:`BenchmarkScore` out. No I/O, no clock, no randomness.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean

from forge_eval.benchmark.models import (
    LOWER_IS_BETTER,
    BenchmarkReport,
    BenchmarkScore,
    BenchmarkScoring,
    CategoryScore,
)
from forge_eval.golden import GoldenCase

__all__ = ["case_category", "compute_benchmark_score", "normalize_metric"]

#: Rounding applied to emitted scores so recomputation is byte-identical across
#: platforms (floating-point sums are order-stable here, but 1e-6 granularity
#: keeps stored JSON canonical).
_PRECISION = 6


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def normalize_metric(value: float, direction: str | None) -> float:
    """Direction-normalize a raw metric value into [0, 1].

    ``lower_is_better`` contributes ``1 - value`` (clamped); anything else is
    treated as higher-is-better and clamped as-is.
    """
    if direction == LOWER_IS_BETTER:
        return _clamp01(1.0 - value)
    return _clamp01(value)


def case_category(case: GoldenCase, category_field: str) -> str:
    """Derive the single category a case belongs to (AC5).

    ``case[category_field][0]`` when that field is a non-empty list (e.g. the
    first tag), else ``str(value)`` when truthy, falling back to the case kind.
    """
    value = getattr(case, category_field, None)
    if value is None:
        value = case.metadata.get(category_field)
    if isinstance(value, list | tuple):
        return str(value[0]) if value else case.kind
    if value:
        return str(value)
    return case.kind


def compute_benchmark_score(
    report: BenchmarkReport,
    scoring: BenchmarkScoring,
    cases: Sequence[GoldenCase],
) -> BenchmarkScore:
    """Pure composite scoring per the frozen rubric.

    For each metric in ``scoring.metric_weights``, reads
    ``report.aggregate[metric].mean`` (missing -> 0.0), direction-normalizes it,
    then ``composite = sum(w_i * x_i) / sum(w_i)``. ``per_category`` groups
    ``report.results`` (joined to ``cases`` by ``case_id``) into single-owner
    categories; every case is counted exactly once and the category weights sum
    to 1.0.
    """
    total_weight = sum(scoring.metric_weights.values())
    per_metric: dict[str, float] = {}
    composite = 0.0
    for metric, weight in scoring.metric_weights.items():
        aggregate = report.aggregate.get(metric)
        mean = aggregate.mean if aggregate is not None else 0.0
        per_metric[metric] = round(mean, _PRECISION)
        composite += weight * normalize_metric(mean, scoring.direction.get(metric))
    composite = round(composite / total_weight, _PRECISION) if total_weight else 0.0

    results_by_case = {result.case_id: result for result in report.results}
    total_cases = len(cases)
    categories: dict[str, list[float]] = {}
    for case in cases:
        category = case_category(case, scoring.category_field)
        result = results_by_case.get(case.id)
        categories.setdefault(category, []).append(result.score if result else 0.0)

    per_category = [
        CategoryScore(
            category=category,
            score=round(fmean(scores), _PRECISION),
            weight=round(len(scores) / total_cases, _PRECISION) if total_cases else 0.0,
            case_count=len(scores),
        )
        for category, scores in sorted(categories.items())
    ]

    passed = sum(1 for r in report.results if r.passed and r.error is None)
    errored = sum(1 for r in report.results if r.error is not None)
    return BenchmarkScore(
        composite=composite,
        per_metric=per_metric,
        per_category=per_category,
        total_cases=total_cases,
        passed=passed,
        errored=errored,
    )
