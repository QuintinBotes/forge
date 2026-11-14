"""The ``forge.create_approval`` Temporal activity body (spec K P0, A1).

``FeatureWorkflow`` calls ``forge.create_approval`` before each human gate,
but the production worker never wired it, so every Temporal feature run
failed at its first gate with ``ActivityNotConfigured``. This module builds
the real body: open the gate's approval request through the approval service
on the database repository — one pending request per ``(run, gate)``
(``ApprovalService.create`` is idempotent while pending, honouring
``uq_pending_gate``, so an activity retry returns the existing request).

A ``pr`` gate carries the repository's review rules from the default branch
(:mod:`forge_worker.review_rules`, spec K §4.11), so repo policy can raise its
quorum.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from forge_api.services.workflow_gate_hook import WORKFLOW_RUN_SUBJECT, agent_actor_for_run
from forge_approval import ApprovalAuthorizer, ApprovalService, GateRegistry
from forge_approval.models import GateType
from forge_db.models import Task
from forge_worker.review_rules import review_rules_stamp
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
    )


def _task_title(factory: sessionmaker[Session], task_id: uuid.UUID) -> str | None:
    with factory() as session:
        task = session.get(Task, task_id)
        return task.title if task is not None else None


def make_create_approval_fn(
    factory: sessionmaker[Session],
) -> Callable[[ApprovalInput], Awaitable[uuid.UUID]]:
    """Build the async ``forge.create_approval`` body bound to ``factory``."""

    async def _create_approval(inp: ApprovalInput) -> uuid.UUID:
        title = (
            _task_title(factory, inp.task_id)
            or inp.summary
            or (f"{inp.gate} approval for run {inp.workflow_run_id}")
        )
        payload: dict[str, object] = {"summary": inp.summary} if inp.summary else {}
        if inp.gate == GateType.PR.value:
            payload |= review_rules_stamp(
                factory, workspace_id=inp.workspace_id, task_id=inp.task_id
            )
        request = await build_approval_service(factory).create(
            workspace_id=inp.workspace_id,
            gate_type=GateType(inp.gate),
            subject_type=WORKFLOW_RUN_SUBJECT,
            subject_id=inp.workflow_run_id,
            workflow_run_id=inp.workflow_run_id,
            task_id=inp.task_id,
            title=title,
            requested_actor=agent_actor_for_run(inp.workflow_run_id),
            gate_payload=payload,
        )
        return request.id

    return _create_approval


__all__ = ["build_approval_service", "make_create_approval_fn"]
