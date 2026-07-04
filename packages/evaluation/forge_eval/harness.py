"""Golden-task evaluation harness + regression gate (Task 1.16).

The harness is provider-agnostic: it takes golden tasks and a caller-supplied
``solve_fn`` (the real agent / RAG pipeline in production, a deterministic fake
in tests — no I/O or network here) and produces a :class:`TaskScorecard` scoring
three dimensions per task:

1. **spec-requirement satisfaction** — fraction of the task's expected
   requirements the run satisfied (spec: "spec requirement satisfaction rate");
2. **retrieval recall** — recall@k of expected chunks, when the task declares any;
3. **correctness** — terminal status match and verification-check coverage.

A task *passes* only when all applicable dimensions clear their per-task
thresholds. The scorecard then enforces an aggregate **regression gate** on the
pass rate and the mean requirement-satisfaction rate so regressions block merge.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean

from forge_eval.metrics import recall_at_k, requirement_satisfaction
from forge_eval.tasks import GoldenTask, load_golden_tasks

__all__ = [
    "SolveFn",
    "TaskEval",
    "TaskOutput",
    "TaskScorecard",
    "evaluate_tasks",
    "reference_solver",
    "run_task_harness",
]


@dataclass
class TaskOutput:
    """The output a pipeline produces for a single golden task.

    All fields default to empty so a pipeline that produces nothing still yields
    a well-formed (zero-scoring) output rather than raising.
    """

    satisfied_requirements: list[str] = field(default_factory=list)
    retrieved_ids: list[str] = field(default_factory=list)
    status: str | None = None
    checks_passed: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


#: A solver maps a golden task to the output a pipeline produced for it.
SolveFn = Callable[[GoldenTask], TaskOutput]


@dataclass
class TaskEval:
    """Per-task scoring across every applicable dimension."""

    task_id: str
    kind: str
    requirement_satisfaction: float
    retrieval_recall: float | None
    status_match: bool
    checks_satisfied: bool
    passed: bool


@dataclass
class TaskScorecard:
    """Aggregate task-harness result with a regression-threshold gate."""

    k: int
    min_requirement_satisfaction: float
    min_recall: float
    pass_rate_threshold: float
    satisfaction_rate_threshold: float
    results: list[TaskEval] = field(default_factory=list)

    @property
    def num_tasks(self) -> int:
        return len(self.results)

    @property
    def num_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.num_passed / self.num_tasks if self.results else 0.0

    @property
    def mean_requirement_satisfaction(self) -> float:
        return (
            fmean(r.requirement_satisfaction for r in self.results)
            if self.results
            else 0.0
        )

    @property
    def mean_retrieval_recall(self) -> float | None:
        scored = [r.retrieval_recall for r in self.results if r.retrieval_recall is not None]
        return fmean(scored) if scored else None

    def by_kind(self) -> dict[str, int]:
        """Count of tasks per kind (golden-set coverage breakdown)."""
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.kind] = counts.get(r.kind, 0) + 1
        return counts

    @property
    def passed(self) -> bool:
        """Regression gate: pass rate AND mean satisfaction must clear thresholds."""
        return (
            self.num_tasks > 0
            and self.pass_rate >= self.pass_rate_threshold
            and self.mean_requirement_satisfaction >= self.satisfaction_rate_threshold
        )

    def assert_threshold(self) -> None:
        """Raise ``AssertionError`` if the regression gate is not met."""
        if self.num_tasks == 0:
            raise AssertionError("regression gate: no golden tasks evaluated")
        if self.pass_rate < self.pass_rate_threshold:
            raise AssertionError(
                f"task pass rate regression: {self.pass_rate:.3f} "
                f"< threshold {self.pass_rate_threshold:.3f} over {self.num_tasks} task(s)"
            )
        if self.mean_requirement_satisfaction < self.satisfaction_rate_threshold:
            raise AssertionError(
                f"requirement satisfaction regression: "
                f"{self.mean_requirement_satisfaction:.3f} "
                f"< threshold {self.satisfaction_rate_threshold:.3f} "
                f"over {self.num_tasks} task(s)"
            )


def _score_task(
    task: GoldenTask,
    output: TaskOutput,
    *,
    k: int,
    min_requirement_satisfaction: float,
    min_recall: float,
) -> TaskEval:
    satisfaction = requirement_satisfaction(
        output.satisfied_requirements, task.requirement_ids
    )

    if task.expected_chunks:
        recall: float | None = recall_at_k(output.retrieved_ids, task.expected_chunks, k)
    else:
        recall = None

    status_match = output.status == task.expected_status
    checks_satisfied = set(task.expected_checks) <= set(output.checks_passed)

    passed = (
        satisfaction >= min_requirement_satisfaction
        and status_match
        and checks_satisfied
        and (recall is None or recall >= min_recall)
    )

    return TaskEval(
        task_id=task.id,
        kind=task.kind,
        requirement_satisfaction=satisfaction,
        retrieval_recall=recall,
        status_match=status_match,
        checks_satisfied=checks_satisfied,
        passed=passed,
    )


def evaluate_tasks(
    tasks: Sequence[GoldenTask],
    solve_fn: SolveFn,
    *,
    k: int = 10,
    min_requirement_satisfaction: float = 1.0,
    min_recall: float = 0.0,
    pass_rate_threshold: float = 0.0,
    satisfaction_rate_threshold: float = 0.0,
) -> TaskScorecard:
    """Run ``solve_fn`` over ``tasks`` and score the results."""
    results = [
        _score_task(
            task,
            solve_fn(task),
            k=k,
            min_requirement_satisfaction=min_requirement_satisfaction,
            min_recall=min_recall,
        )
        for task in tasks
    ]
    return TaskScorecard(
        k=k,
        min_requirement_satisfaction=min_requirement_satisfaction,
        min_recall=min_recall,
        pass_rate_threshold=pass_rate_threshold,
        satisfaction_rate_threshold=satisfaction_rate_threshold,
        results=results,
    )


def run_task_harness(
    path: str | Path,
    solve_fn: SolveFn,
    *,
    k: int = 10,
    min_requirement_satisfaction: float = 1.0,
    min_recall: float = 0.0,
    pass_rate_threshold: float = 0.0,
    satisfaction_rate_threshold: float = 0.0,
) -> TaskScorecard:
    """Convenience: load a golden task set from ``path`` then evaluate it."""
    tasks = load_golden_tasks(path)
    return evaluate_tasks(
        tasks,
        solve_fn,
        k=k,
        min_requirement_satisfaction=min_requirement_satisfaction,
        min_recall=min_recall,
        pass_rate_threshold=pass_rate_threshold,
        satisfaction_rate_threshold=satisfaction_rate_threshold,
    )


def reference_solver(task: GoldenTask) -> TaskOutput:
    """A deterministic, network-free baseline stand-in for the real pipeline.

    It models a competent-but-not-perfect agent: it satisfies every *core*
    requirement, surfaces the expected retrieval chunks, reaches the expected
    terminal status, and runs the expected checks — but it does **not** solve
    ``stretch`` requirements. This is a demonstration baseline (so the harness is
    runnable without a live model), *not* a fake test pass: the harness still
    computes every metric honestly, and stretch goals genuinely score below 1.0.
    """
    return TaskOutput(
        satisfied_requirements=list(task.core_requirement_ids),
        retrieved_ids=list(task.expected_chunks),
        status=task.expected_status,
        checks_passed=list(task.expected_checks),
    )
