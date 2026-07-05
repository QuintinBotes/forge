"""SCIM 2.0 service-provider routes (F33), mounted at ``/scim/v2``.

Authentication: a per-workspace bearer token (hashed at rest, constant-time
compared, revocable, expirable) resolved by :func:`require_scim_token`; every
route — including the discovery documents — returns a SCIM-shaped 401 without
one (AC12). The workspace always comes from the token, never the payload.
Responses use ``application/scim+json``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import JSONResponse

from forge_api.auth.service import get_auth_service
from forge_api.deps import DbSession, SettingsDep
from forge_api.sso.config_service import verify_scim_token
from forge_api.sso.errors import LastAdminError, ScimApiError
from forge_api.sso.scim_service import ScimGroupService, ScimUserService
from forge_contracts.sso import (
    GROUP_SCHEMA,
    USER_SCHEMA,
    ScimGroup,
    ScimListResponse,
    ScimPatchRequest,
    ScimUser,
)
from forge_db.models import ScimToken

SCIM_MEDIA_TYPE = "application/scim+json"


class ScimJSONResponse(JSONResponse):
    media_type = SCIM_MEDIA_TYPE


router = APIRouter(prefix="/scim/v2", tags=["scim"], default_response_class=ScimJSONResponse)


def require_scim_token(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> ScimToken:
    """Resolve the SCIM bearer token to its workspace-scoped record (401 fail)."""
    token_value: str | None = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            token_value = value.strip()
    if not token_value:
        raise ScimApiError(status.HTTP_401_UNAUTHORIZED, "missing SCIM bearer token")
    record = verify_scim_token(session, token_value)
    if record is None:
        raise ScimApiError(status.HTTP_401_UNAUTHORIZED, "invalid, revoked, or expired SCIM token")
    session.commit()  # persist last_used_at even when the request later fails
    return record


ScimTokenDep = Annotated[ScimToken, Depends(require_scim_token)]


def _revoke_sessions(workspace_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Revoke the user's Forge API keys / agent tokens (F37 session layer)."""
    return get_auth_service().api_keys.revoke_for_user(workspace_id, user_id)


def get_user_service(session: DbSession, settings: SettingsDep) -> ScimUserService:
    return ScimUserService(session, base_url=settings.public_url, revoke_sessions=_revoke_sessions)


def get_group_service(session: DbSession, settings: SettingsDep) -> ScimGroupService:
    return ScimGroupService(session, base_url=settings.public_url)


UserServiceDep = Annotated[ScimUserService, Depends(get_user_service)]
GroupServiceDep = Annotated[ScimGroupService, Depends(get_group_service)]


def _run(session, fn, *args, **kwargs):
    """Execute a SCIM service call; commit on success, map errors on failure."""
    try:
        result = fn(*args, **kwargs)
        session.commit()
        return result
    except ScimApiError:
        session.rollback()
        raise
    except LastAdminError as exc:
        session.rollback()
        raise ScimApiError(status.HTTP_409_CONFLICT, str(exc), scim_type="mutability") from exc


# -- discovery documents ------------------------------------------------------- #


@router.get("/ServiceProviderConfig", summary="SCIM capabilities")
def service_provider_config(token: ScimTokenDep, settings: SettingsDep) -> dict[str, Any]:
    base = settings.public_url.rstrip("/")
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "documentationUri": f"{base}/docs",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": "Per-workspace SCIM bearer token",
            }
        ],
    }


@router.get("/ResourceTypes", summary="SCIM resource types")
def resource_types(token: ScimTokenDep, settings: SettingsDep) -> dict[str, Any]:
    base = settings.public_url.rstrip("/")
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 2,
        "Resources": [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "User",
                "name": "User",
                "endpoint": "/Users",
                "schema": USER_SCHEMA,
                "meta": {"location": f"{base}/scim/v2/ResourceTypes/User"},
            },
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "Group",
                "name": "Group",
                "endpoint": "/Groups",
                "schema": GROUP_SCHEMA,
                "meta": {"location": f"{base}/scim/v2/ResourceTypes/Group"},
            },
        ],
    }


@router.get("/Schemas", summary="SCIM schemas")
def schemas(token: ScimTokenDep) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 2,
        "Resources": [
            {"id": USER_SCHEMA, "name": "User"},
            {"id": GROUP_SCHEMA, "name": "Group"},
        ],
    }


