"""RBAC + tenant isolation for the PM router (AC20)."""

from __future__ import annotations

import uuid

from forge_contracts import UserRole


def _payload(project_id: uuid.UUID, **overrides) -> dict:
    base = {
        "provider": "linear",
        "name": "Eng",
        "project_id": str(project_id),
        "external_project_key": "ENG",
        "auth_type": "api_token",
        "api_token": "tok",
    }
    base.update(overrides)
    return base


def test_viewer_can_read_but_not_mutate(client_factory, project_id) -> None:
    admin = client_factory(role=UserRole.ADMIN)
    created = admin.post("/integrations/pm/connections", json=_payload(project_id)).json()

    viewer = client_factory(role=UserRole.VIEWER)
    assert viewer.get("/integrations/pm/connections").status_code == 200
    assert viewer.get(f"/integrations/pm/connections/{created['id']}").status_code == 200
    # mutations forbidden
    assert viewer.post(
        "/integrations/pm/connections", json=_payload(project_id)
    ).status_code == 403
    assert viewer.patch(
        f"/integrations/pm/connections/{created['id']}", json={"name": "x"}
    ).status_code == 403
    assert viewer.delete(
        f"/integrations/pm/connections/{created['id']}"
    ).status_code == 403
    assert viewer.post(
        f"/integrations/pm/connections/{created['id']}/test"
    ).status_code == 403


def test_member_cannot_create_but_can_read(client_factory, project_id) -> None:
    admin = client_factory(role=UserRole.ADMIN)
    admin.post("/integrations/pm/connections", json=_payload(project_id))

    member = client_factory(role=UserRole.MEMBER)
    assert member.get("/integrations/pm/connections").status_code == 200
    # create/patch/delete are admin-only (MANAGE_SECRETS)
    assert member.post(
        "/integrations/pm/connections", json=_payload(project_id)
    ).status_code == 403


def test_only_admin_can_create(client_factory, project_id) -> None:
    admin = client_factory(role=UserRole.ADMIN)
    assert admin.post(
        "/integrations/pm/connections", json=_payload(project_id)
    ).status_code == 201


def test_cross_workspace_returns_404_not_403(client_factory, project_id) -> None:
    from .conftest import OTHER_WORKSPACE_ID

    admin = client_factory(role=UserRole.ADMIN)
    created = admin.post("/integrations/pm/connections", json=_payload(project_id)).json()

    other_admin = client_factory(role=UserRole.ADMIN, workspace_id=OTHER_WORKSPACE_ID)
    # admin in another workspace: authorized by role, but the row is invisible.
    assert other_admin.get(
        f"/integrations/pm/connections/{created['id']}"
    ).status_code == 404
    assert other_admin.delete(
        f"/integrations/pm/connections/{created['id']}"
    ).status_code == 404
