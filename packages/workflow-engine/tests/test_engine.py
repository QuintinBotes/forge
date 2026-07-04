"""Tests for the workflow engine FSM driver (plan Task 1.8).

These are the headline scenarios from the plan:

- happy path: ``created -> ... -> merged`` (-> closed),
- ``checks_failed`` consumes the retry budget then escalates to
  ``needs_human_input``,
- the engine conforms to the frozen ``WorkflowEngine`` Protocol.
"""

from __future__ import annotations

import uuid

import pytest

from forge_contracts import RunStatus, WorkflowEngine, WorkflowRun, WorkflowState
from forge_workflow.engine import RETRY_COUNT_KEY, WorkflowEngineImpl
from forge_workflow.exceptions import InvalidTransitionError, WorkflowRunNotFoundError
from forge_workflow.store import InMemoryWorkflowStore


@pytest.fixture
def engine() -> WorkflowEngineImpl:
    return WorkflowEngineImpl(store=InMemoryWorkflowStore())


def test_engine_satisfies_protocol(engine: WorkflowEngineImpl) -> None:
    assert isinstance(engine, WorkflowEngine)


def test_start_creates_run_in_created_state(engine: WorkflowEngineImpl) -> None:
    task_id = uuid.uuid4()
    run = engine.start(task_id)
    assert isinstance(run, WorkflowRun)
    assert run.id is not None
    assert run.task_id == task_id
    assert run.current_state == WorkflowState.CREATED.value
    assert run.workflow_name == "default_feature"
    assert run.status == RunStatus.RUNNING
    assert run.started_at is not None


def test_happy_path_to_merged(engine: WorkflowEngineImpl) -> None:
    run = engine.start(uuid.uuid4())
    rid = run.id
    assert rid is not None

    chain = [
        ("generate_spec_draft", WorkflowState.SPEC_DRAFTING),
        ("gather_clarifications", WorkflowState.CLARIFICATION),
        ("submit_spec_for_review", WorkflowState.SPEC_REVIEW),
        ("spec_approved_by_human", WorkflowState.SPEC_APPROVED),
        ("generate_plan", WorkflowState.PLAN_DRAFTING),
        ("submit_plan_for_review", WorkflowState.PLAN_REVIEW),
        ("plan_approved_by_human", WorkflowState.TASK_GENERATION),
        ("generate_tasks", WorkflowState.TASK_READY),
        ("start_agent_run", WorkflowState.EXECUTING),
        ("run_checks", WorkflowState.VERIFYING),
        ("all_checks_passed", WorkflowState.PR_OPENED),
        ("request_reviews", WorkflowState.AWAITING_REVIEW),
    ]
    for event, expected in chain:
        state = engine.transition(rid, event)
        assert state == expected, f"event {event} -> {state}, expected {expected}"

    # The merge gate is an AND of three external signals.
    engine.update_context(rid, {"ci_status_green": True, "spec_validated": True})
    assert engine.transition(rid, "review_approved_by_human") == WorkflowState.MERGED

    merged = engine.get_run(rid)
    assert merged.status == RunStatus.SUCCEEDED
    assert merged.completed_at is not None

    # ... and on to closed.
    assert engine.transition(rid, "close_task") == WorkflowState.CLOSED


def test_checks_failed_retries_then_escalates(engine: WorkflowEngineImpl) -> None:
    run = engine.start(uuid.uuid4())
    rid = run.id
    assert rid is not None

    # Drive to the verifying state.
    for event in (
        "generate_spec_draft",
        "gather_clarifications",
        "submit_spec_for_review",
        "spec_approved_by_human",
        "generate_plan",
        "submit_plan_for_review",
        "plan_approved_by_human",
        "generate_tasks",
        "start_agent_run",
        "run_checks",
    ):
        engine.transition(rid, event)
    assert engine.get_run(rid).current_state == WorkflowState.VERIFYING.value

    # max_retries == 3: three failures loop back to executing (and re-verify)...
    for expected_retry in (1, 2, 3):
        assert engine.transition(rid, "checks_failed") == WorkflowState.EXECUTING
        stored = engine.get_run(rid)
        assert stored.context[RETRY_COUNT_KEY] == expected_retry
        assert engine.transition(rid, "run_checks") == WorkflowState.VERIFYING

    # ...the fourth failure exhausts the budget and escalates to a human.
    assert engine.transition(rid, "checks_failed") == WorkflowState.NEEDS_HUMAN_INPUT
    escalated = engine.get_run(rid)
    assert escalated.status == RunStatus.ESCALATED


def test_invalid_event_raises(engine: WorkflowEngineImpl) -> None:
    run = engine.start(uuid.uuid4())
    assert run.id is not None
    with pytest.raises(InvalidTransitionError):
        engine.transition(run.id, "not_a_real_event")


def test_transition_unknown_run_raises(engine: WorkflowEngineImpl) -> None:
    with pytest.raises(WorkflowRunNotFoundError):
        engine.transition(uuid.uuid4(), "generate_spec_draft")


def test_should_escalate_uses_confidence_threshold(engine: WorkflowEngineImpl) -> None:
    # Spec escalation_policy.confidence_threshold == 0.72.
    assert engine.should_escalate(0.5) is True
    assert engine.should_escalate(0.72) is False
    assert engine.should_escalate(0.9) is False


def test_load_definition_round_trips(engine: WorkflowEngineImpl) -> None:
    from forge_workflow.default_workflow import DEFAULT_FEATURE_WORKFLOW_YAML

    definition = engine.load_definition(DEFAULT_FEATURE_WORKFLOW_YAML)
    assert definition.name == "default_feature"
