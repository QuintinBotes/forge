"""Deployments router (F31) — environment pipeline + promotion control plane.

All routes auth-required; ``workspace_id`` is resolved from the principal.
RBAC: get/list/gate need READ (viewer+); request/decision/cancel/rollback need
WRITE (member+); pipeline upsert + freeze-override need ADMIN. Cross-workspace
access is 404 (no existence leak).

Note (foundation deviation): Forge's approval substrate is the in-memory
``ApprovalStore`` + the frozen ``ApprovalRequest`` contract, so deploy approvals
are handled in the deployment domain via ``POST /deployments/{id}/decision``
(distinct ``min_approvals`` + no-self-approval) and the append-only
``deployment_approval`` table, rather than mutating the shared primitive.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.observability.audit import AuditLog
from forge_api.routers._rbac import require_permission
from forge_api.schemas.deployments import (
    DecisionRequest,
    DeploymentDetail,
    DeploymentRead,
    FreezeOverrideRequest,
    PipelineRead,
    PipelineUpsert,
)
from forge_api.services.deployment_service import DeploymentService, NotInitiatorError
from forge_contracts.deployment import (
    DeploymentRequest,
    DeploymentState,
)
from forge_deploy.errors import (
    DeploymentConflictError,
    DeploymentNotFoundError,
    EnvironmentNotFoundError,
    GateBlockedError,
    InvalidTransitionError,
    PipelineNotFoundError,
    RuleValidationError,
    SelfApprovalError,
    UnauthorizedApproverError,
    VersionConflictError,
)
from forge_deploy.schemas import GateEvaluation

router = APIRouter(tags=["deployments"], dependencies=[Depends(get_current_principal)])

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


@lru_cache(maxsize=1)
def _service_singleton() -> DeploymentService:
    return DeploymentService(session_factory=get_session_factory(), audit=AuditLog())


def get_deployment_service() -> DeploymentService:
    """Return the process-wide deployments service (override in tests via DI)."""
    return _service_singleton()


ServiceDep = Annotated[DeploymentService, Depends(get_deployment_service)]


def _handle(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        DeploymentNotFoundError | PipelineNotFoundError | EnvironmentNotFoundError,
    ):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, RuleValidationError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if isinstance(exc, VersionConflictError | DeploymentConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, GateBlockedError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "blocking_reasons": exc.blocking_reasons},
        )
    if isinstance(exc, InvalidTransitionError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, SelfApprovalError | UnauthorizedApproverError | NotInitiatorError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise exc


# --------------------------------------------------------------- pipeline
@router.get("/projects/{project_id}/pipeline", response_model=PipelineRead)
def get_pipeline(project_id: uuid.UUID, principal: ReaderDep, service: ServiceDep) -> PipelineRead:
    try:
        data = service.get_pipeline(ws=principal.workspace_id, project_id=project_id)
    except Exception as exc:
        raise _handle(exc) from exc
    return PipelineRead.model_validate(data)


@router.put("/projects/{project_id}/pipeline", response_model=PipelineRead)
def upsert_pipeline(
    project_id: uuid.UUID,
    body: PipelineUpsert,
    principal: AdminDep,
    service: ServiceDep,
) -> PipelineRead:
    try:
        data = service.upsert_pipeline(
            ws=principal.workspace_id,
            project_id=project_id,
            repo_id=body.repo_id,
            enabled=body.enabled,
            version=body.version,
            environments=body.environments,
            actor=f"user:{principal.user_id}",
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return PipelineRead.model_validate(data)


# ------------------------------------------------------------ deployments
@router.post(
    "/projects/{project_id}/deployments",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
)
def request_deployment(
    project_id: uuid.UUID,
    body: DeploymentRequest,
    principal: WriterDep,
    service: ServiceDep,
) -> DeploymentRead:
    try:
        dto = service.request_deployment(
            ws=principal.workspace_id,
            project_id=project_id,
            request=body,
            initiated_by=f"user:{principal.user_id}",
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return DeploymentRead.model_validate(dto, from_attributes=True)


@router.get("/projects/{project_id}/deployments", response_model=list[DeploymentRead])
def list_deployments(
    project_id: uuid.UUID,
    principal: ReaderDep,
    service: ServiceDep,
    environment: Annotated[str | None, Query()] = None,
    state: Annotated[DeploymentState | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeploymentRead]:
    dtos = service.list_deployments(
        ws=principal.workspace_id,
        project_id=project_id,
        environment=environment,
        state=state,
        limit=limit,
    )
    return [DeploymentRead.model_validate(d, from_attributes=True) for d in dtos]


@router.get("/deployments/{deployment_id}", response_model=DeploymentDetail)
def get_deployment(
    deployment_id: uuid.UUID, principal: ReaderDep, service: ServiceDep
) -> DeploymentDetail:
    try:
        data = service.get_deployment(ws=principal.workspace_id, deployment_id=deployment_id)
    except Exception as exc:
        raise _handle(exc) from exc
    return DeploymentDetail.model_validate(data)


@router.get("/deployments/{deployment_id}/gate", response_model=GateEvaluation)
def get_gate(deployment_id: uuid.UUID, principal: ReaderDep, service: ServiceDep) -> GateEvaluation:
    try:
        return service.get_gate(ws=principal.workspace_id, deployment_id=deployment_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployments/{deployment_id}/decision", response_model=DeploymentRead)
def decide_deployment(
    deployment_id: uuid.UUID,
    body: DecisionRequest,
    principal: WriterDep,
    service: ServiceDep,
) -> DeploymentRead:
    try:
        dto = service.decide(
            ws=principal.workspace_id,
            deployment_id=deployment_id,
            decision=body.decision,
            principal=principal,
            note=body.note,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return DeploymentRead.model_validate(dto, from_attributes=True)


@router.post("/deployments/{deployment_id}/cancel", response_model=DeploymentRead)
def cancel_deployment(
    deployment_id: uuid.UUID, principal: WriterDep, service: ServiceDep
) -> DeploymentRead:
    try:
        dto = service.cancel(
            ws=principal.workspace_id,
            deployment_id=deployment_id,
            principal=principal,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return DeploymentRead.model_validate(dto, from_attributes=True)


@router.post("/deployments/{deployment_id}/rollback", response_model=DeploymentRead)
def rollback_deployment(
    deployment_id: uuid.UUID, principal: WriterDep, service: ServiceDep
) -> DeploymentRead:
    try:
        dto = service.rollback(
            ws=principal.workspace_id,
            deployment_id=deployment_id,
            principal=principal,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return DeploymentRead.model_validate(dto, from_attributes=True)


@router.post("/deployments/{deployment_id}/freeze-override", response_model=DeploymentRead)
def override_freeze(
    deployment_id: uuid.UUID,
    body: FreezeOverrideRequest,
    principal: AdminDep,
    service: ServiceDep,
) -> DeploymentRead:
    try:
        dto = service.override_freeze(
            ws=principal.workspace_id,
            deployment_id=deployment_id,
            principal=principal,
            reason=body.reason,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return DeploymentRead.model_validate(dto, from_attributes=True)


__all__ = ["get_deployment_service", "router"]
