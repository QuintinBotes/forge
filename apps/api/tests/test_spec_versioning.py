"""Integration tests for spec versioning + diff (ss-versioning).

Every save through the editing endpoints (``spec_create`` / ``write_manifest``
/ ``write_spec_markdown`` / ``write_spec_manifest_yaml``) appends an immutable
``spec_version`` row; ``GET .../versions`` lists them, ``GET
.../versions/{n}`` reads one snapshot, and ``GET
.../versions/{a}/diff/{b}`` diffs two of them (line-level markdown +
structured manifest). Hermetic: SQLite in-memory backs the DB, a tmp-rooted
``FileSpecEngine`` backs the spec content.
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
from forge_api.deps import get_current_principal
from forge_api.main import create_app
from forge_api.routers.spec import get_spec_engine
from forge_db.base import Base
from forge_db.models import Workspace
from forge_spec import FileSpecEngine, spec_id_for_key

# Deterministic identities mirroring ``conftest.py``'s (tests mirror rather
# than cross-import conftest constants, per repo convention).
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture
def db_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.add(Workspace(id=TEST_WORKSPACE_ID, name="Acme", slug="acme"))
        session.commit()
    yield factory


@pytest.fixture
def client(
    tmp_path: Path,
    authenticate_app: Callable[..., FastAPI],
    db_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    engine = FileSpecEngine(root=tmp_path / "specs")

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


def _create_spec(client: TestClient, name: str = "Customer search") -> dict:
    resp = client.post(
        "/spec/specs",
        json={
            "epic_id": str(uuid.uuid4()),
            "name": name,
            "requirements": [{"id": "R1", "text": "Search customers by name"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_spec_create_records_version_one(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    resp = client.get(f"/spec/specs/{spec_uuid}/versions")

    assert resp.status_code == 200, resp.text
    versions = resp.json()
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["name"] == "Customer search"
    assert versions[0]["created_by"] == str(TEST_USER_ID)


def test_saving_manifest_appends_a_new_version(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    manifest["name"] = "Customer search v2"
    resp = client.put(f"/spec/specs/{spec_uuid}", json=manifest)
    assert resp.status_code == 200, resp.text

    versions = client.get(f"/spec/specs/{spec_uuid}/versions").json()
    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[0]["name"] == "Customer search v2"


def test_saving_markdown_appends_a_new_version(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    markdown = client.get(f"/spec/specs/{spec_uuid}/markdown").text
    updated_markdown = markdown.replace(
        "Search customers by name", "Search customers by name or email"
    )
    resp = client.put(
        f"/spec/specs/{spec_uuid}/markdown",
        json={"content": updated_markdown},
    )
    assert resp.status_code == 200, resp.text

    versions = client.get(f"/spec/specs/{spec_uuid}/versions").json()
    assert len(versions) == 2


def test_read_one_version_returns_full_snapshot(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    resp = client.get(f"/spec/specs/{spec_uuid}/versions/1")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_number"] == 1
    assert body["manifest"]["name"] == "Customer search"
    assert "Customer search" in body["spec_md"]
    assert "Customer search" in body["manifest_yaml"]


def test_read_missing_version_is_404(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    resp = client.get(f"/spec/specs/{spec_uuid}/versions/99")

    assert resp.status_code == 404


def test_diff_two_versions_reports_markdown_and_manifest_changes(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    manifest["name"] = "Customer search v2"
    manifest["requirements"].append({"id": "R2", "text": "Filter by status"})
    client.put(f"/spec/specs/{spec_uuid}", json=manifest)

    resp = client.get(f"/spec/specs/{spec_uuid}/versions/1/diff/2")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from_version"] == 1
    assert body["to_version"] == 2
    assert any(line["op"] == "insert" for line in body["markdown"])
    assert any(line["op"] == "delete" for line in body["markdown"])
    scalar_fields = {c["field"] for c in body["manifest"]["scalar_changes"]}
    assert "name" in scalar_fields
    added_requirement_ids = {
        c["id"] for c in body["manifest"]["requirements"] if c["change"] == "added"
    }
    assert "R2" in added_requirement_ids


def test_diff_missing_version_is_404(client: TestClient) -> None:
    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    resp = client.get(f"/spec/specs/{spec_uuid}/versions/1/diff/2")

    assert resp.status_code == 404


def test_versions_are_workspace_scoped(
    client: TestClient, db_factory: sessionmaker[Session]
) -> None:
    other_workspace = uuid.uuid4()
    with db_factory() as session:
        session.add(Workspace(id=other_workspace, name="Other", slug="other"))
        session.commit()

    manifest = _create_spec(client)
    spec_uuid = spec_id_for_key(manifest["id"])

    # A different workspace never sees this spec's versions (empty, not 404 —
    # mirrors the F23 dashboard's "no linked specs" convention).
    from forge_api.deps import Principal
    from forge_contracts import UserRole

    other_principal = Principal(
        user_id=uuid.uuid4(),
        workspace_id=other_workspace,
        role=UserRole.ADMIN,
        auth_method="test",
        scopes=["*"],
    )
    client.app.dependency_overrides[get_current_principal] = lambda: other_principal
    resp = client.get(f"/spec/specs/{spec_uuid}/versions")
    assert resp.status_code == 200
    assert resp.json() == []
