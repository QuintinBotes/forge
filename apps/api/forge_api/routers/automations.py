"""Automations router (F21) — saved WHEN/IF/THEN rule CRUD + dry-run + catalog.

All routes auth-required; ``workspace_id`` is resolved from the principal.
RBAC: list/get/executions/catalog need READ (viewer+); create/update/enable/
disable/test need WRITE (member+; ``agent-runner`` lacks WRITE so an agent can
never author a rule); delete needs ADMIN (admin only). Cross-workspace access is
404 (no existence leak).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.observability.audit import AuditLog
from forge_api.routers._rbac import require_permission
from forge_api.schemas.automations import (
    ActionCatalogEntry,
    AutomationCatalog,
    AutomationExecutionRead,
    AutomationRuleCreate,
    AutomationRuleRead,
    AutomationRuleUpdate,
    DryRunRequest,
    DryRunResult,
    TriggerCatalogEntry,
)
from forge_api.services.automations import (
    AutomationRuleService,
    RuleNotFound,
    VersionConflict,
)
from forge_board.automation import CONDITION_FIELDS
from forge_board.automation.errors import ActionForbiddenError, RuleValidationError
from forge_board.automation.schemas import (
    AddCommentAction,
    CloseLinkedSpecTasksAction,
    CreateTaskAction,
    SendNotificationAction,
    SendWorkflowEventAction,
    SetAssigneeAction,
    SetFieldAction,
    SetPriorityAction,
    SetStatusAction,
)
from forge_contracts.automation import (
    AutomationActionType,
    AutomationTriggerType,
    ConditionOp,
)

router = APIRouter(tags=["automations"], dependencies=[Depends(get_current_principal)])

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


# --------------------------------------------------------------------------- #
# Service dependency (overridable in tests)                                    #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _service_singleton() -> AutomationRuleService:
    return AutomationRuleService(session_factory=get_session_factory(), audit=AuditLog())


def get_automation_service() -> AutomationRuleService:
    """Return the process-wide automations service (override in tests via DI)."""
    return _service_singleton()


ServiceDep = Annotated[AutomationRuleService, Depends(get_automation_service)]


# --------------------------------------------------------------------------- #
# Error mapping                                                                #
# --------------------------------------------------------------------------- #


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except ActionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "action_forbidden_event", "issues": exc.issues},
        ) from exc
    except RuleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "rule_validation_error", "issues": exc.issues},
        ) from exc
    except VersionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "version_conflict", "current_version": exc.current_version},
        ) from exc
    except RuleNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Catalog                                                                      #
# --------------------------------------------------------------------------- #

_ACTION_MODELS: dict[AutomationActionType, type[BaseModel]] = {
    AutomationActionType.SET_STATUS: SetStatusAction,
    AutomationActionType.SET_PRIORITY: SetPriorityAction,
    AutomationActionType.SET_ASSIGNEE: SetAssigneeAction,
    AutomationActionType.SET_FIELD: SetFieldAction,
    AutomationActionType.ADD_COMMENT: AddCommentAction,
    AutomationActionType.CLOSE_LINKED_SPEC_TASKS: CloseLinkedSpecTasksAction,
    AutomationActionType.SEND_WORKFLOW_EVENT: SendWorkflowEventAction,
    AutomationActionType.SEND_NOTIFICATION: SendNotificationAction,
    AutomationActionType.CREATE_TASK: CreateTaskAction,
}

_REQUIRED_TRIGGER_CONFIG: dict[AutomationTriggerType, list[str]] = {
    AutomationTriggerType.WORKFLOW_STATE_CHANGED: ["to_state"],
}


@router.get("/automations/catalog", response_model=AutomationCatalog)
def catalog(principal: ReaderDep) -> AutomationCatalog:
    """Trigger/condition/action catalog that drives the UI builder."""
    triggers = [
        TriggerCatalogEntry(type=t, required_config=_REQUIRED_TRIGGER_CONFIG.get(t, []))
        for t in AutomationTriggerType
    ]
    actions = [
        ActionCatalogEntry(
            type=t,
            args={
                name: str(field.annotation)
                for name, field in model.model_fields.items()
                if name != "type"
            },
        )
        for t, model in _ACTION_MODELS.items()
    ]
    return AutomationCatalog(
        triggers=triggers,
        condition_fields=sorted(CONDITION_FIELDS),
        condition_ops=list(ConditionOp),
        actions=actions,
    )


# --------------------------------------------------------------------------- #
# Project-scoped CRUD                                                          #
# --------------------------------------------------------------------------- #


@router.post(
    "/projects/{project_id}/automations",
    response_model=AutomationRuleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(
    service: ServiceDep,
    principal: WriterDep,
    project_id: uuid.UUID,
    body: AutomationRuleCreate,
) -> AutomationRuleRead:
    with _errors():
        return service.create(
            workspace_id=principal.workspace_id,
            project_id=project_id,
            body=body,
            actor_user_id=principal.user_id,
        )


@router.get("/projects/{project_id}/automations", response_model=list[AutomationRuleRead])
def list_rules(
    service: ServiceDep, principal: ReaderDep, project_id: uuid.UUID
) -> list[AutomationRuleRead]:
    return service.list(workspace_id=principal.workspace_id, project_id=project_id)


# --------------------------------------------------------------------------- #
# Rule-scoped operations                                                       #
# --------------------------------------------------------------------------- #


@router.get("/automations/{rule_id}", response_model=AutomationRuleRead)
def get_rule(service: ServiceDep, principal: ReaderDep, rule_id: uuid.UUID) -> AutomationRuleRead:
    with _errors():
        return service.get(workspace_id=principal.workspace_id, rule_id=rule_id)


@router.patch("/automations/{rule_id}", response_model=AutomationRuleRead)
def update_rule(
    service: ServiceDep,
    principal: WriterDep,
    rule_id: uuid.UUID,
    body: AutomationRuleUpdate,
) -> AutomationRuleRead:
    with _errors():
        return service.update(
            workspace_id=principal.workspace_id,
            rule_id=rule_id,
            patch=body,
            actor_user_id=principal.user_id,
        )


@router.post("/automations/{rule_id}/enable", response_model=AutomationRuleRead)
def enable_rule(
    service: ServiceDep, principal: WriterDep, rule_id: uuid.UUID
) -> AutomationRuleRead:
    with _errors():
        return service.set_enabled(
            workspace_id=principal.workspace_id,
            rule_id=rule_id,
            enabled=True,
            actor_user_id=principal.user_id,
        )


@router.post("/automations/{rule_id}/disable", response_model=AutomationRuleRead)
def disable_rule(
    service: ServiceDep, principal: WriterDep, rule_id: uuid.UUID
) -> AutomationRuleRead:
    with _errors():
        return service.set_enabled(
            workspace_id=principal.workspace_id,
            rule_id=rule_id,
            enabled=False,
            actor_user_id=principal.user_id,
        )


@router.delete("/automations/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(service: ServiceDep, principal: AdminDep, rule_id: uuid.UUID) -> None:
    with _errors():
        service.delete(
            workspace_id=principal.workspace_id,
            rule_id=rule_id,
            actor_user_id=principal.user_id,
        )


@router.post("/automations/{rule_id}/test", response_model=DryRunResult)
def dry_run(
    service: ServiceDep,
    principal: WriterDep,
    rule_id: uuid.UUID,
    body: DryRunRequest,
) -> DryRunResult:
    with _errors():
        return service.dry_run(
            workspace_id=principal.workspace_id,
            rule_id=rule_id,
            task_id=body.task_id,
            change=body.change,
        )


@router.get("/automations/{rule_id}/executions", response_model=list[AutomationExecutionRead])
def executions(
    service: ServiceDep,
    principal: ReaderDep,
    rule_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AutomationExecutionRead]:
    with _errors():
        return service.executions(workspace_id=principal.workspace_id, rule_id=rule_id, limit=limit)


__all__ = ["get_automation_service", "router"]
