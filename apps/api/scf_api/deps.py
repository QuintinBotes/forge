"""Shared FastAPI dependencies (auth principal, settings, DB session).

``get_current_principal`` is the authentication dependency every feature router
depends on. Phase-2 Task 2.3 wires it to the **real** auth layer (Task 1.15): it
verifies an API key (``Authorization: Bearer <key>`` or ``X-API-Key``) and
returns the resolved :class:`Principal` (role + scopes from the key), raising 401
when no valid credential is presented. A browser that signed in through OIDC or
SAML presents the same kind of key in the ``forge_session`` cookie instead (a
header always wins). There is no longer a hardcoded admin principal — the
previous stub left every endpoint unauthenticated.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Cookie, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection
from starlette.types import Scope

from scf_api.db import get_db
from scf_api.settings import Settings, get_settings
from scf_contracts import UserRole

#: The HTTP-only cookie ``routers.saml.start_session`` sets to the session key (OIDC, SAML
#: and the CLI sign-in link).
SESSION_COOKIE = "forge_session"


class Principal(BaseModel):
    """The authenticated actor for a request (user, agent-runner, or API key).

    Populated by :func:`get_current_principal` from a verified API key; ``role``
    and ``scopes`` reflect the key's RBAC grant.
    """

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    # Least-privileged default (HARD-09): a partially-constructed principal can
    # never silently be an admin. The real ``get_current_principal`` always sets
    # the role from the verified key, so this only hardens the failure mode.
    role: UserRole = UserRole.VIEWER
    email: str | None = None
    auth_method: str = "stub"
    scopes: list[str] = Field(default_factory=list)
    #: Whether the caller is a person who may decide approvals (spec L §6a): a
    #: session, or an API key bound to a person (``user_id``) that is unscoped or
    #: holds ``approvals:decide``. Unbound keys and agent tokens never decide; the
    #: owner's current role still bounds a key at decision time.
    decides: bool = False
    #: An API key minted with scopes: a viewer on every role check (an agent-runner
    #: with ``runs:execute``), and F30 sees only a workspace viewer grant, never its
    #: owner's grants.
    narrowed: bool = False
    #: Where the credential says the caller is (``web`` for a session, the key's
    #: surface or ``api``); never taken from a request header.
    surface: str | None = None
    #: The API key's id and expiry, so a decision stays traceable and a socket
    #: opened with it closes when the key does.
    credential_id: uuid.UUID | None = None
    credential_expires_at: datetime | None = None


def scope_session_cookie(scope: Scope) -> str | None:
    """The session cookie of a raw ASGI request, parsed as authentication parses it.

    For middleware that runs before authentication and must tell credentials apart.
    """
    return HTTPConnection(scope).cookies.get(SESSION_COOKIE) or None


def request_method(request: Request) -> str:
    """The HTTP method, so the session-cookie origin rule knows a read from a change."""
    return request.method


def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    origin: Annotated[str | None, Header()] = None,
    sec_fetch_site: Annotated[str | None, Header()] = None,
    method: Annotated[str | None, Depends(request_method)] = None,
    session: Annotated[Session | None, Depends(get_db)] = None,
) -> Principal:
    """Authenticate the caller via API key and return the resolved principal.

    Delegates to the real auth layer (``scf_api.auth.service``): the request
    must present a valid, unexpired API key (in a header, or the session cookie)
    whose role determines the principal's scopes. Missing or invalid credentials
    raise HTTP 401; a cookie-only state change from another origin raises 403.

    The auth-service import is deferred to call time on purpose:
    ``scf_api.auth.service`` imports :class:`Principal` from this module, so a
    module-level import would create a cycle.
    """
    from scf_api.auth.service import get_auth_service, get_authenticated_principal

    return get_authenticated_principal(
        get_auth_service(),
        authorization=authorization,
        x_api_key=x_api_key,
        session_cookie=session_cookie,
        origin=origin,
        sec_fetch_site=sec_fetch_site,
        method=method,
        session=session,
    )


# Ergonomic annotated aliases for use in router signatures.
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]
