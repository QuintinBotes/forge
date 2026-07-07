"""SAML 2.0 protocol surface (F33): metadata, SP-initiated login, ACS, HRD.

Intentionally unauthenticated — the (signed, replay-guarded) ``SAMLResponse``
*is* the authentication. The workspace is resolved from the URL slug, never
from attacker-controlled assertion fields (confused-deputy guard).

Session establishment note (foundation conformance): the F37 substrate's
session layer is the in-memory API-key store, so a successful ACS mints a
short-lived Forge API key bound to the provisioned user and sets it as an
HttpOnly cookie before redirecting to the RelayState target. Deprovisioning
revokes these via ``APIKeyStore.revoke_for_user``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from forge_api.auth.service import get_auth_service
from forge_api.deps import DbSession, SettingsDep
from forge_api.settings import Settings
from forge_api.sso.attribute_mapping import map_assertion
from forge_api.sso.config_service import SsoConfigService
from forge_api.sso.errors import SamlValidationError, SsoConfigError
from forge_api.sso.provisioning import emit_sso_audit, link_or_jit_provision
from forge_api.sso.replay import InMemoryReplayGuard
from forge_api.sso.saml import SamlSpService, peek_in_response_to
from forge_api.sso.saml_metadata import render_sp_metadata
from forge_contracts.enums import APIKeyKind, UserRole
from forge_contracts.sso import AttributeMapping, ReplayGuard, SamlIdpConfig
from forge_db.models import SsoConfiguration, Workspace

router = APIRouter(prefix="/auth/saml", tags=["saml"])

#: Lifetime of the session credential minted at the ACS.
SESSION_TTL = timedelta(hours=12)

# Process-wide replay guard (single-process default; Redis-backed for
# multi-process deployments is parked — see forge_api.sso.replay).
_replay_guard = InMemoryReplayGuard()


def get_replay_guard() -> ReplayGuard:
    """Dependency seam so tests inject a fresh/fake guard."""
    return _replay_guard


ReplayGuardDep = Annotated[ReplayGuard, Depends(get_replay_guard)]


def get_saml_service() -> SamlSpService:
    return SamlSpService()


SamlServiceDep = Annotated[SamlSpService, Depends(get_saml_service)]


class DiscoverRequest(BaseModel):
    email: str = Field(min_length=3)


class DiscoverResponse(BaseModel):
    sso: bool
    redirect: str | None = None


def _load(
    session: Session, settings: Settings, slug: str
) -> tuple[Workspace, SsoConfiguration, SsoConfigService]:
    service = SsoConfigService(session, public_url=settings.public_url)
    try:
        pair = service.get_config_by_slug(slug)
    except OperationalError as exc:
        # Fail closed, typed (skeleton invariant: anonymous routes never emit an
        # un-typed 500): when the identity store is unreachable no realm can be
        # resolved and no SSO login is possible.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO realm unavailable (identity store unreachable)",
        ) from exc
    if pair is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown SSO realm")
    workspace, config = pair
    return workspace, config, service


def _idp(config: SsoConfiguration) -> SamlIdpConfig:
    return SamlIdpConfig(
        entity_id=config.idp_entity_id,
        sso_url=config.idp_sso_url,
        slo_url=config.idp_slo_url,
        x509_certs=list(config.idp_x509_certs),
        name_id_format=config.name_id_format,
    )


def _require_enabled(config: SsoConfiguration) -> None:
    if not config.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="SSO is disabled for this workspace"
        )


def _safe_next(target: str | None) -> str:
    """Only same-origin relative paths are honoured (open-redirect guard)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


@router.get("/discover", include_in_schema=False)
def discover_get() -> Response:  # pragma: no cover - convenience 405 guard
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)


@router.post("/discover", response_model=DiscoverResponse, summary="Home-realm discovery")
def discover(body: DiscoverRequest, session: DbSession, settings: SettingsDep) -> DiscoverResponse:
    service = SsoConfigService(session, public_url=settings.public_url)
    slug = service.discover(body.email)
    if slug is None:
        return DiscoverResponse(sso=False)
    return DiscoverResponse(sso=True, redirect=f"/auth/saml/{slug}/login")


