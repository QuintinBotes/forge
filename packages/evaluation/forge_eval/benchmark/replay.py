"""Deterministic, offline replay of submission bundles (F35 verification core).

A :class:`ReplayBundle` records the *ordered outputs* a submitter's pipeline
produced for one frozen case. Replay re-derives every rubric metric from those
outputs against the frozen ground truth using the in-tree ``forge_eval.metrics``
primitives — zero model calls, zero network, identical results across runs
(AC11). The bundle ``content_hash`` binds the recorded outputs, so tampering
with either the outputs or the claimed score is detectable (AC9/AC10).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from statistics import fmean

from forge_eval.benchmark.models import (
    BenchmarkCaseResult,
    BenchmarkReport,
    BenchmarkScoring,
    MetricAggregate,
    ReplayBundle,
)
from forge_eval.benchmark.scoring import normalize_metric
from forge_eval.golden import GoldenCase
from forge_eval.metrics import (
    average_precision,
    hit_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

__all__ = ["METRIC_REGISTRY", "compute_bundle_hash", "make_bundle", "replay_bundles"]

#: Dotted metric name -> pure scorer over (retrieved_ids, expected_ids, k).
METRIC_REGISTRY: dict[str, Callable[[Sequence[str], Sequence[str], int], float]] = {
    "retrieval.recall_at_k": lambda out, exp, k: recall_at_k(out, exp, k),
    "retrieval.precision_at_k": lambda out, exp, k: precision_at_k(out, exp, k),
    "retrieval.hit_at_k": lambda out, exp, k: 1.0 if hit_at_k(out, exp, k) else 0.0,
    "retrieval.mrr": lambda out, exp, _k: reciprocal_rank(out, exp),
    "retrieval.average_precision": lambda out, exp, _k: average_precision(out, exp),
    # Task-style cases record the satisfied requirement ids as their outputs;
    # set overlap against the expected ids is the satisfaction rate.
    "agent.requirement_satisfaction_rate": lambda out, exp, _k: (
        len(set(exp) & set(out)) / len(set(exp)) if exp else 1.0
    ),
}


def compute_bundle_hash(case_id: str, output_ids: Sequence[str]) -> str:
    """``sha256:<hex>`` over the canonical JSON of the bundle body."""
    canonical = json.dumps(
        {"case_id": case_id, "output_ids": list(output_ids)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_bundle(case_id: str, output_ids: Sequence[str]) -> ReplayBundle:
    """Build a bundle with its content hash stamped."""
    return ReplayBundle(
        case_id=case_id,
        output_ids=list(output_ids),
        content_hash=compute_bundle_hash(case_id, output_ids),
    )


def _score_case(
    case: GoldenCase, bundle: ReplayBundle, scoring: BenchmarkScoring
) -> BenchmarkCaseResult:
    metrics: dict[str, float] = {}
    weighted: list[tuple[float, float]] = []
    for metric, weight in scoring.metric_weights.items():
        scorer = METRIC_REGISTRY.get(metric)
        raw = scorer(bundle.output_ids, case.expected_ids, scoring.k) if scorer else 0.0
        metrics[metric] = raw
        weighted.append((weight, normalize_metric(raw, scoring.direction.get(metric))))
    total = sum(w for w, _ in weighted)
    score = sum(w * x for w, x in weighted) / total if total else 0.0
    return BenchmarkCaseResult(
        case_id=case.id,
        score=round(score, 6),
        metrics={m: round(v, 6) for m, v in metrics.items()},
        passed=score > 0.0,
    )


def replay_bundles(
    bundles: Sequence[ReplayBundle],
    cases: Sequence[GoldenCase],
    scoring: BenchmarkScoring,
) -> BenchmarkReport:
    """Re-derive a full report from recorded bundles. Pure and deterministic.

    Every frozen case is scored exactly once; a case with no bundle is an
    errored zero-score result (a submission cannot gain by omitting cases).
    """
    by_case: dict[str, ReplayBundle] = {b.case_id: b for b in bundles}
    results: list[BenchmarkCaseResult] = []
    for case in cases:
        bundle = by_case.get(case.id)
        if bundle is None:
            results.append(
                BenchmarkCaseResult(
                    case_id=case.id,
                    score=0.0,
                    metrics=dict.fromkeys(scoring.metric_weights, 0.0),
                    passed=False,
                    error="missing replay bundle",
                )
            )
            continue
        results.append(_score_case(case, bundle, scoring))

    aggregate = {
        metric: MetricAggregate(
            mean=round(fmean(r.metrics.get(metric, 0.0) for r in results), 6) if results else 0.0,
            count=len(results),
        )
        for metric in scoring.metric_weights
    }
    return BenchmarkReport(aggregate=aggregate, results=results)
