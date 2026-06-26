"""Golden task set, retrieval metrics, and quality evaluation harness.

Task 0.6 ships the dependency-light *scaffold* (golden-case model + loader,
metric primitives, runner, scorecard, text report). Tasks 1.4 and 1.16 fill in
the real golden sets and wire the harness to the live hybrid pipeline.
"""

from __future__ import annotations

from forge_eval.golden import GoldenCase, load_golden_set, parse_golden_cases
from forge_eval.metrics import (
    average_precision,
    hit_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from forge_eval.report import format_scorecard
from forge_eval.runner import (
    CaseResult,
    RetrieveFn,
    Scorecard,
    evaluate_retrieval,
    run_golden_eval,
)

__version__ = "0.1.0"

__all__ = [
    "CaseResult",
    "GoldenCase",
    "RetrieveFn",
    "Scorecard",
    "average_precision",
    "evaluate_retrieval",
    "format_scorecard",
    "hit_at_k",
    "load_golden_set",
    "parse_golden_cases",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "run_golden_eval",
]
