"""Golden task set, retrieval metrics, and quality evaluation harness.

Task 0.6 ships the dependency-light *scaffold* (golden-case model + loader,
metric primitives, runner, scorecard, text report). Tasks 1.4 and 1.16 fill in
the real golden sets and wire the harness to the live hybrid pipeline.
"""

from __future__ import annotations

from forge_eval.golden import GoldenCase, load_golden_set, parse_golden_cases
from forge_eval.harness import (
    SolveFn,
    TaskEval,
    TaskOutput,
    TaskScorecard,
    evaluate_tasks,
    reference_solver,
    run_task_harness,
)
from forge_eval.metrics import (
    average_precision,
    hit_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    requirement_satisfaction,
)
from forge_eval.report import format_scorecard, format_task_scorecard
from forge_eval.runner import (
    CaseResult,
    RetrieveFn,
    Scorecard,
    evaluate_retrieval,
    run_golden_eval,
)
from forge_eval.tasks import (
    GoldenRequirement,
    GoldenTask,
    load_golden_tasks,
    parse_golden_tasks,
)

__version__ = "0.1.0"

__all__ = [
    "CaseResult",
    "GoldenCase",
    "GoldenRequirement",
    "GoldenTask",
    "RetrieveFn",
    "Scorecard",
    "SolveFn",
    "TaskEval",
    "TaskOutput",
    "TaskScorecard",
    "average_precision",
    "evaluate_retrieval",
    "evaluate_tasks",
    "format_scorecard",
    "format_task_scorecard",
    "hit_at_k",
    "load_golden_set",
    "load_golden_tasks",
    "parse_golden_cases",
    "parse_golden_tasks",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "reference_solver",
    "requirement_satisfaction",
    "run_golden_eval",
    "run_task_harness",
]
