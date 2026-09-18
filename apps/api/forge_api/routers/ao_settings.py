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
* ``GET  /ao/self-eval/status``     — the Self-Eval Gate facts: enforcement
  flag, the workspace's private suite, and the recorded baseline.
* ``POST /ao/self-eval/runs``       — enqueue the worker-owned
  ``forge.self_eval.run`` task for this workspace (admin, 202).

Reads are ``Permission.READ``-gated (a viewer may inspect its own workspace's
AO configuration); every mutation is ``Permission.ADMIN``-gated, matching the
cost price-book precedent (workspace-wide model/routing config is
admin-sensitive, not a per-member setting).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

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
    SelfEvalBaselineOut,
    SelfEvalRunAccepted,
    SelfEvalStatusOut,
    SelfEvalSuiteOut,
)
from forge_api.services import self_eval_service
from forge_api.services.ao_settings_service import (
    AoSettingsService,
    EffectiveAoSettings,
)
from forge_api.services.audit import SqlAuditWriter
from forge_api.services.self_eval_gate import get_self_eval_gate
from forge_api.settings import get_settings as get_app_settings
from forge_contracts.audit import AuditEvent
from forge_contracts.orchestration_config import AgentRole, EffectiveRoleConfig
from forge_db.ao_settings import SqlAoSettingsStore
from forge_db.models.benchmark import BenchmarkSuite, SelfEvalBaseline
from forge_db.role_config import SqlRoleConfigStore
from forge_eval.sweval import SelfEvalGate, SelfEvalRegressionError

GateDep = Annotated[SelfEvalGate, Depends(get_self_eval_gate)]

