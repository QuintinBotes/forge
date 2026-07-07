"""SSO admin routes (F33): SAML config + SCIM token management.

RBAC: every route requires the ``admin`` permission (403 for member / viewer /
agent-runner). Tenant isolation: the ``workspace_id`` in the path must be the
caller's workspace — a foreign workspace id returns 404 (no existence leak).
The SP private key is never serialized (``SsoConfigOut`` has no such field).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forge_api.auth.rbac import Permission
from forge_api.deps import DbSession, Principal, SettingsDep
from forge_api.routers._rbac import require_permission
from forge_api.sso.config_service import (
    ScimTokenCreated,
    ScimTokenInfo,
    SsoConfigService,
)
from forge_api.sso.errors import (
    DomainConflictError,
    LastAdminError,
    SamlValidationError,
    SsoConfigError,
)
from forge_api.sso.saml import SamlSpService
from forge_contracts.sso import SamlIdpConfig, SsoConfigIn, SsoConfigOut

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["sso"])

AdminPrincipal = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


def get_sso_config_service(session: DbSession, settings: SettingsDep) -> SsoConfigService:
    """Request-scoped config service (tests override this seam)."""
    return SsoConfigService(session, public_url=settings.public_url)


ConfigServiceDep = Annotated[SsoConfigService, Depends(get_sso_config_service)]


def _check_workspace(principal: Principal, workspace_id: uuid.UUID) -> None:
    if principal.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")


def _map_config_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, DomainConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "domain_conflict", "domain": exc.domain},
        )
    if isinstance(exc, LastAdminError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "last_admin", "message": str(exc)},
        )
    if isinstance(exc, SsoConfigError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    raise exc


class SamlTestRequest(BaseModel):
    """Body for the validation-only connection test."""

    saml_response: str = Field(min_length=1)


class SamlTestResult(BaseModel):
    """Parsed (but session-less) result of a test round trip."""

    name_id: str
    name_id_format: str
    issuer: str
    attributes: dict[str, list[str]]


class ScimTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    expires_at: datetime | None = None


@router.get("/sso", response_model=SsoConfigOut, summary="Read the workspace SAML config")
def get_sso_config(
    workspace_id: uuid.UUID,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    session: DbSession,
) -> SsoConfigOut:
    _check_workspace(principal, workspace_id)
    config = service.get_config(workspace_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no SSO configuration")
    return service.to_out(config)


@router.put("/sso", response_model=SsoConfigOut, summary="Create/replace the SAML config")
def put_sso_config(
    workspace_id: uuid.UUID,
    body: SsoConfigIn,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    session: DbSession,
) -> SsoConfigOut:
    _check_workspace(principal, workspace_id)
    try:
        config = service.put_config(workspace_id, body, actor_id=principal.user_id)
        session.commit()
    except (DomainConflictError, LastAdminError, SsoConfigError) as exc:
        session.rollback()
        raise _map_config_errors(exc) from exc
    return service.to_out(config)


def _set_enabled(
    workspace_id: uuid.UUID,
    enabled: bool,
    principal: Principal,
    service: SsoConfigService,
    session: Session,
) -> SsoConfigOut:
    _check_workspace(principal, workspace_id)
    try:
        config = service.set_enabled(workspace_id, enabled, actor_id=principal.user_id)
        session.commit()
    except (LastAdminError, SsoConfigError) as exc:
        session.rollback()
        if isinstance(exc, SsoConfigError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        raise _map_config_errors(exc) from exc
    return service.to_out(config)


@router.post("/sso/enable", response_model=SsoConfigOut, summary="Enable SSO")
def enable_sso(
    workspace_id: uuid.UUID,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    session: DbSession,
) -> SsoConfigOut:
    return _set_enabled(workspace_id, True, principal, service, session)


@router.post(
    "/sso/disable",
    response_model=SsoConfigOut,
    summary="Disable SSO (break-glass guarded)",
)
def disable_sso(
    workspace_id: uuid.UUID,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    session: DbSession,
) -> SsoConfigOut:
    return _set_enabled(workspace_id, False, principal, service, session)


@router.delete("/sso", status_code=status.HTTP_204_NO_CONTENT, summary="Delete the SAML config")
def delete_sso_config(
    workspace_id: uuid.UUID,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    session: DbSession,
) -> None:
    _check_workspace(principal, workspace_id)
    try:
        service.delete_config(workspace_id, actor_id=principal.user_id)
        session.commit()
    except SsoConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/sso/test",
    response_model=SamlTestResult,
    summary="Validation-only SAML round trip (never creates a session)",
)
def test_sso_config(
    workspace_id: uuid.UUID,
    body: SamlTestRequest,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    settings: SettingsDep,
) -> SamlTestResult:
    _check_workspace(principal, workspace_id)
    config = service.get_config(workspace_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no SSO configuration")
    idp = SamlIdpConfig(
        entity_id=config.idp_entity_id,
        sso_url=config.idp_sso_url,
        slo_url=config.idp_slo_url,
        x509_certs=list(config.idp_x509_certs),
        name_id_format=config.name_id_format,
    )
    try:
        assertion = SamlSpService().validate_response(
            saml_response_b64=body.saml_response,
            config=idp,
            sp_entity_id=config.sp_entity_id,
            acs_url=service.sp_urls(service.workspace_slug(workspace_id))["sp_acs_url"],
            want_signed=config.want_assertions_signed,
            expected_in_response_to=None,
            now=datetime.now(UTC),
            clock_skew_seconds=settings.saml_clock_skew_seconds,
        )
    except SamlValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "saml_validation_failed", "reason": exc.reason},
        ) from exc
    return SamlTestResult(
        name_id=assertion.name_id,
        name_id_format=assertion.name_id_format,
        issuer=assertion.issuer,
        attributes=assertion.attributes,
    )


# -- SCIM token management ---------------------------------------------------- #


@router.get("/scim/tokens", response_model=list[ScimTokenInfo], summary="List SCIM tokens")
def list_scim_tokens(
    workspace_id: uuid.UUID,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
) -> list[ScimTokenInfo]:
    _check_workspace(principal, workspace_id)
    return service.list_scim_tokens(workspace_id)


@router.post(
    "/scim/tokens",
    response_model=ScimTokenCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a SCIM bearer token (raw value returned exactly once)",
)
def create_scim_token(
    workspace_id: uuid.UUID,
    body: ScimTokenCreateRequest,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    session: DbSession,
    settings: SettingsDep,
) -> ScimTokenCreated:
    _check_workspace(principal, workspace_id)
    try:
        created = service.issue_scim_token(
            workspace_id,
            name=body.name,
            token_bytes=settings.scim_token_bytes,
            expires_at=body.expires_at,
            created_by=principal.user_id,
        )
        session.commit()
    except SsoConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return created


@router.delete(
    "/scim/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a SCIM token",
)
def revoke_scim_token(
    workspace_id: uuid.UUID,
    token_id: uuid.UUID,
    principal: AdminPrincipal,
    service: ConfigServiceDep,
    session: DbSession,
) -> None:
    _check_workspace(principal, workspace_id)
    if not service.revoke_scim_token(workspace_id, token_id, actor_id=principal.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    session.commit()
