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
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.main import create_app
from forge_api.routers.spec import get_spec_engine
from forge_db.base import Base
from forge_db.models import Workspace
from forge_spec import FileSpecEngine, spec_id_for_key

#: Mirrors ``conftest.py``'s deterministic test workspace (tests mirror rather
#: than cross-import conftest constants, per repo convention).
_TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def client(tmp_path: Path, authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    engine = FileSpecEngine(root=tmp_path / "specs")

    # ss-versioning: every save also records a ``spec_version`` row, so the
    # write endpoints now need a DB session (SQLite in-memory here, mirroring
    # ``test_project_spec_overview.py``'s hermetic fixture).
    db_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(db_engine)
    db_factory = sessionmaker(bind=db_engine, expire_on_commit=False, class_=Session)
    with db_factory() as session:
        session.add(Workspace(id=_TEST_WORKSPACE_ID, name="Acme", slug="acme"))
        session.commit()

    def _override_db() -> Iterator[Session]:
        session = db_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_spec_engine] = lambda: engine
    app.dependency_overrides[get_db] = _override_db
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


def test_read_missing_constitution_is_404(client: TestClient) -> None:
    resp = client.get(f"/spec/constitution/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_read_constitution_after_init(client: TestClient) -> None:
    project_id = uuid.uuid4()
    init = client.post("/spec/constitution", json={"project_id": str(project_id)})
    assert init.status_code == 201, init.text

    resp = client.get(f"/spec/constitution/{project_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == str(project_id)
    assert resp.json()["principles"] == init.json()["principles"]


def test_read_spec_markdown_round_trips_the_manifest(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    resp = client.get(f"/spec/specs/{spec_uuid}/markdown")

    assert resp.status_code == 200, resp.text
    assert "text/plain" in resp.headers["content-type"]
    text = resp.text
    assert manifest["id"] in text
    assert "Customer search" in text
    assert "R1" in text


def test_edit_spec_via_markdown_updates_the_manifest(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])
    text = client.get(f"/spec/specs/{spec_uuid}/markdown").text

    edited = text.replace(
        "- **R1**: Search customers by name",
        "- **R1**: Search customers by name or email",
    )
    resp = client.put(f"/spec/specs/{spec_uuid}/markdown", json={"content": edited})

    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["requirements"][0]["text"] == "Search customers by name or email"

    # manifest.yaml was re-rendered to match.
    yaml_text = client.get(f"/spec/specs/{spec_uuid}/manifest").text
    assert "Search customers by name or email" in yaml_text


def test_read_missing_spec_markdown_is_404(client: TestClient) -> None:
    resp = client.get(f"/spec/specs/{uuid.uuid4()}/markdown")
    assert resp.status_code == 404


def test_read_spec_manifest_yaml_round_trips_the_manifest(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    resp = client.get(f"/spec/specs/{spec_uuid}/manifest")

    assert resp.status_code == 200, resp.text
    assert "text/plain" in resp.headers["content-type"]
    assert f"id: {manifest['id']}" in resp.text
    assert "Search customers by name" in resp.text


def test_create_spec_via_manifest_yaml(client: TestClient) -> None:
    """Creating a spec straight from ``manifest.yaml`` (both formats writable)."""
    yaml_body = (
        "id: SPEC-99\n"
        "name: Billing v2\n"
        "status: draft\n"
        "requirements:\n"
        "  - id: R1\n"
        "    text: Charge a card\n"
    )
    spec_uuid = spec_id_for_key("SPEC-99")

    resp = client.put(f"/spec/specs/{spec_uuid}/manifest", json={"content": yaml_body})

    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["id"] == "SPEC-99"
    assert created["name"] == "Billing v2"

    fetched = client.get(f"/spec/specs/{spec_uuid}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Billing v2"

    # spec.md was rendered to match the YAML-authored manifest.
    md_text = client.get(f"/spec/specs/{spec_uuid}/markdown").text
    assert "Billing v2" in md_text
    assert "Charge a card" in md_text


def test_edit_spec_via_manifest_yaml_updates_the_manifest(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])
    yaml_text = client.get(f"/spec/specs/{spec_uuid}/manifest").text

    edited = yaml_text.replace(
        "text: Search customers by name", "text: Search customers by name, email, or phone"
    )
    resp = client.put(f"/spec/specs/{spec_uuid}/manifest", json={"content": edited})

    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["requirements"][0]["text"] == "Search customers by name, email, or phone"

    # spec.md was re-rendered to match.
    md_text = client.get(f"/spec/specs/{spec_uuid}/markdown").text
    assert "Search customers by name, email, or phone" in md_text


def test_read_missing_spec_manifest_yaml_is_404(client: TestClient) -> None:
    resp = client.get(f"/spec/specs/{uuid.uuid4()}/manifest")
    assert resp.status_code == 404


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
