"""The ``forge.create_approval`` Temporal activity body (spec K P0, A1).

``FeatureWorkflow`` calls ``forge.create_approval`` before each human gate,
but the production worker never wired it, so every Temporal feature run
failed at its first gate with ``ActivityNotConfigured``. This module builds
the real body: open the gate's approval request through the approval service
on the database repository — one pending request per ``(run, gate)``
(``ApprovalService.create`` is idempotent while pending, honouring
``uq_pending_gate``, so an activity retry returns the existing request). A
retry that lands after the gate was decided returns the decided request, found
by the activity's idempotency key, and the request's audit entry goes on the
database audit chain (issue #170).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.observability.audit import AuditLog
from forge_api.observability.audit_db import DbAuditStore
from forge_api.observability.redaction import redact_mapping
from forge_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from forge_api.services.workflow_gate_hook import WORKFLOW_RUN_SUBJECT, agent_actor_for_run
from forge_approval import ApprovalAuthorizer, ApprovalService, GateRegistry
from forge_approval.models import GateType
from forge_db.models import ApprovalRequest, Task
from forge_workflow.temporal.payloads import ApprovalInput


def build_approval_service(factory: sessionmaker[Session]) -> ApprovalService:
    """The approval service over the durable DB repository.

    Creation needs no gate providers/hooks (those serve context + resolve in
    the API process); the authorizer still applies its required-approvals
    floor at create time, exactly as in the API composition root.
    """
    return ApprovalService(
        SqlAlchemyApprovalRepository(factory),
        GateRegistry(),
        ApprovalAuthorizer(),
        # The request row is always in Postgres, so its audit entry is too,
        # whatever FORGE_AUDIT_BACKEND selects for the API.
        audit=AuditLog(DbAuditStore(factory)),
        redactor=redact_mapping,
    )


def _existing_request(session: Session, inp: ApprovalInput) -> uuid.UUID | None:
    """The request an earlier attempt of this activity opened, decided or not.

    ``ApprovalService.create`` only dedupes while the gate is pending; a retry
    that lands after the decision must not open a second gate.
    """
    for row in session.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == inp.workspace_id,
            ApprovalRequest.workflow_run_id == inp.workflow_run_id,
            ApprovalRequest.gate == GateType(inp.gate),
        )
    ):
        if row.payload.get("idempotency_key") == inp.idempotency_key:
            return row.id
    return None


def make_create_approval_fn(
    factory: sessionmaker[Session],
) -> Callable[[ApprovalInput], Awaitable[uuid.UUID]]:
    """Build the async ``forge.create_approval`` body bound to ``factory``."""

    async def _create_approval(inp: ApprovalInput) -> uuid.UUID:
        with factory() as session:
            existing = _existing_request(session, inp)
            if existing is not None:
                return existing
            task = session.get(Task, inp.task_id)
        title = (
            (task.title if task is not None else None)
            or inp.summary
            or (f"{inp.gate} approval for run {inp.workflow_run_id}")
        )
        request = await build_approval_service(factory).create(
            workspace_id=inp.workspace_id,
            gate_type=GateType(inp.gate),
            subject_type=WORKFLOW_RUN_SUBJECT,
            subject_id=inp.workflow_run_id,
            workflow_run_id=inp.workflow_run_id,
            task_id=inp.task_id,
            project_id=task.project_id if task is not None else None,
            title=title,
            requested_actor=agent_actor_for_run(inp.workflow_run_id),
            gate_payload={
                **({"summary": inp.summary} if inp.summary else {}),
                "idempotency_key": inp.idempotency_key,
            },
        )
        return request.id

    return _create_approval


__all__ = ["build_approval_service", "make_create_approval_fn"]
