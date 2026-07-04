"""Shared FastAPI dependencies (auth principal, settings, DB session).

``get_current_principal`` is the authentication dependency every feature router
depends on. Phase-2 Task 2.3 wires it to the **real** auth layer (Task 1.15): it
verifies an API key (``Authorization: Bearer <key>`` or ``X-API-Key``) and
returns the resolved :class:`Principal` (role + scopes from the key), raising 401
when no valid credential is presented. There is no longer a hardcoded admin
principal — the previous stub left every endpoint unauthenticated.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forge_api.db import get_db
from forge_api.settings import Settings, get_settings
from forge_contracts import UserRole


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


def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Authenticate the caller via API key and return the resolved principal.

    Delegates to the real auth layer (``forge_api.auth.service``): the request
    must present a valid, unexpired API key whose role determines the principal's
    scopes. Missing or invalid credentials raise HTTP 401.

    The auth-service import is deferred to call time on purpose:
    ``forge_api.auth.service`` imports :class:`Principal` from this module, so a
    module-level import would create a cycle.
    """
    from forge_api.auth.service import get_auth_service, get_authenticated_principal

    return get_authenticated_principal(
        get_auth_service(), authorization=authorization, x_api_key=x_api_key
    )


# Ergonomic annotated aliases for use in router signatures.
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]
