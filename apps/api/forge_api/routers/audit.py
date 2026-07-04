"""Audit router (F39) — the canonical, DB-backed ``/audit`` query surface.

Admin-only in V1 (Security: least-privilege read; the log exposes cross-user
actions and credential touches). Read/verify/export ONLY — audit rows are
produced exclusively by trusted in-process producers via the ``AuditSink``;
there is deliberately **no** HTTP route that creates, updates, or deletes an
entry (AC13).

Sibling surface note: ``GET /observability/audit`` (F38) serves the in-process
observability stream; this router serves the durable, hash-chained security
log in Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from forge_api.auth.rbac import Permission
from forge_api.db import get_db
from forge_api.deps import Principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.audit import (
    AuditActionsOut,
    AuditEntryOut,
    AuditListResponse,
    AuditVerifyIn,
    ChainVerifyResult,
)
from forge_api.services.audit import AuditService
from forge_contracts.audit import (
    ActorType,
    AuditAction,
    AuditOutcome,
    AuditResourceType,
    AuditSeverity,
)

router = APIRouter(prefix="/audit", tags=["audit"])

AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]
SessionDep = Annotated[Session, Depends(get_db)]


def _service(session: Session) -> AuditService:
    return AuditService(session)


@router.get("", response_model=AuditListResponse, summary="List audit entries.")
def list_audit(
    principal: AdminDep,
    session: SessionDep,
    actor_id: uuid.UUID | None = None,
    actor_type: str | None = None,
    action: Annotated[list[str] | None, Query()] = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    outcome: str | None = None,
    severity: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditListResponse:
    rows, next_cursor = _service(session).list_entries(
        principal.workspace_id,
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=outcome,
        severity=severity,
        from_time=from_,
        to_time=to,
        q=q,
        cursor=cursor,
        limit=limit,
    )
    return AuditListResponse(
        items=[AuditEntryOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )


@router.get(
    "/actions",
    response_model=AuditActionsOut,
    summary="Filter vocabulary (actions / actors / outcomes / severities).",
)
def audit_actions(principal: AdminDep) -> AuditActionsOut:
    del principal
    return AuditActionsOut(
        actions=[a.value for a in AuditAction],
        actor_types=[a.value for a in ActorType],
        resource_types=[r.value for r in AuditResourceType],
        outcomes=[o.value for o in AuditOutcome],
        severities=[s.value for s in AuditSeverity],
    )


@router.get(
    "/export",
    summary="Stream an NDJSON export (chain hashes included; re-verifiable offline).",
)
def export_audit(
    principal: AdminDep,
    session: SessionDep,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> StreamingResponse:
    lines = _service(session).export_ndjson(
        principal.workspace_id,
        from_time=from_,
        to_time=to,
        actor_label=f"user:{principal.email}" if principal.email else None,
    )
    return StreamingResponse(
        lines,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit-export.ndjson"'},
    )


@router.post(
    "/verify",
    response_model=ChainVerifyResult,
    summary="Verify the workspace's audit hash chain.",
)
def verify_audit(
    body: AuditVerifyIn | None,
    principal: AdminDep,
    session: SessionDep,
) -> ChainVerifyResult:
    params = body or AuditVerifyIn()
    return _service(session).verify(
        principal.workspace_id, from_seq=params.from_seq, to_seq=params.to_seq
    )


@router.get(
    "/{entry_id}",
    response_model=AuditEntryOut,
    summary="One audit entry (workspace-isolated; foreign ids 404).",
)
def get_audit_entry(
    entry_id: uuid.UUID,
    principal: AdminDep,
    session: SessionDep,
) -> AuditEntryOut:
    row = _service(session).get_entry(principal.workspace_id, entry_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No audit entry {entry_id}",
        )
    return AuditEntryOut.model_validate(row)
