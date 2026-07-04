"""Golden-set evaluation runner + scorecard.

The runner is provider-agnostic: it takes golden cases and a caller-supplied
``retrieve_fn`` (the real hybrid pipeline in production, a deterministic fake in
tests) and produces a :class:`Scorecard` with per-case and aggregate metrics
plus a regression-threshold gate. No I/O or network happens here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean

from forge_eval.golden import GoldenCase, load_golden_set
from forge_eval.metrics import hit_at_k, precision_at_k, recall_at_k, reciprocal_rank

__all__ = ["CaseResult", "RetrieveFn", "Scorecard", "evaluate_retrieval", "run_golden_eval"]

#: A retrieval function maps a golden case to an *ordered* list of result ids.
RetrieveFn = Callable[[GoldenCase], Sequence[str]]


@dataclass
class CaseResult:
    """Per-case scoring for a single golden case."""

    case_id: str
    retrieved_ids: list[str]
    recall_at_k: float
    precision_at_k: float
    reciprocal_rank: float
    hit: bool
    passed: bool


@dataclass
class Scorecard:
    """Aggregate evaluation result with a regression-threshold gate."""

    k: int
    recall_threshold: float
    results: list[CaseResult] = field(default_factory=list)

    @property
    def num_cases(self) -> int:
        return len(self.results)

    @property
    def num_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def mean_recall_at_k(self) -> float:
        return fmean(r.recall_at_k for r in self.results) if self.results else 0.0

    @property
    def mean_precision_at_k(self) -> float:
        return fmean(r.precision_at_k for r in self.results) if self.results else 0.0

    @property
    def mean_mrr(self) -> float:
        return fmean(r.reciprocal_rank for r in self.results) if self.results else 0.0

    @property
    def hit_rate(self) -> float:
        return fmean(1.0 if r.hit else 0.0 for r in self.results) if self.results else 0.0

    @property
    def passed(self) -> bool:
        """Regression gate: mean recall@k must meet the configured threshold."""
        return self.num_cases > 0 and self.mean_recall_at_k >= self.recall_threshold

    def assert_threshold(self) -> None:
        """Raise ``AssertionError`` if the gate is not met (CI regression guard)."""
        if not self.passed:
            raise AssertionError(
                f"recall@{self.k} regression: mean {self.mean_recall_at_k:.3f} "
                f"< threshold {self.recall_threshold:.3f} over {self.num_cases} case(s)"
            )


def evaluate_retrieval(
    cases: Sequence[GoldenCase],
    retrieve_fn: RetrieveFn,
    *,
    k: int = 10,
    recall_threshold: float = 0.0,
) -> Scorecard:
    """Run ``retrieve_fn`` over ``cases`` and score the results."""
    results: list[CaseResult] = []
    for case in cases:
        retrieved = list(retrieve_fn(case))
        relevant = case.expected_ids
        recall = recall_at_k(retrieved, relevant, k)
        results.append(
            CaseResult(
                case_id=case.id,
                retrieved_ids=retrieved,
                recall_at_k=recall,
                precision_at_k=precision_at_k(retrieved, relevant, k),
                reciprocal_rank=reciprocal_rank(retrieved, relevant),
                hit=hit_at_k(retrieved, relevant, k),
                passed=recall >= recall_threshold,
            )
        )
    return Scorecard(k=k, recall_threshold=recall_threshold, results=results)


def run_golden_eval(
    path: str | Path,
    retrieve_fn: RetrieveFn,
    *,
    k: int = 10,
    recall_threshold: float = 0.0,
) -> Scorecard:
    """Convenience: load a golden set from ``path`` then evaluate it."""
    cases = load_golden_set(path)
    return evaluate_retrieval(cases, retrieve_fn, k=k, recall_threshold=recall_threshold)
