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

A ``pr`` gate carries the repository's review rules from the default branch
(:mod:`scf_worker.review_rules`, spec K §4.11), so repo policy can raise its
quorum. A ``spec`` gate names the held-out scenarios the verifier drafted for
the run's spec, by id and title only (#288). The opened gate is announced on
``/ws`` through the Redis channel the API relays (spec L §6a), when the Redis
realtime backend is selected.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.observability.audit import AuditLog
from scf_api.observability.audit_db import DbAuditStore
from scf_api.observability.redaction import redact_mapping
from scf_api.realtime.approval_events import create_published, relay_url
from scf_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from scf_api.services.held_out import drafted_for_run
from scf_api.services.workflow_gate_hook import WORKFLOW_RUN_SUBJECT, agent_actor_for_run
from scf_approval import ApprovalAuthorizer, ApprovalService, GateRegistry
from scf_approval.models import GateType
from scf_db.models import ApprovalRequest, ApprovalStatus, Task
from scf_worker.review_rules import review_rules_stamp
from scf_workflow.temporal.payloads import ApprovalInput


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


def expire_open_gates(
    factory: sessionmaker[Session],
    *,
    workflow_run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor: str,
) -> int:
    """Expire every gate request still pending for a cancelled run (spec K §4.11).

    The API expires the open gate when it cancels, but a Temporal workflow can open
    a gate request after that, before it acts on the cancel; its cancel path runs
    this to catch it. Returns how many requests it expired.
    """
    with factory() as session:
        ids = list(
            session.scalars(
                select(ApprovalRequest.id).where(
                    ApprovalRequest.workspace_id == workspace_id,
                    ApprovalRequest.workflow_run_id == workflow_run_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                )
            )
        )
    if not ids:
        return 0
    service = build_approval_service(factory)

    async def _expire_all() -> None:
        for approval_id in ids:
            request = await service.get(approval_id, workspace_id=workspace_id)
            await service.expire(request, actor=actor, reason="run_cancelled")

    asyncio.run(_expire_all())
    return len(ids)


def make_create_approval_fn(
    factory: sessionmaker[Session],
) -> Callable[[ApprovalInput], Awaitable[uuid.UUID]]:
    """Build the async ``forge.create_approval`` body bound to ``factory``."""
    redis_url = relay_url()

    async def _create_approval(inp: ApprovalInput) -> uuid.UUID:
        with factory() as session:
            existing = _existing_request(session, inp)
            if existing is not None:
                return existing
            task = session.get(Task, inp.task_id)
            scenarios = (
                drafted_for_run(
                    session, workspace_id=inp.workspace_id, workflow_run_id=inp.workflow_run_id
                )
                if inp.gate == GateType.SPEC.value
                else []
            )
        title = (
            (task.title if task is not None else None)
            or inp.summary
            or (f"{inp.gate} approval for run {inp.workflow_run_id}")
        )
        payload: dict[str, object] = {"summary": inp.summary} if inp.summary else {}
        payload["idempotency_key"] = inp.idempotency_key
        if scenarios:  # ids and titles: the content is read through the scenario route
            payload["held_out_scenarios"] = [{"id": str(r.id), "title": r.title} for r in scenarios]
        if inp.gate == GateType.PR.value:
            payload |= review_rules_stamp(
                factory, workspace_id=inp.workspace_id, task_id=inp.task_id
            )
        request = await create_published(
            build_approval_service(factory),
            redis_url=redis_url,
            workspace_id=inp.workspace_id,
            gate_type=GateType(inp.gate),
            subject_type=WORKFLOW_RUN_SUBJECT,
            subject_id=inp.workflow_run_id,
            workflow_run_id=inp.workflow_run_id,
            task_id=inp.task_id,
            project_id=task.project_id if task is not None else None,
            title=title,
            requested_actor=agent_actor_for_run(inp.workflow_run_id),
            gate_payload=payload,
        )
        return request.id

    return _create_approval


__all__ = ["build_approval_service", "expire_open_gates", "make_create_approval_fn"]
