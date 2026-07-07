"""Incident workflow effect bodies (F17, dedicated ``incident`` Celery queue).

These are the effect bodies the ``incident`` workflow definition dispatches by
name. They are written as pure, framework-free async functions (decoupled from
the API service and from Celery) so they are unit-testable with deterministic
doubles and no live network. Each returns the FSM event the run should advance
on, so a driver (Celery dispatcher in prod; the test harness here) feeds it into
``forge_workflow.drive_incident``.

The catastrophic failure mode — an incident agent mutating production — is
prevented twice: ``propose_remediation`` validates the proposal against the
``incident-response`` blast-radius posture, and ``execute_runbook`` **re-checks
every step at execution time** against the skill directives + blast cap (defense
in depth), so a stale approval or a changed policy can never execute an over-blast
step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from forge_board.incidents import (
    TemplatePostmortemComposer,
    assert_runbook_within_policy,
    create_action_item_tasks,
)
from forge_board.incidents.actions import TaskCreator
from forge_contracts import TaskDTO
from forge_contracts.incident import (
    ContextFinding,
    ImpactAssessment,
    IncidentAgentPort,
    IncidentEventDTO,
    IncidentSnapshot,
    Postmortem,
    PostmortemComposer,
    RecoveryMonitor,
    Runbook,
    RunbookExecutor,
    StepResult,
)
from forge_skill import (
    SkillDirectives,
    SkillProfileRegistry,
    blast_within,
    skill_permits_action,
    to_directives,
)
from forge_worker.celery_app import celery_app

__all__ = [
    "INCIDENT_QUEUE",
    "assess_incident_impact",
    "execute_runbook",
    "gather_incident_context",
    "generate_postmortem",
    "incident_directives",
    "monitor_recovery",
    "propose_remediation",
    "register_incident_queue",
    "run_post_approval_phase",
    "run_pre_approval_phase",
]

INCIDENT_QUEUE = "incident"


def incident_directives() -> SkillDirectives:
    """The resolved ``incident-response`` directives (read-only/low-blast floor)."""
    return to_directives(SkillProfileRegistry().get("incident-response"))


# --------------------------------------------------------------------------- #
# Effect bodies (pure async)                                                   #
# --------------------------------------------------------------------------- #


async def gather_incident_context(
    agent: IncidentAgentPort,
    *,
    incident_id: uuid.UUID,
    repo_id: str | None = None,
    knowledge_scope: dict | None = None,
) -> tuple[str, list[ContextFinding]]:
    """Run read-only diagnosis; return ``context_gathered`` + the findings."""
    findings = await agent.gather_context(
        incident_id=incident_id, repo_id=repo_id, knowledge_scope=knowledge_scope or {}
    )
    return "context_gathered", findings


async def assess_incident_impact(
    agent: IncidentAgentPort,
    *,
    incident_id: uuid.UUID,
    findings: list[ContextFinding],
) -> tuple[str, ImpactAssessment]:
    """Produce an :class:`ImpactAssessment`; return the ``impact_assessed`` event."""
    assessment = await agent.assess_impact(incident_id=incident_id, findings=findings)
    return "impact_assessed", assessment


async def propose_remediation(
    agent: IncidentAgentPort,
    *,
    incident_id: uuid.UUID,
    attempt: int,
    assessment: ImpactAssessment,
    findings: list[ContextFinding],
    directives: SkillDirectives,
) -> tuple[str, Runbook, list[str]]:
    """Propose a runbook and validate it against the blast-radius posture.

    Returns ``remediation_proposed`` when the plan is within posture, else
    ``remediation_blast_radius_exceeded`` with the offending step ids.
    """
    runbook = await agent.propose_remediation(
        incident_id=incident_id, attempt=attempt, assessment=assessment, findings=findings
    )
    offending = assert_runbook_within_policy(runbook, directives)
    event = "remediation_proposed" if not offending else "remediation_blast_radius_exceeded"
    return event, runbook, offending


async def execute_runbook(
    executor: RunbookExecutor,
    *,
    incident_id: uuid.UUID,
    runbook: Runbook,
    directives: SkillDirectives,
    policy: object | None = None,
) -> tuple[str, list[StepResult]]:
    """Execute the approved runbook, re-checking every step (defense in depth)."""
    results: list[StepResult] = []
    for step in runbook.steps:
        decision = skill_permits_action(directives, step.action)
        if not decision.allowed or not blast_within(step.blast_radius, directives.max_blast_radius):
            # A step that would violate the posture is never executed.
            return "runbook_step_failed", results
        result = await executor.execute_step(
            step, incident_id=incident_id, directives=directives, policy=policy
        )
        results.append(result)
        if result.status == "failed":
            return "runbook_step_failed", results
    return "runbook_completed", results


async def monitor_recovery(
    monitor: RecoveryMonitor,
    *,
    incident_id: uuid.UUID,
    assessment: ImpactAssessment,
) -> tuple[str, object]:
    """Read recovery signals; return ``recovery_confirmed`` / ``recovery_failed``."""
    status = await monitor.check_recovery(incident_id=incident_id, assessment=assessment)
    return ("recovery_confirmed" if status.recovered else "recovery_failed"), status


def generate_postmortem(
    *,
    incident: IncidentSnapshot,
    events: list[IncidentEventDTO],
    plans: list[Runbook],
    board: TaskCreator,
    composer: PostmortemComposer | None = None,
) -> tuple[Postmortem, list[TaskDTO]]:
    """Compose the postmortem and create follow-up board Tasks from its items."""
    comp = composer or TemplatePostmortemComposer()
    postmortem = comp.compose(incident=incident, events=events, plans=plans)
    # A postmortem's action items are tracked as board tasks in the incident's
    # project; the composer is only run for a resolved incident, which always
    # carries a project (mirrors the ``state.runbook``/``assessment`` asserts).
    assert incident.project_id is not None
    tasks = create_action_item_tasks(
        board,
        project_id=incident.project_id,
        action_items=postmortem.action_items,
        incident_key=incident.key,
    )
    return postmortem, tasks


# --------------------------------------------------------------------------- #
# Orchestration helpers (drive the FSM with the effect bodies + doubles)       #
# --------------------------------------------------------------------------- #


@dataclass
class IncidentFlowState:
    """Lightweight FSM state carried through the worker orchestration."""

    incident_id: uuid.UUID
    lifecycle_state: str
    retry_count: int = 0
    findings: list[ContextFinding] = field(default_factory=list)
    assessment: ImpactAssessment | None = None
    runbook: Runbook | None = None
    events: list[IncidentEventDTO] = field(default_factory=list)
    plans: list[Runbook] = field(default_factory=list)
    context: dict[str, bool] = field(default_factory=dict)

    def record(self, kind: str, summary: str, **data: object) -> None:
        self.events.append(
            IncidentEventDTO(
                incident_id=self.incident_id,
                sequence=len(self.events) + 1,
                kind=kind,
                summary=summary,
                data=dict(data),
                created_at=datetime.now(UTC),
            )
        )


async def run_pre_approval_phase(
    state: IncidentFlowState,
    *,
    agent: IncidentAgentPort,
    directives: SkillDirectives,
    repo_id: str | None = None,
) -> IncidentFlowState:
    """Drive ``incident_created`` → ``awaiting_approval`` (or escalation).

    Returns the updated state; the FSM transitions are applied by the caller via
    ``forge_workflow.drive_incident`` using the returned per-step events.
    """
    from forge_workflow import drive_incident

    def advance(event: str) -> None:
        outcome = drive_incident(
            state.lifecycle_state,
            event,
            context=dict(state.context),
            retry_count=state.retry_count,
            max_retries=2,
        )
        state.retry_count = outcome.retry_count
        state.lifecycle_state = outcome.to_state

    advance("incident_acknowledged")
    event, findings = await gather_incident_context(
        agent, incident_id=state.incident_id, repo_id=repo_id
    )
    state.findings = findings
    for finding in findings:
        state.record("context_finding", finding.summary, source=finding.source)
    advance(event)

    event, assessment = await assess_incident_impact(
        agent, incident_id=state.incident_id, findings=findings
    )
    state.assessment = assessment
    state.record("impact", assessment.user_impact, blast_radius=assessment.blast_radius.value)
    advance(event)

    event, runbook, offending = await propose_remediation(
        agent,
        incident_id=state.incident_id,
        attempt=state.retry_count + 1,
        assessment=assessment,
        findings=findings,
        directives=directives,
    )
    state.runbook = runbook
    state.plans.append(runbook)
    state.context["remediation_within_blast_radius"] = not offending
    state.context["remediation_exceeds_blast_radius"] = bool(offending)
    state.record("remediation_proposed", f"{len(runbook.steps)} steps", offending=offending)
    advance(event)
    return state


async def run_post_approval_phase(
    state: IncidentFlowState,
    *,
    executor: RunbookExecutor,
    monitor: RecoveryMonitor,
    directives: SkillDirectives,
    incident: IncidentSnapshot,
    board: TaskCreator,
    policy: object | None = None,
    composer: PostmortemComposer | None = None,
) -> tuple[IncidentFlowState, Postmortem | None, list[TaskDTO]]:
    """Drive ``executing_runbook`` → ``postmortem_created`` and compose the PM."""
    from forge_workflow import drive_incident

    def advance(event: str) -> None:
        outcome = drive_incident(
            state.lifecycle_state,
            event,
            context=dict(state.context),
            retry_count=state.retry_count,
            max_retries=2,
        )
        state.retry_count = outcome.retry_count
        state.lifecycle_state = outcome.to_state

    assert state.runbook is not None
    assert state.assessment is not None

    event, results = await execute_runbook(
        executor,
        incident_id=state.incident_id,
        runbook=state.runbook,
        directives=directives,
        policy=policy,
    )
    for res in results:
        state.record("runbook_step", res.summary or res.step_id)
    advance(event)
    if event == "runbook_step_failed":
        return state, None, []

    event, _status = await monitor_recovery(
        monitor, incident_id=state.incident_id, assessment=state.assessment
    )
    advance(event)
    if event == "recovery_failed":
        return state, None, []

    advance("postmortem_requested")
    postmortem, tasks = generate_postmortem(
        incident=incident,
        events=state.events,
        plans=state.plans,
        board=board,
        composer=composer,
    )
    state.context["postmortem_persisted"] = True
    return state, postmortem, tasks


# --------------------------------------------------------------------------- #
# Celery registration (the ``incident`` queue seam)                            #
# --------------------------------------------------------------------------- #


def run_incident_diagnosis_task(incident_id: str) -> str:
    """Celery seam: enqueue read-only diagnosis for an incident.

    The heavy lifting lives in the pure effect bodies above (unit-tested without
    Celery); in production this task loads the incident, runs the agent-backed
    effect with the workspace BYOK model, and delivers the resulting FSM event
    back to the engine. Kept thin and side-effect-free at import time.
    """
    return incident_id


def register_incident_queue() -> None:
    """Register the incident task on the dedicated ``incident`` Celery queue."""
    celery_app.task(
        name="forge_worker.tasks.incident.run_incident_diagnosis",
        queue=INCIDENT_QUEUE,
    )(run_incident_diagnosis_task)


register_incident_queue()
