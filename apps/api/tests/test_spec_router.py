"""Integration tests for the spec router (Phase 2 Task 2.1 wires ``/spec/*``).

Exercises the real handlers wired to a tmp-rooted :class:`~forge_spec.FileSpecEngine`:
the SDD lifecycle (constitution -> create -> clarify -> plan -> approve -> tasks),
manifest read, and gate enforcement (tasks before approval -> 409).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.routers.spec import get_spec_engine
from forge_spec import FileSpecEngine, spec_id_for_key


@pytest.fixture
def client(
    tmp_path: Path, authenticate_app: Callable[..., FastAPI]
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    engine = FileSpecEngine(root=tmp_path / "specs")
    app.dependency_overrides[get_spec_engine] = lambda: engine
    with TestClient(app) as c:
        yield c


def _create_spec(client: TestClient) -> dict:
    resp = client.post(
        "/spec/specs",
        json={
            "epic_id": str(uuid.uuid4()),
            "name": "Customer search",
            "requirements": [{"id": "R1", "text": "Search customers by name"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_constitution_init(client: TestClient) -> None:
    resp = client.post("/spec/constitution", json={"project_id": str(uuid.uuid4())})
    assert resp.status_code == 201, resp.text
    assert resp.json()["principles"]


def test_create_and_read_manifest(client: TestClient) -> None:
    manifest = _create_spec(client)
    assert manifest["status"] == "draft"
    spec_uuid = spec_id_for_key(manifest["id"])
    fetched = client.get(f"/spec/specs/{spec_uuid}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Customer search"


def test_read_missing_manifest_is_404(client: TestClient) -> None:
    resp = client.get(f"/spec/specs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_tasks_before_approval_is_gated_409(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])
    resp = client.post(f"/spec/specs/{spec_uuid}/tasks")
    assert resp.status_code == 409


def test_lifecycle_clarify_plan_approve_tasks(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    assert client.post(f"/spec/specs/{spec_uuid}/clarify").status_code == 200
    assert client.post(f"/spec/specs/{spec_uuid}/plan").status_code == 200

    approved = client.post(f"/spec/specs/{spec_uuid}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    tasks = client.post(f"/spec/specs/{spec_uuid}/tasks")
    assert tasks.status_code == 200, tasks.text
    assert isinstance(tasks.json(), list)
    assert len(tasks.json()) >= 1
