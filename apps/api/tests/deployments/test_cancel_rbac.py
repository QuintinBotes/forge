"""Cancel + RBAC + tenant isolation (AC20, AC22)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from conftest import MEMBER_ID, OTHER_WS_ID, configure_pipeline
from fastapi.testclient import TestClient

from forge_contracts import UserRole


def _setup(client_factory: Callable[..., TestClient], project_id: uuid.UUID):
    admin = client_factory(UserRole.ADMIN)
    configure_pipeline(admin, project_id)
    return client_factory(UserRole.MEMBER, user_id=MEMBER_ID)


def _awaiting_staging(member: TestClient, project_id: uuid.UUID) -> str:
    member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "dev", "commit_sha": "abc123"},
    )
    return member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "staging", "commit_sha": "abc123"},
    ).json()["id"]


def test_cancel_non_terminal_by_initiator(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    dep_id = _awaiting_staging(member, project_id)
    resp = member.post(f"/deployments/{dep_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelled"


def test_cancel_terminal_409(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    dep = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "dev", "commit_sha": "abc123"},
    ).json()
    assert dep["state"] == "succeeded"
    resp = member.post(f"/deployments/{dep['id']}/cancel")
    assert resp.status_code == 409


def test_viewer_read_only(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    _setup(client_factory, project_id)
    viewer = client_factory(UserRole.VIEWER)
    assert viewer.get(f"/projects/{project_id}/pipeline").status_code == 200
    assert (
        viewer.post(
            f"/projects/{project_id}/deployments",
            json={"environment": "dev", "commit_sha": "abc123"},
        ).status_code
        == 403
    )


def test_member_cannot_upsert_pipeline(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    assert configure_pipeline(member, project_id, version=1).status_code == 403


def test_cross_workspace_is_404(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    dep_id = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "dev", "commit_sha": "abc123"},
    ).json()["id"]
    other = client_factory(UserRole.MEMBER, workspace_id=OTHER_WS_ID)
    assert other.get(f"/deployments/{dep_id}").status_code == 404
