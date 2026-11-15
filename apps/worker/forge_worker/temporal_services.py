"""Real services for the Temporal workflow's activities (issue #170).

Each function here returns the callable ``WorkflowActivities`` invokes for one
activity. An activity with no service here keeps refusing (ADR-0007).
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from forge_api.observability.audit import AuditLog
from forge_api.observability.audit_db import DbAuditStore
from forge_api.observability.redaction import redact_mapping
from forge_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from forge_approval.authorizer import ApprovalAuthorizer
from forge_approval.models import GateType
from forge_approval.registry import GateRegistry
from forge_approval.service import ApprovalService
from forge_db.models import ApprovalRequest, Task, WorkflowRun
from forge_workflow.red_team_gate import ensure_red_team_verdict
from forge_workflow.temporal.activities import ApprovalFn, RedTeamFn
from forge_workflow.temporal.payloads import ApprovalInput, RedTeamInput, RedTeamResult

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
    """Open the gate's approval request through ``ApprovalService.create``."""
    service = ApprovalService(
        SqlAlchemyApprovalRepository(factory),
        GateRegistry(),
        ApprovalAuthorizer(),
        # The request row is always in Postgres, so its audit entry is too,
        # whatever FORGE_AUDIT_BACKEND selects for the API.
        audit=AuditLog(DbAuditStore(factory)),
        redactor=redact_mapping,
    )

    def create(inp: ApprovalInput) -> uuid.UUID:
        gate = GateType(inp.gate)
        with factory() as session:
            run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            task = session.get(Task, run.task_id)
            # ``ApprovalService.create`` only dedupes while the gate is pending; a
            # retry that lands after the decision must not open a second gate.
            for row in session.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.workspace_id == inp.workspace_id,
                    ApprovalRequest.workflow_run_id == inp.workflow_run_id,
                    ApprovalRequest.gate == gate,
                )
            ):
                if row.payload.get("idempotency_key") == inp.idempotency_key:
                    return row.id
        request = service.create(
            workspace_id=inp.workspace_id,
            gate_type=gate,
            subject_type="workflow_run",
            subject_id=inp.workflow_run_id,
            workflow_run_id=inp.workflow_run_id,
            task_id=run.task_id,
            project_id=task.project_id if task is not None else None,
            title=inp.summary,
            gate_payload={"idempotency_key": inp.idempotency_key},
        )
        # The activity calls this from inside its running event loop, where
        # ``asyncio.run`` refuses; the coroutine gets a loop of its own.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, request).result().id

    return create
