"""F36 unified approvals router — the generic, gate-agnostic REST surface.

One inbox + one decision path for every gate type (``spec | plan | pr | deploy
| incident_remediation | policy_override``):

* ``GET  /approvals``                        — inbox (status/gate_type/project/mine filters)
* ``GET  /approvals/count``                  — pending-count nav badge
* ``GET  /approvals/{id}``                   — one gate
* ``GET  /approvals/{id}/context``           — the nine "must-show" items
* ``GET  /approvals/{id}/decisions``         — per-approver decision trail
* ``POST /approvals/{id}/decision``          — approve / reject / request changes / escalate
* ``POST /approvals``                        — open a gate (producers / synthetic triggers)

Authorization for DECISIONS lives in exactly one place — the domain
:class:`ApprovalAuthorizer` inside ``ApprovalService.resolve`` — so agents,
viewers, and under-privileged members receive the same ``403`` (with a
``reason``) on every surface. Cross-workspace ids map to ``404`` (no existence
leak); already-resolved gates and duplicate votes map to ``409``.

The Phase-2 ``/approval/*`` router (in-memory pr-era queue) remains mounted
for backward compatibility; this router supersedes it for all new gates.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_api.auth.rbac import Permission
from forge_api.deps import Principal, get_current_principal
from forge_api.realtime.broadcaster import Broadcaster, emit_event, get_broadcaster
from forge_api.routers._rbac import require_permission
from forge_api.schemas.approvals import ApprovalCount, CreateApprovalRequest
from forge_api.services.approval_service import (
    get_approval_service,
    to_approval_principal,
)
from forge_approval import (
    AlreadyResolvedError,
    ApprovalNotFoundError,
    ApprovalService,
    AuthorizationError,
    DuplicateDecisionError,
)
from forge_approval.models import (
    ApprovalContext,
    ApprovalDecisionRecord,
    ApprovalDecisionRequest,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalSummary,
    GateStatus,
    GateType,
)
from forge_contracts import RealtimeEvent, RealtimeEventType

router = APIRouter(
    prefix="/approvals",
    tags=["approvals"],
    dependencies=[Depends(get_current_principal)],
)

# Reads require READ. Opening a gate is a WRITE (workflow effects / producers).
# The DECISION endpoint deliberately requires only READ at the router: the
# domain ApprovalAuthorizer is the single policy (agents/viewers get their 403
# from it, with a reason), so no surface can diverge from another.
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]

ServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]

# RT-2: fan approval decisions out to the workspace's live ``/ws`` sockets.
BroadcasterDep = Annotated[Broadcaster, Depends(get_broadcaster)]


def _not_found(approval_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no approval request {approval_id}"
    )


@router.post("", response_model=ApprovalRequest, status_code=status.HTTP_201_CREATED)
async def create_approval(
    service: ServiceDep,
    principal: WriterDep,
    broadcaster: BroadcasterDep,
    body: CreateApprovalRequest,
) -> ApprovalRequest:
    """Open a gate in the caller's workspace (idempotent while pending)."""
    request = await service.create(
        workspace_id=principal.workspace_id,
        gate_type=body.gate_type,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        workflow_run_id=body.workflow_run_id,
        agent_run_id=body.agent_run_id,
        task_id=body.task_id,
        project_id=body.project_id,
        requested_actor=body.requested_actor,
        required_approvals=body.required_approvals,
        risk_level=body.risk_level,
        title=body.title,
        gate_payload=body.gate_payload,
        expires_at=body.expires_at,
    )
    await emit_event(
        broadcaster,
        RealtimeEvent(
            type=RealtimeEventType.APPROVAL_REQUESTED,
            workspace_id=principal.workspace_id,
            approval_id=request.id,
            payload={"gate_type": body.gate_type.value},
        ),
    )
    return request


@router.get("", response_model=list[ApprovalSummary])
async def list_approvals(
    service: ServiceDep,
    principal: ReaderDep,
    status_filter: Annotated[GateStatus | None, Query(alias="status")] = None,
    gate_type: Annotated[GateType | None, Query()] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    mine: Annotated[bool, Query()] = False,
) -> list[ApprovalSummary]:
    """The approval inbox: workspace-scoped, critical risk first."""
    return await service.list(
        workspace_id=principal.workspace_id,
        actor=to_approval_principal(principal),
        status=status_filter,
        gate_type=gate_type,
        project_id=project_id,
        mine=mine,
    )


@router.get("/count", response_model=ApprovalCount)
async def count_approvals(
    service: ServiceDep,
    principal: ReaderDep,
    status_filter: Annotated[GateStatus, Query(alias="status")] = GateStatus.PENDING,
    mine: Annotated[bool, Query()] = False,
) -> ApprovalCount:
    """Pending-count badge; matches the inbox length by construction."""
    count = await service.count(
        workspace_id=principal.workspace_id,
        actor=to_approval_principal(principal),
        status=status_filter,
        mine=mine,
    )
    return ApprovalCount(count=count)


@router.get("/{approval_id}", response_model=ApprovalRequest)
async def get_approval(
    service: ServiceDep, principal: ReaderDep, approval_id: uuid.UUID
) -> ApprovalRequest:
    """One gate (workspace-scoped; cross-workspace ids look nonexistent)."""
    try:
        return await service.get(approval_id, workspace_id=principal.workspace_id)
    except ApprovalNotFoundError:
        raise _not_found(approval_id) from None


@router.get("/{approval_id}/context", response_model=ApprovalContext)
async def get_approval_context(
    service: ServiceDep, principal: ReaderDep, approval_id: uuid.UUID
) -> ApprovalContext:
    """The spec's nine "must-show" items, built by the gate's provider."""
    try:
        return await service.get_context(approval_id, workspace_id=principal.workspace_id)
    except ApprovalNotFoundError:
        raise _not_found(approval_id) from None


@router.get("/{approval_id}/decisions", response_model=list[ApprovalDecisionRecord])
async def list_approval_decisions(
    service: ServiceDep, principal: ReaderDep, approval_id: uuid.UUID
) -> list[ApprovalDecisionRecord]:
    """The immutable per-approver decision trail."""
    try:
        return await service.decisions(approval_id, workspace_id=principal.workspace_id)
    except ApprovalNotFoundError:
        raise _not_found(approval_id) from None


@router.post("/{approval_id}/decision", response_model=ApprovalResolution)
async def decide_approval(
    service: ServiceDep,
    principal: ReaderDep,
    broadcaster: BroadcasterDep,
    approval_id: uuid.UUID,
    body: ApprovalDecisionRequest,
) -> ApprovalResolution:
    """Approve / reject / request changes / escalate a gate.

    The decider identity is the authenticated principal — never the request
    body — and authorization is the domain ``ApprovalAuthorizer`` (the same
    one every other surface calls), so accountability cannot be forged.
    """
    try:
        resolution = await service.resolve(
            approval_id,
            body,
            to_approval_principal(principal),
            workspace_id=principal.workspace_id,
        )
    except ApprovalNotFoundError:
        raise _not_found(approval_id) from None
    except AuthorizationError as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=err.reason) from None
    except AlreadyResolvedError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"approval {approval_id} is already {err.status.value}",
        ) from None
    except DuplicateDecisionError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this approver has already voted on this gate",
        ) from None

    await emit_event(
        broadcaster,
        RealtimeEvent(
            type=RealtimeEventType.APPROVAL_DECIDED,
            workspace_id=principal.workspace_id,
            approval_id=resolution.approval_id,
            payload={"status": resolution.status.value},
        ),
    )
    return resolution


__all__ = ["router"]
