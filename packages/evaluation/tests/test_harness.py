"""Unit tests for the golden-task evaluation harness (Task 1.16).

The harness runs a caller-supplied ``solve_fn`` (the real agent/RAG pipeline in
production, a deterministic fake here — no network) over the golden task set and
scores three dimensions: spec-requirement satisfaction, retrieval recall, and
verification-check / status correctness. It emits a :class:`TaskScorecard` and
enforces a regression gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_eval.harness import (
    TaskEval,
    TaskOutput,
    TaskScorecard,
    evaluate_tasks,
    reference_solver,
    run_task_harness,
)
from forge_eval.report import format_task_scorecard
from forge_eval.tasks import GoldenRequirement, GoldenTask, load_golden_tasks

V1_SET = (
    Path(__file__).resolve().parent.parent / "forge_eval" / "golden" / "v1_task_set.yaml"
)


# --------------------------------------------------------------------------- #
# Fake pipelines (deterministic; no network)                                  #
# --------------------------------------------------------------------------- #


def _perfect(task: GoldenTask) -> TaskOutput:
    """A pipeline that produces exactly the known-good output for every task."""
    return TaskOutput(
        satisfied_requirements=list(task.requirement_ids),
        retrieved_ids=list(task.expected_chunks),
        status=task.expected_status,
        checks_passed=list(task.expected_checks),
    )


def _half(task: GoldenTask) -> TaskOutput:
    """Satisfies only the first half of each task's requirements."""
    ids = task.requirement_ids
    keep = ids[: len(ids) // 2]
    return TaskOutput(
        satisfied_requirements=keep,
        retrieved_ids=list(task.expected_chunks),
        status=task.expected_status,
        checks_passed=list(task.expected_checks),
    )


def _broken(task: GoldenTask) -> TaskOutput:
    """A pipeline that produces nothing useful."""
    return TaskOutput()


# --------------------------------------------------------------------------- #
# Harness loads >=30 tasks and runs the fake pipeline                          #
# --------------------------------------------------------------------------- #


def test_harness_loads_at_least_30_tasks_and_runs() -> None:
    tasks = load_golden_tasks(V1_SET)
    assert len(tasks) >= 30
    card = evaluate_tasks(tasks, _perfect)
    assert isinstance(card, TaskScorecard)
    assert card.num_tasks == len(tasks)
    assert all(isinstance(r, TaskEval) for r in card.results)


def test_run_task_harness_from_path() -> None:
    card = run_task_harness(V1_SET, _perfect, pass_rate_threshold=1.0)
    assert card.num_tasks >= 30
    assert card.passed is True


# --------------------------------------------------------------------------- #
# Metrics: requirement satisfaction, recall, pass rate                         #
# --------------------------------------------------------------------------- #


def test_perfect_pipeline_scores_full_marks() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(tasks, _perfect, pass_rate_threshold=1.0)
    assert card.mean_requirement_satisfaction == 1.0
    assert card.pass_rate == 1.0
    assert card.num_passed == card.num_tasks
    assert card.passed is True


def test_half_pipeline_degrades_satisfaction() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(tasks, _half, min_requirement_satisfaction=1.0)
    assert 0.0 < card.mean_requirement_satisfaction < 1.0
    assert card.pass_rate < 1.0


def test_broken_pipeline_fails_everything() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(tasks, _broken, pass_rate_threshold=0.5)
    assert card.mean_requirement_satisfaction == 0.0
    assert card.num_passed == 0
    assert card.passed is False


def test_reference_solver_is_deterministic_and_imperfect() -> None:
    # The shipped reference baseline solves core requirements but not stretch
    # ones, so over a set containing stretch goals it must score below perfect.
    tasks = load_golden_tasks(V1_SET)
    card_a = evaluate_tasks(tasks, reference_solver, min_requirement_satisfaction=1.0)
    card_b = evaluate_tasks(tasks, reference_solver, min_requirement_satisfaction=1.0)
    assert card_a.mean_requirement_satisfaction == card_b.mean_requirement_satisfaction
    assert 0.0 < card_a.mean_requirement_satisfaction <= 1.0
    assert card_a.pass_rate < 1.0  # at least one stretch task is not fully solved


# --------------------------------------------------------------------------- #
# Regression gate                                                              #
# --------------------------------------------------------------------------- #


def test_regression_gate_passes_when_thresholds_met() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(
        tasks,
        _perfect,
        pass_rate_threshold=1.0,
        satisfaction_rate_threshold=1.0,
    )
    card.assert_threshold()  # must not raise


def test_regression_gate_raises_on_pass_rate_regression() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(tasks, _broken, pass_rate_threshold=0.9)
    with pytest.raises(AssertionError, match="pass rate"):
        card.assert_threshold()


def test_regression_gate_raises_on_satisfaction_regression() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(tasks, _half, satisfaction_rate_threshold=0.95)
    with pytest.raises(AssertionError, match="satisfaction"):
        card.assert_threshold()


def test_empty_scorecard_does_not_pass_gate() -> None:
    card = evaluate_tasks([], _perfect, pass_rate_threshold=0.0)
    assert card.num_tasks == 0
    assert card.passed is False


# --------------------------------------------------------------------------- #
# Per-task scoring dimensions                                                  #
# --------------------------------------------------------------------------- #


def test_status_mismatch_fails_task_even_with_satisfied_requirements() -> None:
    task = GoldenTask(
        id="T",
        objective="x",
        kind="feature",
        requirements=[GoldenRequirement(id="R1")],
        expected_status="done",
    )

    def solver(_: GoldenTask) -> TaskOutput:
        return TaskOutput(satisfied_requirements=["R1"], status="failed")

    card = evaluate_tasks([task], solver, min_requirement_satisfaction=1.0)
    result = card.results[0]
    assert result.requirement_satisfaction == 1.0
    assert result.status_match is False
    assert result.passed is False


def test_missing_check_fails_task() -> None:
    task = GoldenTask(
        id="T",
        objective="x",
        kind="feature",
        requirements=[GoldenRequirement(id="R1")],
        expected_status="done",
        expected_checks=["lint", "tests"],
    )

    def solver(_: GoldenTask) -> TaskOutput:
        return TaskOutput(
            satisfied_requirements=["R1"], status="done", checks_passed=["lint"]
        )

    card = evaluate_tasks([task], solver, min_requirement_satisfaction=1.0)
    assert card.results[0].checks_satisfied is False
    assert card.results[0].passed is False


def test_retrieval_recall_dimension_is_scored() -> None:
    task = GoldenTask(
        id="T",
        objective="find auth",
        kind="feature",
        expected_chunks=["c1", "c2"],
        expected_status="done",
    )

    def solver(_: GoldenTask) -> TaskOutput:
        return TaskOutput(retrieved_ids=["c1", "x"], status="done")

    card = evaluate_tasks([task], solver, k=10, min_recall=0.75)
    result = card.results[0]
    assert result.retrieval_recall == 0.5
    assert result.passed is False  # 0.5 recall < 0.75 threshold


def test_retrieval_recall_is_none_when_no_expected_chunks() -> None:
    task = GoldenTask(
        id="T",
        objective="x",
        kind="feature",
        requirements=[GoldenRequirement(id="R1")],
        expected_status="done",
    )
    card = evaluate_tasks([task], _perfect, min_requirement_satisfaction=1.0)
    assert card.results[0].retrieval_recall is None
    assert card.results[0].passed is True


# --------------------------------------------------------------------------- #
# Breakdown + reporting                                                        #
# --------------------------------------------------------------------------- #


def test_by_kind_breakdown_covers_all_tasks() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(tasks, _perfect)
    by_kind = card.by_kind()
    assert sum(by_kind.values()) == card.num_tasks
    assert all(count > 0 for count in by_kind.values())


def test_format_task_scorecard_is_readable() -> None:
    tasks = load_golden_tasks(V1_SET)
    card = evaluate_tasks(tasks, _perfect, pass_rate_threshold=1.0)
    report = format_task_scorecard(card)
    assert "requirement satisfaction" in report.lower()
    assert "pass rate" in report.lower()
    assert "PASS" in report
