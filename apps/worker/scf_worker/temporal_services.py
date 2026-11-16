"""Real services for the Temporal workflow's activities (issue #170).

Each function here returns the callable ``WorkflowActivities`` invokes for one
activity. An activity with no service here keeps refusing (ADR-0007).
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from scf_db.models import WorkflowRun
from scf_worker.approval_activity import make_create_approval_fn
from scf_workflow.red_team_gate import ensure_red_team_verdict
from scf_workflow.temporal.activities import ApprovalFn, RedTeamFn
from scf_workflow.temporal.payloads import ApprovalInput, RedTeamInput, RedTeamResult

__all__ = ["approval_service", "red_team_service"]


def _owned_run(session: Session, run_id: uuid.UUID, workspace_id: uuid.UUID) -> WorkflowRun:
    """The workflow run, or a refusal when it is not in the caller's workspace."""
    run = session.get(WorkflowRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise ApplicationError(
            f"workflow run {run_id} does not exist in workspace {workspace_id}",
            type="WorkflowRunNotFound",
            non_retryable=True,
        )
    return run


def red_team_service(factory: sessionmaker[Session]) -> RedTeamFn:
    """Record the run's red-team verdict once. No adversary is wired (#174 adds
    one), so the row is the explicit ``parked`` verdict the V1 engine records."""

    def scan(inp: RedTeamInput) -> RedTeamResult:
        with factory() as session:
            _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            row, _ = ensure_red_team_verdict(
                session,
                inp.workspace_id,
                workflow_run_id=inp.workflow_run_id,
                task_id=inp.task_id,
                phase=inp.phase,
                coder_model=inp.coder_model,
            )
            session.commit()
            return RedTeamResult(
                verdict=row.verdict,
                kind=row.kind,
                evidence=dict(row.evidence),
                adversary_model=row.adversary_model,
                coder_model=row.coder_model,
            )

    return scan


def approval_service(factory: sessionmaker[Session]) -> ApprovalFn:
    """Open the gate's approval request, refusing a run outside the caller's workspace.

    The request itself is opened by ``approval_activity.make_create_approval_fn``.
    """
    create = make_create_approval_fn(factory)

    async def create_for_owned_run(inp: ApprovalInput) -> uuid.UUID:
        with factory() as session:
            _owned_run(session, inp.workflow_run_id, inp.workspace_id)
        return await create(inp)

    return create_for_owned_run
