"""External PM-adapter router (F18), mounted at ``/integrations/pm``.

Connection management (admin), health probe, link listing (members), and the two
signature/secret-verified webhook intake routes (no bearer). All queries are
workspace-scoped (cross-workspace ids -> 404, no existence leak).

An accepted webhook now completes the inbound loop: the service persists the
delivery and enqueues the worker board-write task ``forge.pm.process_webhook``
(``forge_worker.tasks.pm_sync`` — re-fetch through the provider adapter, then
``PMSyncEngine.sync_in`` onto the F01 Postgres board substrate, workspace-scoped
and idempotent on redelivery; see the ``pm_service`` docstring).

Still parked: OAuth code-exchange routes, ``backfill`` enqueue, the manual
conflict ``resolve`` execution, and the outbound ``activity_events`` scan.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from forge_api.auth.rbac import Permission
from forge_api.deps import Principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.pm import (
    PMConnectionDetail,
    PMConnectionPatch,
    PMConnectionResponse,
    PMLinkResponse,
)
from forge_api.services.pm_service import (
    PMConflictExists,
    PMConnectionNotFound,
    PMConnectionService,
)
from forge_contracts.pm import PMConnectionConfig
from forge_db.models.enums import PMSyncState
from forge_db.models.pm import PMConnection

router = APIRouter(prefix="/integrations/pm", tags=["integrations", "pm"])

AdminDep = Annotated[Principal, Depends(require_permission(Permission.MANAGE_SECRETS))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]


@lru_cache(maxsize=1)
def _default_service() -> PMConnectionService:
    from forge_api.auth.service import get_auth_service
    from forge_api.db import get_session_factory
    from forge_api.observability.audit import AuditLog

    return PMConnectionService(
        session_factory=get_session_factory(),
        vault=get_auth_service().vault,
        audit=AuditLog(),
    )


def get_pm_service() -> PMConnectionService:
    """Injectable service dependency (tests override with SQLite + fixtures)."""
    return _default_service()


PMServiceDep = Annotated[PMConnectionService, Depends(get_pm_service)]


def _to_response(service: PMConnectionService, conn: PMConnection) -> PMConnectionResponse:
    resp = PMConnectionResponse.model_validate(conn)
    resp = resp.model_copy(
        update={
            "has_credential": bool(conn.credential_ref),
            "has_webhook_secret": bool(conn.webhook_secret_ref),
        }
    )
    return resp


# --------------------------------------------------------------------------- #
# connections                                                                  #
# --------------------------------------------------------------------------- #


@router.post("/connections", response_model=PMConnectionResponse, status_code=201)
def create_connection(
    config: PMConnectionConfig, principal: AdminDep, service: PMServiceDep
) -> PMConnectionResponse:
    try:
        conn = service.create(principal.workspace_id, config)
    except PMConflictExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(service, conn)


@router.get("/connections", response_model=list[PMConnectionResponse])
def list_connections(principal: ReaderDep, service: PMServiceDep) -> list[PMConnectionResponse]:
    return [_to_response(service, c) for c in service.list(principal.workspace_id)]


@router.get("/connections/{connection_id}", response_model=PMConnectionDetail)
def get_connection(
    connection_id: uuid.UUID, principal: ReaderDep, service: PMServiceDep
) -> PMConnectionDetail:
    try:
        conn = service.get(principal.workspace_id, connection_id)
    except PMConnectionNotFound as exc:
        raise HTTPException(status_code=404, detail="connection not found") from exc
    base = _to_response(service, conn)
    return PMConnectionDetail(**base.model_dump(), link_counts=service.link_counts(connection_id))


@router.patch("/connections/{connection_id}", response_model=PMConnectionResponse)
def patch_connection(
    connection_id: uuid.UUID,
    patch: PMConnectionPatch,
    principal: AdminDep,
    service: PMServiceDep,
) -> PMConnectionResponse:
    try:
        conn = service.patch(
            principal.workspace_id,
            connection_id,
            name=patch.name,
            status_map=patch.status_map,
            priority_map=patch.priority_map,
            field_map=patch.field_map,
            sync_direction=patch.sync_direction,
            conflict_policy=patch.conflict_policy,
            enabled=patch.enabled,
        )
    except PMConnectionNotFound as exc:
        raise HTTPException(status_code=404, detail="connection not found") from exc
    return _to_response(service, conn)


@router.delete("/connections/{connection_id}", response_model=PMConnectionResponse)
def disconnect_connection(
    connection_id: uuid.UUID, principal: AdminDep, service: PMServiceDep
) -> PMConnectionResponse:
    try:
        conn = service.disconnect(principal.workspace_id, connection_id)
    except PMConnectionNotFound as exc:
        raise HTTPException(status_code=404, detail="connection not found") from exc
    return _to_response(service, conn)


@router.post("/connections/{connection_id}/test")
async def test_connection(
    connection_id: uuid.UUID, principal: AdminDep, service: PMServiceDep
) -> dict:
    try:
        health = await service.test_connection(principal.workspace_id, connection_id)
    except PMConnectionNotFound as exc:
        raise HTTPException(status_code=404, detail="connection not found") from exc
    return health.model_dump()


@router.get("/connections/{connection_id}/links", response_model=list[PMLinkResponse])
def list_links(
    connection_id: uuid.UUID,
    principal: ReaderDep,
    service: PMServiceDep,
    state: PMSyncState | None = None,
) -> list[PMLinkResponse]:
    try:
        links = service.list_links(principal.workspace_id, connection_id, state=state)
    except PMConnectionNotFound as exc:
        raise HTTPException(status_code=404, detail="connection not found") from exc
    return [PMLinkResponse.model_validate(link) for link in links]


# --------------------------------------------------------------------------- #
# webhook intake (no bearer; verified by signature/secret)                     #
# --------------------------------------------------------------------------- #


# Headers that carry a webhook's authenticity proof. An inbound request lacking
# all of them is rejected up-front (cheap, no DB) as unsigned — both correct
# security behaviour and what keeps a bare/unsigned probe from reaching storage.
_SIGNATURE_HEADERS = ("linear-signature", "x-forge-pm-secret")


async def _intake(
    connection_id: uuid.UUID, request: Request, service: PMConnectionService
) -> Response:
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not any(headers.get(h) for h in _SIGNATURE_HEADERS):
        raise HTTPException(status_code=401, detail="missing webhook signature")
    connection = service.get_connection_any_workspace(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="connection not found")
    body = await request.body()
    code, _event = service.receive_webhook(connection, body, headers)
    if code == 401:
        raise HTTPException(status_code=401, detail="invalid signature")
    return Response(status_code=202)


@router.post("/webhooks/jira/{connection_id}")
async def jira_webhook(
    connection_id: uuid.UUID, request: Request, service: PMServiceDep
) -> Response:
    return await _intake(connection_id, request, service)


@router.post("/webhooks/linear/{connection_id}")
async def linear_webhook(
    connection_id: uuid.UUID, request: Request, service: PMServiceDep
) -> Response:
    return await _intake(connection_id, request, service)
