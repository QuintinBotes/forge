"""A verifier agent drafts held-out acceptance scenarios before the spec gate (#258).

The verifier runs on a model family other than the builder's (the reviewer role's
model, #173) and is given only the spec: the task's objective, its description and
its acceptance criteria. It has no tools. Each scenario it returns is checked
against the #176 store's rules and stored as a draft, with the verifier's agent run
and model as its drafter. Scenario content is kept out of everything else: the
verifier's agent run records only scenario ids and titles, no steps and no events,
and the workflow run records only ids and counts.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from scf_agent.providers import ModelClientError
from scf_agent.tools import ToolRegistry
from scf_api.schemas.held_out import HeldOutScenarioCreate
from scf_api.services.held_out import store_agent_draft, withdraw_run_scenarios
from scf_contracts import AgentObjective, ModelClient, RunStatus
from scf_contracts.audit import AuditEvent
from scf_db.audit.writer import SqlAuditWriter
from scf_db.models import AgentRun, Task, WorkflowRun
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_worker.agent_runner import (
    _run_token_budget,
    build_agent_runner,
    configured_model,
    run_objective,
)
from scf_worker.review import (
    HomogeneousReviewerError,
    ReviewerRefused,
    ensure_heterogeneous,
    reviewer_client,
    reviewer_model,
)
from scf_workflow.temporal.payloads import DraftScenariosInput, DraftScenariosResult

__all__ = [
    "VERIFIER_ROLE",
    "HomogeneousVerifierError",
    "draft_scenarios_service",
    "parse_scenarios",
    "verifier_tools",
]

#: The ``agent_run.role`` of a verifier run; distinct from the builder's ``primary``.
VERIFIER_ROLE = "verifier"
#: The run-context key recording what the drafting step did (ids and counts only).
DRAFTING_KEY = "held_out_drafting"

_INSTRUCTIONS = (
    "You are the verifier. From the spec below, write executable acceptance "
    "scenarios that check its acceptance criteria from outside the code: each is "
    "files to add to the repository's checkout and one shell command that exits 0 "
    "when the criterion holds. The builder will never see them. Finish with output "
    "that is only this JSON object: "
    '{"scenarios": [{"title": "<what it checks>", "files": [{"path": "<path in the '
    'checkout>", "content": "<file text>"}], "command": "<shell command>"}]}'
)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class HomogeneousVerifierError(ReviewerRefused):
    """The verifier's model family is the builder's: it would write the builder's checks."""

    def __init__(self, message: str) -> None:
        super().__init__("verifier_same_family", message)


def verifier_tools() -> ToolRegistry:
    """The verifier's tools: none. It drafts from the spec, before any code exists."""
    return ToolRegistry()


def parse_scenarios(output: str | None) -> list[dict[str, Any]] | None:
    """The scenarios in the verifier's output, or ``None`` when it does not parse."""
    text = (output or "").strip()
    fenced = _FENCE.fullmatch(text)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        return None
    scenarios = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(scenarios, list) or not all(isinstance(s, dict) for s in scenarios):
        return None
    return scenarios


def _spec(run: WorkflowRun, task: Task) -> AgentObjective:
    """The spec the run carries: the issue's text kept on the run, else the task's."""
    kept = (run.context.get("trigger") or {}).get("objective")
    if kept:
        return AgentObjective.model_validate(kept)
    return AgentObjective.model_validate(
        {
            "objective": task.title,
            "description": task.description,
            "acceptance_criteria": task.acceptance_criteria or [],
        }
    )


def draft_scenarios_service(
    factory: sessionmaker[Session], *, verifier: Callable[[str], ModelClient] = reviewer_client
) -> Callable[[DraftScenariosInput], DraftScenariosResult]:
    """Draft a run's held-out scenarios once per idempotency key (#258)."""

    def draft(inp: DraftScenariosInput) -> DraftScenariosResult:
        with factory() as session:
            run = session.get(WorkflowRun, inp.workflow_run_id)
            if run is None or run.workspace_id != inp.workspace_id:
                raise LookupError(f"workflow run {inp.workflow_run_id} not in this workspace")
            recorded = (run.context.get(DRAFTING_KEY) or {}).get(inp.idempotency_key)
            if recorded is not None:  # a retry of the same step returns what it did
                return DraftScenariosResult(
                    scenario_ids=list(recorded["scenario_ids"]), reason=recorded.get("reason")
                )
            # What this run drafted for its earlier spec text is withdrawn (#288).
            withdraw_run_scenarios(
                session, workspace_id=inp.workspace_id, workflow_run_id=inp.workflow_run_id
            )
            session.commit()
            task = session.get(Task, run.task_id)
            assert task is not None  # workflow_run.task_id is a non-null foreign key
            spec = _spec(run, task)
            if not (spec.description or spec.acceptance_criteria):
                return _record(factory, inp, [], "no_spec_to_draft_from")
            try:
                model = reviewer_model(
                    session, workspace_id=inp.workspace_id, project_id=task.project_id
                )
                builder = configured_model()
                try:
                    ensure_heterogeneous(builder, model)
                except HomogeneousReviewerError as exc:
                    raise HomogeneousVerifierError(str(exc)) from exc
                client = verifier(model)  # no key for its provider: not configured
            except ReviewerRefused as refused:
                reason = refused.reason.replace("reviewer_", "verifier_")
                return _record(factory, inp, [], reason)
        return _run_verifier(factory, inp, task_id=task.id, spec=spec, model=model, client=client)

    return draft


