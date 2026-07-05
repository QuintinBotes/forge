"""Deploy approval decisions (AC7, AC8, AC9, AC10)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from conftest import APPROVER2_ID, APPROVER_ID, MEMBER_ID, configure_pipeline
from fastapi.testclient import TestClient

from forge_contracts import UserRole


def _promote_to(client: TestClient, project_id: uuid.UUID, env: str, commit: str):
    return client.post(
        f"/projects/{project_id}/deployments",
        json={"environment": env, "commit_sha": commit},
    )


def _staging_awaiting(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> tuple[TestClient, str]:
    admin = client_factory(UserRole.ADMIN)
    configure_pipeline(admin, project_id)
    member = client_factory(UserRole.MEMBER, user_id=MEMBER_ID)
    _promote_to(member, project_id, "dev", "abc123")
    resp = _promote_to(member, project_id, "staging", "abc123")
    assert resp.json()["state"] == "awaiting_approval"
    return member, resp.json()["id"]


def test_restricted_creates_pending_approval(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    _member, dep_id = _staging_awaiting(client_factory, project_id)
    gate = client_factory(UserRole.MEMBER).get(f"/deployments/{dep_id}/gate").json()
    assert gate["requires_human_approval"] is True


def test_approve_drives_deploy(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    _member, dep_id = _staging_awaiting(client_factory, project_id)
    approver = client_factory(UserRole.MEMBER, user_id=APPROVER_ID)
    resp = approver.post(f"/deployments/{dep_id}/decision", json={"decision": "approve"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "succeeded"


def test_reject_blocks_deploy(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    _member, dep_id = _staging_awaiting(client_factory, project_id)
    approver = client_factory(UserRole.MEMBER, user_id=APPROVER_ID)
    resp = approver.post(f"/deployments/{dep_id}/decision", json={"decision": "reject"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "gate_rejected"


def test_no_self_approval_403(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member, dep_id = _staging_awaiting(client_factory, project_id)
    # The initiator (MEMBER_ID) cannot approve their own deployment.
    resp = member.post(f"/deployments/{dep_id}/decision", json={"decision": "approve"})
    assert resp.status_code == 403


def test_viewer_cannot_approve_403(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    _member, dep_id = _staging_awaiting(client_factory, project_id)
    viewer = client_factory(UserRole.VIEWER)
    resp = viewer.post(f"/deployments/{dep_id}/decision", json={"decision": "approve"})
    assert resp.status_code == 403


def test_multi_approval_two_distinct(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    admin = client_factory(UserRole.ADMIN)
    configure_pipeline(admin, project_id)
    member = client_factory(UserRole.MEMBER, user_id=MEMBER_ID)
    _promote_to(member, project_id, "dev", "abc123")
    # Approve staging (min 1) to reach production predecessor.
    s = _promote_to(member, project_id, "staging", "abc123").json()
    client_factory(UserRole.MEMBER, user_id=APPROVER_ID).post(
        f"/deployments/{s['id']}/decision", json={"decision": "approve"}
    )
    prod = _promote_to(member, project_id, "production", "abc123").json()
    assert prod["state"] == "awaiting_approval"
    a1 = client_factory(UserRole.MEMBER, user_id=APPROVER_ID)
    a2 = client_factory(UserRole.MEMBER, user_id=APPROVER2_ID)
    # First approval: still awaiting (min_approvals=2).
    r1 = a1.post(f"/deployments/{prod['id']}/decision", json={"decision": "approve"})
    assert r1.json()["state"] == "awaiting_approval"
    # Same approver again does not count.
    a1.post(f"/deployments/{prod['id']}/decision", json={"decision": "approve"})
    still = a1.get(f"/deployments/{prod['id']}").json()
    assert still["state"] == "awaiting_approval"
    # Second distinct approver advances it.
    r2 = a2.post(f"/deployments/{prod['id']}/decision", json={"decision": "approve"})
    assert r2.json()["state"] == "succeeded"