# -- /Users -------------------------------------------------------------------- #


@router.get("/Users", summary="List/search users")
def list_users(
    token: ScimTokenDep,
    service: UserServiceDep,
    session: DbSession,
    filter: Annotated[str | None, Query()] = None,
    startIndex: Annotated[int, Query(ge=1)] = 1,
    count: Annotated[int, Query(ge=0, le=200)] = 100,
) -> ScimListResponse:
    return _run(
        session,
        service.list,
        token.workspace_id,
        filter=filter,
        start_index=startIndex,
        count=count,
    )


@router.post("/Users", status_code=status.HTTP_201_CREATED, summary="Provision a user")
def create_user(
    body: ScimUser,
    token: ScimTokenDep,
    service: UserServiceDep,
    session: DbSession,
) -> ScimJSONResponse:
    resource: ScimUser = _run(session, service.create, token.workspace_id, body)
    return ScimJSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=resource.model_dump(mode="json", exclude_none=True),
        headers={"Location": resource.meta.location if resource.meta else ""},
    )


@router.get("/Users/{scim_id}", summary="Read a user")
def get_user(
    scim_id: str, token: ScimTokenDep, service: UserServiceDep, session: DbSession
) -> ScimUser:
    return _run(session, service.get, token.workspace_id, scim_id)


@router.put("/Users/{scim_id}", summary="Replace a user")
def replace_user(
    scim_id: str,
    body: ScimUser,
    token: ScimTokenDep,
    service: UserServiceDep,
    session: DbSession,
) -> ScimUser:
    return _run(session, service.replace, token.workspace_id, scim_id, body)


@router.patch("/Users/{scim_id}", summary="Patch a user (active=false deprovisions)")
def patch_user(
    scim_id: str,
    body: ScimPatchRequest,
    token: ScimTokenDep,
    service: UserServiceDep,
    session: DbSession,
) -> ScimUser:
    return _run(session, service.patch, token.workspace_id, scim_id, body)


@router.delete(
    "/Users/{scim_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deprovision a user",
)
def delete_user(
    scim_id: str, token: ScimTokenDep, service: UserServiceDep, session: DbSession
) -> None:
    _run(session, service.deactivate, token.workspace_id, scim_id)


# -- /Groups -------------------------------------------------------------------- #


@router.get("/Groups", summary="List groups")
def list_groups(
    token: ScimTokenDep,
    service: GroupServiceDep,
    session: DbSession,
    startIndex: Annotated[int, Query(ge=1)] = 1,
    count: Annotated[int, Query(ge=0, le=200)] = 100,
) -> ScimListResponse:
    return _run(session, service.list, token.workspace_id, start_index=startIndex, count=count)


@router.post("/Groups", status_code=status.HTTP_201_CREATED, summary="Create a group")
def create_group(
    body: ScimGroup,
    token: ScimTokenDep,
    service: GroupServiceDep,
    session: DbSession,
) -> ScimJSONResponse:
    resource: ScimGroup = _run(session, service.create, token.workspace_id, body)
    return ScimJSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=resource.model_dump(mode="json", exclude_none=True),
        headers={"Location": resource.meta.location if resource.meta else ""},
    )


@router.get("/Groups/{scim_id}", summary="Read a group")
def get_group(
    scim_id: str, token: ScimTokenDep, service: GroupServiceDep, session: DbSession
) -> ScimGroup:
    return _run(session, service.get, token.workspace_id, scim_id)


@router.put("/Groups/{scim_id}", summary="Replace a group")
def replace_group(
    scim_id: str,
    body: ScimGroup,
    token: ScimTokenDep,
    service: GroupServiceDep,
    session: DbSession,
) -> ScimGroup:
    return _run(session, service.replace, token.workspace_id, scim_id, body)


@router.patch("/Groups/{scim_id}", summary="Patch a group (membership ops)")
def patch_group(
    scim_id: str,
    body: ScimPatchRequest,
    token: ScimTokenDep,
    service: GroupServiceDep,
    session: DbSession,
) -> ScimGroup:
    return _run(session, service.patch, token.workspace_id, scim_id, body)


@router.delete(
    "/Groups/{scim_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a group",
)
def delete_group(
    scim_id: str, token: ScimTokenDep, service: GroupServiceDep, session: DbSession
) -> None:
    _run(session, service.delete, token.workspace_id, scim_id)


__all__ = ["require_scim_token", "router"]