router = APIRouter(
    prefix="/ao",
    tags=["ao-settings"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


def _service(session: DbSession) -> AoSettingsService:
    return AoSettingsService(SqlRoleConfigStore(session), SqlAoSettingsStore(session))


def _audit(
    session: DbSession,
    principal: Principal,
    action: str,
    *,
    result: str,
    severity: str,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    SqlAuditWriter(session).emit(
        AuditEvent(
            workspace_id=principal.workspace_id,
            action=action,
            actor_id=principal.user_id,
            result=result,
            severity=severity,
            reason=reason,
            details=details or {},
        )
    )


async def _enforce_self_eval(
    gate: SelfEvalGate,
    session: DbSession,
    principal: Principal,
    proposed_config: dict[str, Any],
    *,
    force: bool,
) -> None:
    """Refuse a regressing config change when self-eval enforcement is enabled.

    A no-op unless ``self_eval_enforce`` is set. On a regression the block is
    audited (its own committed row) and a 409 is raised BEFORE the mutation is
    applied. A forced override is audited and allowed through; the forced-audit
    row commits atomically with the mutation the caller then applies.
    """
    if not get_app_settings().self_eval_enforce:
        return
    try:
        await gate.check_config(principal.workspace_id, proposed_config, force=force)
    except SelfEvalRegressionError as exc:
        _audit(
            session,
            principal,
            "ao.config.self_eval_blocked",
            result="denied",
            severity="warning",
            reason=str(exc),
            details={
                "resolution_rate": exc.scorecard.resolution_rate,
                "baseline_rate": exc.baseline_rate,
                "scope": proposed_config.get("scope"),
            },
        )
        session.commit()  # persist the block; the mutation is never applied
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "self_eval_regression",
                "message": str(exc),
                "resolution_rate": exc.scorecard.resolution_rate,
                "baseline_rate": exc.baseline_rate,
            },
        ) from exc
    if force:
        _audit(
            session,
            principal,
            "ao.config.self_eval_forced",
            result="success",
            severity="warning",
            reason="admin forced a config change past the Self-Eval Gate",
            details={"scope": proposed_config.get("scope")},
        )


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
async def upsert_role_config(
    role: AgentRole,
    body: RoleConfigUpsertRequest,
    principal: AdminDep,
    session: DbSession,
    gate: GateDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    force: Annotated[bool, Query()] = False,
) -> RoleConfigOut:
    await _enforce_self_eval(
        gate,
        session,
        principal,
        {
            "scope": "ao.role_config",
            "role": role.value,
            "model": body.model_or_tier,
            "effort": body.effort.value,
            "project_id": str(project_id) if project_id else None,
        },
        force=force,
    )
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
async def update_settings(
    body: AoSettingsUpdateRequest,
    principal: AdminDep,
    session: DbSession,
    gate: GateDep,
    force: Annotated[bool, Query()] = False,
) -> AoSettingsOut:
    await _enforce_self_eval(
        gate,
        session,
        principal,
        {
            "scope": "ao.settings",
            "auto_route": body.auto_route,
            "tier_model_overrides": body.tier_model_overrides,
            "junior_max": body.junior_max,
            "medior_max": body.medior_max,
        },
        force=force,
    )
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


# --------------------------------------------------------------------------- #
# Self-Eval Gate: status read + run trigger (Phase A)                          #
# --------------------------------------------------------------------------- #


def _private_suite(
    session: Session, workspace_id: uuid.UUID, *, published_only: bool = False
) -> BenchmarkSuite | None:
    """The workspace's private Self-Eval suite (published preferred), or ``None``."""
    stmt = select(BenchmarkSuite).where(
        BenchmarkSuite.workspace_id == workspace_id,
        BenchmarkSuite.private.is_(True),
    )
    if published_only:
        stmt = stmt.where(BenchmarkSuite.published.is_(True))
    stmt = stmt.order_by(BenchmarkSuite.published.desc(), BenchmarkSuite.version.desc())
    return session.scalars(stmt).first()


def _effective_config_snapshot(session: Session, workspace_id: uuid.UUID) -> dict[str, Any]:
    """The redacted effective AO settings a self-eval run scores (no secrets)."""
    effective = _service(session).get_settings(workspace_id)
    return {
        "scope": "ao.settings",
        "auto_route": effective.auto_route,
        "tier_model_overrides": effective.tier_model_overrides,
        "junior_max": effective.junior_max,
        "medior_max": effective.medior_max,
    }


@router.get(
    "/self-eval/status",
    summary="Self-Eval Gate facts: enforcement flag, private suite, recorded baseline.",
)
def self_eval_status(principal: ReaderDep, session: DbSession) -> SelfEvalStatusOut:
    suite = _private_suite(session, principal.workspace_id)
    baseline = session.scalars(
        select(SelfEvalBaseline)
        .where(SelfEvalBaseline.workspace_id == principal.workspace_id)
        .order_by(SelfEvalBaseline.updated_at.desc())
    ).first()
    return SelfEvalStatusOut(
        workspace_id=principal.workspace_id,
        enforced=get_app_settings().self_eval_enforce,
        suite=SelfEvalSuiteOut(
            id=suite.id,
            slug=suite.slug,
            version=suite.version,
            title=suite.title,
            task_count=suite.task_count,
            repo_id=suite.repo_id,
            published=suite.published,
        )
        if suite is not None
        else None,
        baseline=SelfEvalBaselineOut(
            benchmark_suite_id=baseline.benchmark_suite_id,
            baseline_rate=baseline.baseline_rate,
            resolved=baseline.resolved,
            total=baseline.total,
            recorded_at=baseline.updated_at,
        )
        if baseline is not None
        else None,
    )


@router.post(
    "/self-eval/runs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue the worker-owned self-eval run for this workspace (admin).",
)
def request_self_eval_run(principal: AdminDep, session: DbSession) -> SelfEvalRunAccepted:
    """Queue ``forge.self_eval.run`` over the workspace's private suite.

    The run itself stays in the worker (minutes-long, agent-driven, A4) — this
    endpoint only enqueues it with the workspace's current effective AO config
    snapshot. 409 when no published private suite exists: there is nothing the
    worker could score, so we refuse rather than queue a guaranteed no-op.
    """
    suite = _private_suite(session, principal.workspace_id, published_only=True)
    if suite is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "no_private_suite",
                "message": (
                    "No published private Self-Eval suite exists for this "
                    "workspace; one is minted from merged PRs by the "
                    "forge.self_eval.mint worker task."
                ),
            },
        )
    self_eval_service.enqueue_self_eval_run(
        principal.workspace_id,
        _effective_config_snapshot(session, principal.workspace_id),
        recorded_by=principal.user_id,
    )
    _audit(
        session,
        principal,
        "ao.self_eval.run_requested",
        result="success",
        severity="info",
        details={"benchmark_suite_id": str(suite.id)},
    )
    session.commit()
    return SelfEvalRunAccepted(
        task=self_eval_service.SELF_EVAL_RUN_TASK,
        workspace_id=principal.workspace_id,
        benchmark_suite_id=suite.id,
    )
