"""Integration tests for ``GET /projects/{project_id}/specs`` (spec-overview-endpoint).

Covers the F23 spec-validation dashboard projection consumed by the web
``/specs`` dashboard (``getProjectSpecOverview``): the project's constitution
plus every linked spec manifest enriched with its latest validation report.

Hermetic: SQLite in-memory backs the ``Project`` existence check (the one real
"project" entity in the system); the board is the hermetic
``InMemoryBoardService`` (an epic's ``spec_id`` is how a spec is linked to a
project today); the spec engine is a ``FileSpecEngine`` rooted at ``tmp_path``.
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

from forge_api.deps import get_db
from forge_api.main import create_app
from forge_api.routers.board import get_board_service
from forge_api.routers.spec import get_spec_engine
from forge_board import InMemoryBoardService
from forge_contracts import EpicDTO
from forge_db.base import Base
from forge_db.models import Project, Workspace
from forge_spec import FileSpecEngine, spec_id_for_key

WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def db_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.add(Workspace(id=WORKSPACE_ID, name="Acme", slug="acme"))
        session.commit()
    yield factory


def _make_project(db_factory: sessionmaker[Session], *, key: str = "PROJ-1") -> uuid.UUID:
    project_id = uuid.uuid4()
    with db_factory() as session:
        session.add(Project(id=project_id, workspace_id=WORKSPACE_ID, name="Project", key=key))
        session.commit()
    return project_id


@pytest.fixture
def client(
    tmp_path: Path,
    authenticate_app: Callable[..., FastAPI],
    db_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    engine = FileSpecEngine(root=tmp_path / "specs")
    board = InMemoryBoardService()

    def _override_db() -> Iterator[Session]:
        session = db_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_spec_engine] = lambda: engine
    app.dependency_overrides[get_board_service] = lambda: board
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c


def test_unknown_project_is_404(client: TestClient) -> None:
    resp = client.get(f"/projects/{uuid.uuid4()}/specs")
    assert resp.status_code == 404


def test_empty_project_returns_empty_dashboard(
    client: TestClient, db_factory: sessionmaker[Session]
) -> None:
    project_id = _make_project(db_factory)

    resp = client.get(f"/projects/{project_id}/specs")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == str(project_id)
    assert body["constitution"] is None
    assert body["specs"] == []


def test_populated_project_returns_constitution_and_specs(
    client: TestClient, db_factory: sessionmaker[Session]
) -> None:
    project_id = _make_project(db_factory)

    client.post("/spec/constitution", json={"project_id": str(project_id)})

    created = client.post(
        "/spec/specs",
        json={
            "epic_id": str(uuid.uuid4()),
            "name": "Customer search",
            "requirements": [{"id": "R1", "text": "Search customers by name"}],
        },
    )
    assert created.status_code == 201, created.text
    manifest = created.json()
    spec_uuid = spec_id_for_key(manifest["id"])

    # Link the spec back to the project the only way the system does today:
    # an epic in the project carrying the spec's deterministic uuid.
    board: InMemoryBoardService = client.app.dependency_overrides[get_board_service]()
    board.create_epic(
        EpicDTO(project_id=project_id, title="Customer search epic", spec_id=spec_uuid)
    )

    resp = client.get(f"/projects/{project_id}/specs")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == str(project_id)
    assert body["constitution"] is not None
    assert body["constitution"]["project_id"] == str(project_id)
    assert len(body["specs"]) == 1
    spec = body["specs"][0]
    assert spec["id"] == manifest["id"]
    assert spec["name"] == "Customer search"
    assert spec["validation"] is None  # never validated yet


def test_populated_project_embeds_latest_validation_report(
    client: TestClient, db_factory: sessionmaker[Session]
) -> None:
    project_id = _make_project(db_factory)
    created = client.post(
        "/spec/specs",
        json={
            "epic_id": str(uuid.uuid4()),
            "name": "Billing",
            "requirements": [{"id": "R1", "text": "Charge a card"}],
        },
    )
    manifest = created.json()
    spec_uuid = spec_id_for_key(manifest["id"])

    assert client.post(f"/spec/specs/{spec_uuid}/clarify").status_code == 200
    assert client.post(f"/spec/specs/{spec_uuid}/plan").status_code == 200
    assert client.post(f"/spec/specs/{spec_uuid}/approve").status_code == 200
    tasks = client.post(f"/spec/specs/{spec_uuid}/tasks").json()
    task_id = tasks[0]["id"]
    assert client.post(f"/spec/tasks/{task_id}/validate").status_code == 200

    board: InMemoryBoardService = client.app.dependency_overrides[get_board_service]()
    board.create_epic(EpicDTO(project_id=project_id, title="Billing epic", spec_id=spec_uuid))

    resp = client.get(f"/projects/{project_id}/specs")

    assert resp.status_code == 200, resp.text
    spec = resp.json()["specs"][0]
    assert spec["validation"] is not None
    assert spec["validation"]["traceability"]
