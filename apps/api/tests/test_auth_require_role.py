"""F37 flat ``require_role`` dependency tests (AC13).

``require_role(ADMIN)`` rejects member/viewer/agent-runner (403) and allows
admin; ``require_role(MEMBER)`` allows member+admin, rejects viewer and
agent-runner. This is the back-compat contract F30's scoped resolver shims.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI

from forge_api.auth.service import (
    AuthService,
    get_auth_service,
    require_admin,
    require_role,
)
from forge_api.deps import Principal
from forge_contracts.enums import UserRole

WS = uuid.uuid4()

app_under_test = FastAPI()


AdminPrincipal = Annotated[Principal, Depends(require_admin)]
MemberPrincipal = Annotated[Principal, Depends(require_role(UserRole.MEMBER))]


@app_under_test.get("/admin-only")
def admin_only(principal: AdminPrincipal) -> dict[str, str]:
    return {"role": principal.role.value}


@app_under_test.get("/member-up")
def member_up(principal: MemberPrincipal) -> dict[str, str]:
    return {"role": principal.role.value}


@pytest.fixture
def service() -> Iterator[AuthService]:
    svc = AuthService(secret_key=b"5" * 32)
    app_under_test.dependency_overrides[get_auth_service] = lambda: svc
    try:
        yield svc
    finally:
        app_under_test.dependency_overrides.pop(get_auth_service, None)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app_under_test)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _token(service: AuthService, role: UserRole) -> dict[str, str]:
    _, token = service.bootstrap_key(workspace_id=WS, name=role.value, role=role)
    return {"Authorization": f"Bearer {token}"}


EXPECTED = {
    ("/admin-only", UserRole.ADMIN): 200,
    ("/admin-only", UserRole.MEMBER): 403,
    ("/admin-only", UserRole.VIEWER): 403,
    ("/admin-only", UserRole.AGENT_RUNNER): 403,
    ("/member-up", UserRole.ADMIN): 200,
    ("/member-up", UserRole.MEMBER): 200,
    ("/member-up", UserRole.VIEWER): 403,
    ("/member-up", UserRole.AGENT_RUNNER): 403,
}


@pytest.mark.parametrize(("route", "role"), list(EXPECTED))
async def test_role_matrix(
    client: httpx.AsyncClient, service: AuthService, route: str, role: UserRole
) -> None:
    resp = await client.get(route, headers=_token(service, role))
    assert resp.status_code == EXPECTED[(route, role)]


async def test_unauthenticated_is_401_not_403(
    client: httpx.AsyncClient, service: AuthService
) -> None:
    resp = await client.get("/admin-only")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
