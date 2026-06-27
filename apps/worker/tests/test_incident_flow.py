"""End-to-end incident worker flow tests (F17) — deterministic doubles, no network."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from forge_board import InMemoryBoardService
from forge_contracts.enums import IncidentSeverity, IncidentState
from forge_contracts.incident import (
    BlastRadius,
    ContextFinding,
    ImpactAssessment,
    IncidentSnapshot,
    Runbook,
    RunbookStep,
    StepResult,
)
from forge_worker.tasks.incident import (
    IncidentFlowState,
    execute_runbook,
    incident_directives,
    run_post_approval_phase,
    run_pre_approval_phase,
)

DIRECTIVES = incident_directives()


class FakeIncidentAgent:
    """A scripted :class:`IncidentAgentPort` double."""

    def __init__(self, *, steps: list[RunbookStep] | None = None) -> None:
        self._steps = steps or [
            RunbookStep(id="s1", order=1, title="tail logs", action="read_logs"),
            RunbookStep(id="s2", order=2, title="check metrics", action="query_metrics"),
        ]

    async def gather_context(self, *, incident_id, repo_id, knowledge_scope):
        return [
            ContextFinding(kind="log", summary="5xx spike on checkout-api", source="mcp://datadog")
        ]

    async def assess_impact(self, *, incident_id, findings):
        return ImpactAssessment(
            blast_radius=BlastRadius.LOW,
            affected_services=["checkout-api"],
            user_impact="8% of checkout requests failing",
            severity_recommendation=IncidentSeverity.HIGH,
        )

    async def propose_remediation(self, *, incident_id, attempt, assessment, findings):
        return Runbook(incident_id=incident_id, attempt=attempt, steps=self._steps)


class FakeExecutor:
    """A :class:`RunbookExecutor` double; records executed steps."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.executed: list[str] = []
        self._fail = fail

    async def execute_step(self, step, *, incident_id, directives, policy):
        self.executed.append(step.id)
        status = "failed" if step.id == self._fail else "succeeded"
        return StepResult(step_id=step.id, status=status, summary=f"ran {step.action}")


class FakeMonitor:
    def __init__(self, *, recovered: bool = True) -> None:
        self._recovered = recovered

    async def check_recovery(self, *, incident_id, assessment):
        from forge_contracts.incident import RecoveryStatus

        return RecoveryStatus(recovered=self._recovered)


def _snapshot(incident_id: uuid.UUID) -> IncidentSnapshot:
    return IncidentSnapshot(
        id=incident_id,
        key="INC-1",
        project_id=uuid.uuid4(),
        title="Checkout 5xx",
        severity=IncidentSeverity.HIGH,
        state=IncidentState.RESOLVED,
    )


def test_alert_to_postmortem_end_to_end() -> None:
    incident_id = uuid.uuid4()
    state = IncidentFlowState(incident_id=incident_id, lifecycle_state="incident_created")

    state = asyncio.run(
        run_pre_approval_phase(state, agent=FakeIncidentAgent(), directives=DIRECTIVES)
    )
    assert state.lifecycle_state == "awaiting_approval"

    # Human approval gate.
    from forge_workflow import drive_incident

    state.context["approval_granted"] = True
    outcome = drive_incident(
        state.lifecycle_state, "remediation_approved", context=dict(state.context)
    )
    state.lifecycle_state = outcome.to_state
    assert state.lifecycle_state == "executing_runbook"

    board = InMemoryBoardService()
    state, postmortem, tasks = asyncio.run(
        run_post_approval_phase(
            state,
            executor=FakeExecutor(),
            monitor=FakeMonitor(recovered=True),
            directives=DIRECTIVES,
            incident=_snapshot(incident_id),
            board=board,
        )
    )
    assert state.lifecycle_state == "postmortem_created"
    assert postmortem is not None
    assert postmortem.timeline
    assert postmortem.root_cause
    assert len(tasks) >= 1
    # Follow-up tasks really landed on the board.
    assert len(board.list_tasks()) == len(tasks)


def test_forbidden_remediation_escalates_pre_approval() -> None:
    incident_id = uuid.uuid4()
    state = IncidentFlowState(incident_id=incident_id, lifecycle_state="incident_created")
    bad_agent = FakeIncidentAgent(
        steps=[RunbookStep(id="bad", order=1, title="deploy", action="deploy_prod")]
    )
    state = asyncio.run(
        run_pre_approval_phase(state, agent=bad_agent, directives=DIRECTIVES)
    )
    assert state.lifecycle_state == "needs_human_input"


def test_execute_runbook_rechecks_every_step() -> None:
    """AC10: a forbidden step is never executed even if it reached execution."""
    incident_id = uuid.uuid4()
    runbook = Runbook(
        incident_id=incident_id,
        steps=[
            RunbookStep(id="ok", order=1, title="logs", action="read_logs"),
            RunbookStep(id="bad", order=2, title="deploy", action="deploy_prod"),
        ],
    )
    executor = FakeExecutor()
    event, _results = asyncio.run(
        execute_runbook(
            executor, incident_id=incident_id, runbook=runbook, directives=DIRECTIVES
        )
    )
    assert event == "runbook_step_failed"
    # The forbidden step was never executed (stopped at the re-check).
    assert "bad" not in executor.executed


def test_runbook_step_failure_reports_failure() -> None:
    incident_id = uuid.uuid4()
    runbook = Runbook(
        incident_id=incident_id,
        steps=[RunbookStep(id="s1", order=1, title="logs", action="read_logs")],
    )
    executor = FakeExecutor(fail="s1")
    event, _results = asyncio.run(
        execute_runbook(
            executor, incident_id=incident_id, runbook=runbook, directives=DIRECTIVES
        )
    )
    assert event == "runbook_step_failed"


def test_celery_incident_task_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge_worker.tasks.incident.run_incident_diagnosis" in celery_app.tasks


@pytest.mark.parametrize("recovered", [True, False])
def test_monitor_recovery_paths(recovered: bool) -> None:
    from forge_worker.tasks.incident import monitor_recovery

    event, _status = asyncio.run(
        monitor_recovery(
            FakeMonitor(recovered=recovered),
            incident_id=uuid.uuid4(),
            assessment=ImpactAssessment(),
        )
    )
    assert event == ("recovery_confirmed" if recovered else "recovery_failed")
