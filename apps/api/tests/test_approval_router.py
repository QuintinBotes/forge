"""Integration tests for the approval router (Phase 2 Task 2.1 wires ``/approval/*``).

Exercises the real handlers wired to a fresh in-memory :class:`ApprovalStore`:
create a request, list/get it, and record a decision; unknown ids -> 404.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.routers.approval import ApprovalStore, get_approval_store


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    store = ApprovalStore()
    app.dependency_overrides[get_approval_store] = lambda: store
    with TestClient(app) as c:
        yield c


def _create(client: TestClient) -> dict:
    resp = client.post(
        "/approval/requests",
        json={"gate": "pr", "title": "Approve PR for TASK-1", "confidence": 0.8},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_get(client: TestClient) -> None:
    created = _create(client)
    assert created["id"]
    assert created["status"] == "pending"
    fetched = client.get(f"/approval/requests/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Approve PR for TASK-1"


def test_list_requests(client: TestClient) -> None:
    _create(client)
    resp = client.get("/approval/requests")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    pending = client.get("/approval/requests", params={"status": "pending"})
    assert len(pending.json()) == 1


def test_decide_approves(client: TestClient) -> None:
    created = _create(client)
    resp = client.post(
        f"/approval/requests/{created['id']}/decision",
        # A forged ``decided_by`` in the body must be ignored: the decider is the
        # authenticated principal, not whatever the caller claims.
        json={"status": "approved", "decided_by": "alice", "reason": "looks good"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "test-principal@forge.local"
    assert body["decided_at"]


def test_get_unknown_is_404(client: TestClient) -> None:
    resp = client.get(f"/approval/requests/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_decide_unknown_is_404(client: TestClient) -> None:
    resp = client.post(
        f"/approval/requests/{uuid.uuid4()}/decision", json={"status": "approved"}
    )
    assert resp.status_code == 404
