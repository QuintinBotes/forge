"""Workflow visual editor router (F28).

Serves the governed, versioned authoring surface over HTTP. All routes are
auth-required; RBAC is per-route: read = viewer+ (READ), draft/validate = member+
(WRITE), publish/fork/rollback/archive/import = admin (ADMIN). Cross-workspace
names return 404 (no existence disclosure).

Audit: publish/rollback/archive emit a critical, fail-closed audit event through
the process audit log. (F39's ``SqlAuditWriter`` is not present in this codebase,
so this degrades to the existing hash-chained :class:`AuditLog` — see slice
notes; the fail-closed ordering still holds.)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from forge_api.auth.rbac import Permission
from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.routers._rbac import require_permission
from forge_workflow.editor.catalog import CatalogResponse, RegistryCatalog
from forge_workflow.editor.errors import (
    BundledReadOnlyError,
    DefinitionNameConflictError,
    DefinitionNotFoundError,
    PublishBlockedError,
    RevisionNotFoundError,
)
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository
from forge_workflow.editor.schemas import (
    CreateDefinition,
    DefinitionDetail,
    DefinitionDiff,
    DefinitionSummary,
    ImportRequest,
    RevisionDetail,
    RevisionSummary,
    RollbackRequest,
    SaveDraftRequest,
)
from forge_workflow.editor.service import WorkflowEditorService
from forge_workflow.editor.validation import ValidationIssue
from forge_workflow.exceptions import WorkflowDefinitionError

router = APIRouter(
    prefix="/workflow/editor",
    tags=["workflow-editor"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


@lru_cache(maxsize=1)
def _catalog() -> RegistryCatalog:
    return RegistryCatalog()


@lru_cache(maxsize=1)
def _audit_log() -> AuditLog:
    return AuditLog()


class _AuditLogSink:
    """Adapt the F28 ``AuditSink`` protocol onto the process audit log."""

    def __init__(self, audit_log: AuditLog) -> None:
        self._log = audit_log

    def emit(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        workspace_id: uuid.UUID,
        actor: uuid.UUID | None,
        metadata: dict[str, Any],
    ) -> None:
        self._log.record(
            category=AuditCategory.SYSTEM,
            action=action,
            actor=str(actor) if actor else None,
            workspace_id=workspace_id,
            target=f"{resource_type}:{resource_id}",
            status="ok",
            metadata=metadata,
        )


def get_editor_service(session: DbSession) -> WorkflowEditorService:
    """Build the editor service for this request (overridable in tests)."""
    return WorkflowEditorService(
        DbWorkflowDefinitionRepository(session),
        catalog=_catalog(),
        audit=_AuditLogSink(_audit_log()),
    )


ServiceDep = Annotated[WorkflowEditorService, Depends(get_editor_service)]


@contextmanager
def _editor_errors() -> Iterator[None]:
    try:
        yield
    except PublishBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": str(exc),
                "errors": [e.model_dump(mode="json") for e in exc.errors],
            },
        ) from exc
    except (DefinitionNotFoundError, RevisionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (BundledReadOnlyError, DefinitionNameConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WorkflowDefinitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# Catalog + listing                                                            #
# --------------------------------------------------------------------------- #


@router.get("/catalog", response_model=CatalogResponse)
def get_catalog(service: ServiceDep, principal: ReaderDep) -> CatalogResponse:
    return service.catalog(principal.workspace_id)


@router.get("/definitions", response_model=list[DefinitionSummary])
def list_definitions(service: ServiceDep, principal: ReaderDep) -> list[DefinitionSummary]:
    return service.list_definitions(principal.workspace_id)


@router.get("/definitions/{name}", response_model=DefinitionDetail)
def get_definition(
    service: ServiceDep, principal: ReaderDep, name: str
) -> DefinitionDetail:
    with _editor_errors():
        return service.get_definition(principal.workspace_id, name)


@router.post(
    "/definitions",
    response_model=DefinitionDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_definition(
    service: ServiceDep, principal: AdminDep, body: CreateDefinition
) -> DefinitionDetail:
    with _editor_errors():
        return service.create_definition(principal.workspace_id, body, actor=principal.user_id)


@router.post(
    "/definitions/{name}/fork",
    response_model=DefinitionDetail,
    status_code=status.HTTP_201_CREATED,
)
def fork_bundled(
    service: ServiceDep, principal: AdminDep, name: str
) -> DefinitionDetail:
    with _editor_errors():
        return service.fork_bundled(principal.workspace_id, name, actor=principal.user_id)


# --------------------------------------------------------------------------- #
# Draft / validate / publish                                                   #
# --------------------------------------------------------------------------- #


@router.put("/definitions/{name}/draft", response_model=RevisionDetail)
def save_draft(
    service: ServiceDep, principal: WriterDep, name: str, body: SaveDraftRequest
) -> RevisionDetail:
    with _editor_errors():
        return service.save_draft(principal.workspace_id, name, body, actor=principal.user_id)


@router.post(
    "/definitions/{name}/draft/validate", response_model=list[ValidationIssue]
)
def validate_draft(
    service: ServiceDep, principal: WriterDep, name: str
) -> list[ValidationIssue]:
    with _editor_errors():
        return service.validate_draft(principal.workspace_id, name)


@router.post("/definitions/{name}/publish", response_model=RevisionDetail)
def publish(service: ServiceDep, principal: AdminDep, name: str) -> RevisionDetail:
    with _editor_errors():
        return service.publish(principal.workspace_id, name, actor=principal.user_id)


# --------------------------------------------------------------------------- #
# Revisions / diff / rollback / archive                                        #
# --------------------------------------------------------------------------- #


@router.get("/definitions/{name}/revisions", response_model=list[RevisionSummary])
def list_revisions(
    service: ServiceDep, principal: ReaderDep, name: str
) -> list[RevisionSummary]:
    with _editor_errors():
        return service.list_revisions(principal.workspace_id, name)


@router.get(
    "/definitions/{name}/revisions/{revision}", response_model=RevisionDetail
)
def get_revision(
    service: ServiceDep, principal: ReaderDep, name: str, revision: int
) -> RevisionDetail:
    with _editor_errors():
        return service.get_revision(principal.workspace_id, name, revision)


@router.get("/definitions/{name}/diff", response_model=DefinitionDiff)
def diff_revisions(
    service: ServiceDep,
    principal: ReaderDep,
    name: str,
    from_: Annotated[int, Query(alias="from")],
    to: int,
) -> DefinitionDiff:
    with _editor_errors():
        return service.diff_revisions(principal.workspace_id, name, from_, to)


@router.post("/definitions/{name}/rollback", response_model=RevisionDetail)
def rollback(
    service: ServiceDep, principal: AdminDep, name: str, body: RollbackRequest
) -> RevisionDetail:
    with _editor_errors():
        return service.rollback(
            principal.workspace_id, name, body.to_revision, actor=principal.user_id
        )


@router.post("/definitions/{name}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive(service: ServiceDep, principal: AdminDep, name: str) -> Response:
    with _editor_errors():
        service.archive(principal.workspace_id, name, actor=principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Import / export                                                              #
# --------------------------------------------------------------------------- #


@router.get("/definitions/{name}/export")
def export_definition(
    service: ServiceDep,
    principal: ReaderDep,
    name: str,
    revision: int | None = None,
    format: str = "yaml",
) -> Response:
    with _editor_errors():
        yaml_text = service.export_yaml(principal.workspace_id, name, revision=revision)
    return Response(content=yaml_text, media_type="text/yaml")


@router.post(
    "/import", response_model=DefinitionDetail, status_code=status.HTTP_201_CREATED
)
def import_yaml(
    service: ServiceDep, principal: AdminDep, body: ImportRequest
) -> DefinitionDetail:
    with _editor_errors():
        return service.import_yaml(principal.workspace_id, body, actor=principal.user_id)


__all__ = ["get_editor_service", "router"]