def _run_verifier(
    factory: sessionmaker[Session],
    inp: DraftScenariosInput,
    *,
    task_id: uuid.UUID,
    spec: AgentObjective,
    model: str,
    client: ModelClient,
) -> DraftScenariosResult:
    run_id = uuid.uuid5(inp.workflow_run_id, f"{inp.idempotency_key}:verifier")
    objective = AgentObjective(
        objective="Draft held-out acceptance scenarios for the spec in the block below.",
        instructions=_INSTRUCTIONS,
        context={
            # The spec is the issue's or the task's text: untrusted data (ADR-0014).
            "spec": {
                "title": spec.objective,
                "description": spec.description or "",
                "criteria": [f"{c.id}: {c.text}" for c in spec.acceptance_criteria],
            },
            "agents_md": "",  # no repository instructions: it sees no repository
        },
    )
    runner = build_agent_runner(
        model_client=client, tools=verifier_tools(), token_budget=_run_token_budget()
    )
    try:
        result = run_objective(runner, objective)  # no step sink: its steps are never recorded
        status = result.status
        output = result.output
    except ModelClientError:  # a provider error must not fail the run before its spec gate
        status, output = RunStatus.FAILED, None
    scenarios = parse_scenarios(output) if status == RunStatus.SUCCEEDED else None
    stored: list[dict[str, str]] = []
    dropped: list[dict[str, str]] = []
    with factory() as session:
        for raw in scenarios or []:
            title = str(raw.get("title") or "").replace("\x00", "")[:255]
            try:
                body = HeldOutScenarioCreate.model_validate(raw)
            except ValidationError as exc:
                dropped.append({"title": title, "reason": str(exc.errors()[0].get("msg", ""))})
                continue
            row = store_agent_draft(
                session,
                workspace_id=inp.workspace_id,
                task_id=task_id,
                body=body,
                drafted_by=f"agent:{run_id}/{model}",
                agent_run_id=run_id,
                workflow_run_id=inp.workflow_run_id,
            )
            stored.append({"id": str(row.id), "title": row.title})
        if status != RunStatus.SUCCEEDED:
            reason: str | None = "verifier_run_failed"
        elif scenarios is None:
            reason = "unparseable_scenarios"
        elif not stored:
            reason = "no_scenarios_drafted"
        else:
            reason = None
        session.merge(
            AgentRun(
                id=run_id,
                workspace_id=inp.workspace_id,
                workflow_run_id=inp.workflow_run_id,
                task_id=task_id,
                role=VERIFIER_ROLE,
                model=model,
                inputs={"objective": objective.objective},  # the spec stays on the run
                steps=[],
                # Ids and titles only: the scenarios themselves live in the #176 store.
                output={"scenarios": stored, "dropped": dropped, "reason": reason},
                status=DbRunStatus(status.value),
            )
        )
        SqlAuditWriter(session).emit(
            AuditEvent(
                workspace_id=inp.workspace_id,
                action="held_out.drafted",
                actor_type="system",
                target_type="workflow_run",
                scope_type="workflow_run",
                scope_id=inp.workflow_run_id,
                severity="notice",
                detail_ref={"table": "agent_run", "id": str(run_id)},
                details={
                    "workflow_run_id": str(inp.workflow_run_id),
                    "verifier_model": model,
                    "drafted": len(stored),
                    "dropped": len(dropped),
                    "reason": reason,
                },
            )
        )
        session.commit()
    return _record(factory, inp, [s["id"] for s in stored], reason, dropped=len(dropped))


def _record(
    factory: sessionmaker[Session],
    inp: DraftScenariosInput,
    scenario_ids: list[str],
    reason: str | None,
    *,
    dropped: int = 0,
) -> DraftScenariosResult:
    """Record on the run what the step did: ids, a count of dropped drafts and why."""
    with factory() as session:
        run = session.get(WorkflowRun, inp.workflow_run_id)
        assert run is not None
        drafting = dict(run.context.get(DRAFTING_KEY) or {})
        drafting[inp.idempotency_key] = {
            "scenario_ids": scenario_ids,
            "dropped": dropped,
            "reason": reason,
        }
        context = {**run.context, DRAFTING_KEY: drafting}
        context.pop("held_out", None)  # an earlier spec's reason no longer applies
        if reason is not None and not scenario_ids:
            context["held_out"] = reason
        run.context = context
        session.commit()
    return DraftScenariosResult(scenario_ids=scenario_ids, reason=reason)