@router.get("/{slug}/metadata", summary="SP metadata XML")
def sp_metadata(slug: str, session: DbSession, settings: SettingsDep) -> Response:
    workspace, config, service = _load(session, settings, slug)
    urls = service.sp_urls(workspace.slug)
    xml = render_sp_metadata(
        sp_entity_id=config.sp_entity_id,
        acs_url=urls["sp_acs_url"],
        slo_url=urls["sp_slo_url"],
        sp_cert_pem=config.sp_cert_pem,
        want_assertions_signed=config.want_assertions_signed,
        authn_requests_signed=config.sign_authn_requests,
        name_id_format=config.name_id_format,
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/{slug}/login", summary="SP-initiated login (302 to the IdP)")
def sp_login(
    slug: str,
    session: DbSession,
    settings: SettingsDep,
    saml: SamlServiceDep,
    guard: ReplayGuardDep,
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    workspace, config, service = _load(session, settings, slug)
    _require_enabled(config)
    urls = service.sp_urls(workspace.slug)
    sp_key = service.decrypt_sp_key(config) if config.sign_authn_requests else None
    redirect_url, request_id = saml.build_authn_request(
        _idp(config),
        sp_entity_id=config.sp_entity_id,
        acs_url=urls["sp_acs_url"],
        relay_state=_safe_next(next),
        sign=config.sign_authn_requests,
        sp_private_key_pem=sp_key,
    )
    guard.register_request(request_id, settings.saml_authnrequest_ttl_seconds)
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.post("/{slug}/acs", summary="Assertion Consumer Service")
def acs(
    slug: str,
    session: DbSession,
    settings: SettingsDep,
    saml: SamlServiceDep,
    guard: ReplayGuardDep,
    SAMLResponse: Annotated[str, Form()],
    RelayState: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    workspace, config, service = _load(session, settings, slug)
    _require_enabled(config)
    urls = service.sp_urls(workspace.slug)
    now = datetime.now(UTC)

    def _login_failed(reason: str, http_status: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
        emit_sso_audit(
            session,
            workspace_id=workspace.id,
            action="sso.login_failed",
            actor_type="idp",
            result="failure",
            details={"reason": reason},
        )
        session.commit()
        return HTTPException(
            status_code=http_status,
            detail={"error": "saml_login_failed", "reason": reason},
        )

    try:
        in_response_to = peek_in_response_to(SAMLResponse)
    except SamlValidationError as exc:
        raise _login_failed(exc.reason) from exc

    if in_response_to is None:
        if not config.allow_idp_initiated:
            raise _login_failed("idp_initiated_disabled")
    elif not guard.consume_request(in_response_to):
        raise _login_failed("unknown_in_response_to")

    try:
        assertion = saml.validate_response(
            saml_response_b64=SAMLResponse,
            config=_idp(config),
            sp_entity_id=config.sp_entity_id,
            acs_url=urls["sp_acs_url"],
            want_signed=config.want_assertions_signed,
            expected_in_response_to=in_response_to,
            now=now,
            clock_skew_seconds=settings.saml_clock_skew_seconds,
        )
    except SamlValidationError as exc:
        raise _login_failed(exc.reason) from exc

    ttl = settings.saml_clock_skew_seconds + max(
        0, int((assertion.not_on_or_after - now).total_seconds())
    )
    if guard.seen_assertion(assertion.assertion_id, ttl):
        raise _login_failed("assertion_replayed")

    identity = map_assertion(
        assertion,
        mapping=AttributeMapping.model_validate(config.attribute_mapping or {}),
        group_role_map=dict(config.group_role_map or {}),
        default_role=config.default_role.value,
    )
    try:
        user = link_or_jit_provision(session=session, config=config, identity=identity)
    except SsoConfigError as exc:
        raise _login_failed("provisioning_rejected", status.HTTP_403_FORBIDDEN) from exc

    emit_sso_audit(
        session,
        workspace_id=workspace.id,
        action="sso.login",
        actor_id=user.id,
        actor_type="user",
        target_type="user",
        target_id=user.id,
        details={"name_id_format": assertion.name_id_format},
    )
    session.commit()

    _info, token = get_auth_service().bootstrap_key(
        workspace_id=workspace.id,
        name=f"sso-session-{uuid.uuid4().hex[:8]}",
        role=UserRole(user.role.value),
        user_id=user.id,
        kind=APIKeyKind.SYSTEM,
        expires_at=now + SESSION_TTL,
    )
    response = RedirectResponse(url=_safe_next(RelayState), status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        "forge_session",
        token,
        httponly=True,
        secure=settings.public_url.startswith("https://"),
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return response


@router.get("/{slug}/slo", summary="Single Logout (parked)")
@router.post("/{slug}/slo", summary="Single Logout (parked)")
def slo(slug: str, session: DbSession, settings: SettingsDep) -> Response:
    """PARKED: SLO choreography needs the (future) browser session store.

    The endpoint exists so SP metadata advertises a stable SLO location; it
    honestly reports 501 instead of pretending to terminate sessions.
    """
    _load(session, settings, slug)  # 404 for unknown realms
    return Response(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content="SAML Single Logout is not implemented yet",
    )
