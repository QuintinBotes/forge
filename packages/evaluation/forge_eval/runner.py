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
from forge_eval.metrics import (
    hit_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

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
    #: nDCG at ``ndcg_k`` (rank-quality). Additive; defaults to ``0.0`` so older
    #: callers that build a :class:`CaseResult` directly stay source-compatible.
    ndcg_at_k: float = 0.0


@dataclass
class Scorecard:
    """Aggregate evaluation result with a regression-threshold gate."""

    k: int
    recall_threshold: float
    #: nDCG regression floor. ``0.0`` (the default) disables the nDCG leg of the
    #: gate, so pre-existing recall-only scorecards behave exactly as before.
    ndcg_threshold: float = 0.0
    #: The ``k`` nDCG is measured at (may differ from the recall ``k``; the real
    #: eval reports recall@5 but nDCG@10).
    ndcg_k: int = 0
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
    def mean_ndcg_at_k(self) -> float:
        return fmean(r.ndcg_at_k for r in self.results) if self.results else 0.0

    @property
    def hit_rate(self) -> float:
        return fmean(1.0 if r.hit else 0.0 for r in self.results) if self.results else 0.0

    @property
    def passed(self) -> bool:
        """Regression gate: mean recall@k AND mean nDCG must meet their floors.

        The nDCG leg is inert when ``ndcg_threshold == 0.0`` (the default), so a
        recall-only scorecard gates exactly as it always did.
        """
        return (
            self.num_cases > 0
            and self.mean_recall_at_k >= self.recall_threshold
            and self.mean_ndcg_at_k >= self.ndcg_threshold
        )

    def assert_threshold(self) -> None:
        """Raise ``AssertionError`` if the gate is not met (CI regression guard)."""
        if self.num_cases == 0:
            raise AssertionError("scorecard has no cases; nothing to gate")
        if self.mean_recall_at_k < self.recall_threshold:
            raise AssertionError(
                f"recall@{self.k} regression: mean {self.mean_recall_at_k:.3f} "
                f"< threshold {self.recall_threshold:.3f} over {self.num_cases} case(s)"
            )
        if self.mean_ndcg_at_k < self.ndcg_threshold:
            raise AssertionError(
                f"nDCG@{self.ndcg_k or self.k} regression: mean "
                f"{self.mean_ndcg_at_k:.3f} < threshold {self.ndcg_threshold:.3f} "
                f"over {self.num_cases} case(s)"
            )


def evaluate_retrieval(
    cases: Sequence[GoldenCase],
    retrieve_fn: RetrieveFn,
    *,
    k: int = 10,
    ndcg_k: int | None = None,
    recall_threshold: float = 0.0,
    ndcg_threshold: float = 0.0,
) -> Scorecard:
    """Run ``retrieve_fn`` over ``cases`` and score the results.

    ``ndcg_k`` defaults to ``k``; each case's optional graded relevance lives in
    ``case.metadata["gains"]`` (id -> gain) and feeds nDCG when present, else the
    default binary gain of ``1.0`` per relevant id is used.
    """
    ndcg_cutoff = k if ndcg_k is None else ndcg_k
    results: list[CaseResult] = []
    for case in cases:
        retrieved = list(retrieve_fn(case))
        relevant = case.expected_ids
        gains = case.metadata.get("gains") if isinstance(case.metadata, dict) else None
        recall = recall_at_k(retrieved, relevant, k)
        results.append(
            CaseResult(
                case_id=case.id,
                retrieved_ids=retrieved,
                recall_at_k=recall,
                precision_at_k=precision_at_k(retrieved, relevant, k),
                reciprocal_rank=reciprocal_rank(retrieved, relevant),
                ndcg_at_k=ndcg_at_k(retrieved, relevant, ndcg_cutoff, gains=gains),
                hit=hit_at_k(retrieved, relevant, k),
                passed=recall >= recall_threshold,
            )
        )
    return Scorecard(
        k=k,
        recall_threshold=recall_threshold,
        ndcg_threshold=ndcg_threshold,
        ndcg_k=ndcg_cutoff,
        results=results,
    )


def run_golden_eval(
    path: str | Path,
    retrieve_fn: RetrieveFn,
    *,
    k: int = 10,
    ndcg_k: int | None = None,
    recall_threshold: float = 0.0,
    ndcg_threshold: float = 0.0,
) -> Scorecard:
    """Convenience: load a golden set from ``path`` then evaluate it."""
    cases = load_golden_set(path)
    return evaluate_retrieval(
        cases,
        retrieve_fn,
        k=k,
        ndcg_k=ndcg_k,
        recall_threshold=recall_threshold,
        ndcg_threshold=ndcg_threshold,
    )
