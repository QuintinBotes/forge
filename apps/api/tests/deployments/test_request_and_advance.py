"""Request + advance, idempotency, active-dup conflict (AC4, AC4a, AC17)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from conftest import configure_pipeline
from fastapi.testclient import TestClient

from forge_contracts import UserRole


def _setup(client_factory: Callable[..., TestClient], project_id: uuid.UUID) -> TestClient:
    admin = client_factory(UserRole.ADMIN)
    configure_pipeline(admin, project_id)
    return client_factory(UserRole.MEMBER)


def test_request_dev_advances_without_approval(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    resp = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "dev", "commit_sha": "abc123"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["state"] == "succeeded"
    # No approval was needed; the detail timeline shows it passed through deploying.
    detail = member.get(f"/deployments/{data['id']}").json()
    assert any(t["to_state"] == "deploying" for t in detail["transitions"])


def test_idempotent_request_returns_same(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    body = {"environment": "dev", "commit_sha": "abc123", "idempotency_key": "dev:abc123"}
    first = member.post(f"/projects/{project_id}/deployments", json=body)
    second = member.post(f"/projects/{project_id}/deployments", json=body)
    assert first.status_code == 201
    assert second.json()["id"] == first.json()["id"]


def test_active_duplicate_conflict_409(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    # Promote dev so staging predecessor is satisfied.
    member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "dev", "commit_sha": "abc123"},
    )
    # Staging is restricted -> stays awaiting_approval (active).
    first = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "staging", "commit_sha": "abc123"},
    )
    assert first.status_code == 201
    assert first.json()["state"] == "awaiting_approval"
    dup = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "staging", "commit_sha": "abc123"},
    )
    assert dup.status_code == 409


def test_predecessor_not_ready_gate_rejected(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    member = _setup(client_factory, project_id)
    # Request production for def456 with no staging success -> gate_rejected.
    resp = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "production", "commit_sha": "def456"},
    )
    assert resp.status_code == 201
    assert resp.json()["state"] == "gate_rejected"
