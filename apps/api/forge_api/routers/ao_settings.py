"""Adaptive Orchestration settings API (``ao-settings-api``), mounted at ``/ao``.

* ``GET  /ao/role-config``          — every role's effective ``{model_or_tier,
  effort}`` (workspace/project override merged with the hardcoded default).
* ``PUT  /ao/role-config/{role}``   — pin a workspace- or project-scoped
  override (admin).
* ``DELETE /ao/role-config/{role}`` — remove an override, reverting to the
  next fallback (admin).
* ``GET  /ao/settings``             — the workspace-wide auto-route toggle,
  ``tier -> model`` overrides, and effective complexity thresholds.
* ``PUT  /ao/settings``             — partially update those (admin).
* ``POST /ao/routing-preview``      — what tier/model/strategy a sample task
  would get, given this workspace's current settings.

Reads are ``Permission.READ``-gated (a viewer may inspect its own workspace's
AO configuration); every mutation is ``Permission.ADMIN``-gated, matching the
cost price-book precedent (workspace-wide model/routing config is
admin-sensitive, not a per-member setting).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from forge_api.auth.rbac import Permission
from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.ao_settings import (
    AoSettingsOut,
    AoSettingsUpdateRequest,
    RoleConfigListResponse,
    RoleConfigOut,
    RoleConfigUpsertRequest,
    RoutingPreviewRequest,
    RoutingPreviewResponse,
)
from forge_api.services.ao_settings_service import (
    AoSettingsService,
    EffectiveAoSettings,
)
from forge_contracts.orchestration_config import AgentRole, EffectiveRoleConfig
from forge_db.ao_settings import SqlAoSettingsStore
from forge_db.role_config import SqlRoleConfigStore

router = APIRouter(
    prefix="/ao",
    tags=["ao-settings"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


def _service(session: DbSession) -> AoSettingsService:
    return AoSettingsService(SqlRoleConfigStore(session), SqlAoSettingsStore(session))


def _role_out(effective: EffectiveRoleConfig) -> RoleConfigOut:
    return RoleConfigOut(
        role=effective.role,
        model_or_tier=effective.model_or_tier,
        effort=effective.effort,
        source=effective.source,
    )


def _settings_out(effective: EffectiveAoSettings) -> AoSettingsOut:
    return AoSettingsOut(
        workspace_id=effective.workspace_id,
        auto_route=effective.auto_route,
        tier_model_overrides=effective.tier_model_overrides,
        junior_max=effective.junior_max,
        medior_max=effective.medior_max,
        junior_max_is_default=effective.junior_max_is_default,
        medior_max_is_default=effective.medior_max_is_default,
    )


@router.get("/role-config", summary="Every role's effective model+effort config.")
def list_role_config(
    principal: ReaderDep,
    session: DbSession,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RoleConfigListResponse:
    service = _service(session)
    items = [
        _role_out(cfg)
        for cfg in service.list_role_configs(principal.workspace_id, project_id=project_id)
    ]
    return RoleConfigListResponse(items=items)


@router.put(
    "/role-config/{role}",
    summary="Pin a workspace- or project-scoped override for one role (admin).",
)
def upsert_role_config(
    role: AgentRole,
    body: RoleConfigUpsertRequest,
    principal: AdminDep,
    session: DbSession,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RoleConfigOut:
    service = _service(session)
    effective = service.upsert_role_config(
        principal.workspace_id,
        role,
        body.model_or_tier,
        body.effort,
        project_id=project_id,
    )
    session.commit()
    return _role_out(effective)


@router.delete(
    "/role-config/{role}",
    summary="Remove an override for one role, reverting to the next fallback (admin).",
)
def delete_role_config(
    role: AgentRole,
    principal: AdminDep,
    session: DbSession,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RoleConfigOut:
    service = _service(session)
    effective = service.delete_role_config(principal.workspace_id, role, project_id=project_id)
    session.commit()
    return _role_out(effective)


@router.get(
    "/settings",
    summary="Workspace-wide auto-route toggle, tier-model map, complexity thresholds.",
)
def get_settings(principal: ReaderDep, session: DbSession) -> AoSettingsOut:
    service = _service(session)
    return _settings_out(service.get_settings(principal.workspace_id))


@router.put(
    "/settings",
    summary="Update the workspace-wide Adaptive Orchestration settings (admin).",
)
def update_settings(
    body: AoSettingsUpdateRequest, principal: AdminDep, session: DbSession
) -> AoSettingsOut:
    service = _service(session)
    effective = service.update_settings(
        principal.workspace_id,
        auto_route=body.auto_route,
        tier_model_overrides=body.tier_model_overrides,
        junior_max=body.junior_max,
        medior_max=body.medior_max,
        clear_junior_max=body.clear_junior_max,
        clear_medior_max=body.clear_medior_max,
    )
    session.commit()
    return _settings_out(effective)


@router.post(
    "/routing-preview",
    summary="What tier/model/strategy a sample task would get under this workspace's settings.",
)
def routing_preview(
    body: RoutingPreviewRequest, principal: ReaderDep, session: DbSession
) -> RoutingPreviewResponse:
    service = _service(session)
    preview = service.preview_routing(
        principal.workspace_id,
        kind=body.kind,
        priority=body.priority,
        blast_radius=body.blast_radius,
        file_count=body.file_count,
        repo_count=body.repo_count,
        requirement_count=body.requirement_count,
        acceptance_criteria_count=body.acceptance_criteria_count,
        touches_contracts=body.touches_contracts,
        touches_security=body.touches_security,
        dependency_count=body.dependency_count,
        open_questions_count=body.open_questions_count,
        underspecified=body.underspecified,
        provider=body.provider,
    )
    return RoutingPreviewResponse(
        tier=preview.tier,
        strategy=preview.strategy,
        score=preview.score,
        reasons=preview.reasons,
        model=preview.model,
        provider=preview.provider,
        junior_max=preview.junior_max,
        medior_max=preview.medior_max,
        auto_route_enabled=preview.auto_route_enabled,
    )
