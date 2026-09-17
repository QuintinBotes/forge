"""``/spec/specs/{spec_id}`` identifier contract (F5) and client keys (F6).

``POST /spec/specs`` returns the manifest, whose ``id`` is the human spec key
(``SPEC-1``). Feeding that straight back used to 422: the read endpoint took
only ``uuid5(SPEC_NAMESPACE, key)``, so a client could not use the identifier
the create call had just handed it without reimplementing the derivation. These
tests pin that both forms address the same spec, and that a caller may bring
their own key.
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

#: Mirrors ``conftest.py``'s deterministic test workspace.
_TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def client(tmp_path: Path, authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    engine = FileSpecEngine(root=tmp_path / "specs")

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


def _create(client: TestClient, **extra: object) -> dict:
    body: dict[str, object] = {
        "epic_id": str(uuid.uuid4()),
        "name": "Customer search",
        "requirements": [{"id": "R1", "text": "Search customers by name"}],
    }
    body.update(extra)
    resp = client.post("/spec/specs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- F5: the id the create call returns is usable ------------------------- #


def test_the_id_create_returns_is_accepted_by_the_read(client: TestClient) -> None:
    """The regression this exists for: round-trip the identifier verbatim."""
    manifest = _create(client)
    assert manifest["id"] == "SPEC-1"

    fetched = client.get(f"/spec/specs/{manifest['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["name"] == "Customer search"
    assert len(fetched.json()["requirements"]) == 1


def test_key_and_uuid_address_the_same_spec(client: TestClient) -> None:
    manifest = _create(client)
    by_key = client.get(f"/spec/specs/{manifest['id']}")
    by_uuid = client.get(f"/spec/specs/{spec_id_for_key(manifest['id'])}")

    assert by_key.status_code == by_uuid.status_code == 200
    assert by_key.json() == by_uuid.json()


@pytest.mark.parametrize(
    "suffix",
    ["", "/markdown", "/manifest"],
)
def test_every_read_route_accepts_the_key(client: TestClient, suffix: str) -> None:
    manifest = _create(client)
    resp = client.get(f"/spec/specs/{manifest['id']}{suffix}")
    assert resp.status_code == 200, resp.text


def test_lifecycle_actions_accept_the_key(client: TestClient) -> None:
    manifest = _create(client)
    key = manifest["id"]

    assert client.post(f"/spec/specs/{key}/plan").status_code == 200
    assert client.post(f"/spec/specs/{key}/approve").status_code == 200
    assert client.post(f"/spec/specs/{key}/tasks").status_code == 200


def test_a_wellformed_key_for_a_missing_spec_is_404_not_422(client: TestClient) -> None:
    """ "No such spec" and "unusable identifier" must stay distinct answers."""
    assert client.get("/spec/specs/SPEC-999").status_code == 404


def test_a_malformed_identifier_is_422(client: TestClient) -> None:
    resp = client.get("/spec/specs/not-an-identifier")
    assert resp.status_code == 422
    assert "spec key" in resp.text


# --- F6: bring your own key ------------------------------------------------ #


def test_create_accepts_a_client_supplied_key(client: TestClient) -> None:
    manifest = _create(client, key="MOD-893")
    assert manifest["id"] == "MOD-893"

    fetched = client.get("/spec/specs/MOD-893")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Customer search"


def test_an_imported_key_does_not_shift_forge_numbering(client: TestClient) -> None:
    _create(client, key="MOD-893")
    assert _create(client, name="Forge-numbered")["id"] == "SPEC-1"


def test_a_duplicate_key_is_409(client: TestClient) -> None:
    _create(client, key="MOD-893")
    resp = client.post(
        "/spec/specs",
        json={"epic_id": str(uuid.uuid4()), "name": "Second", "key": "MOD-893"},
    )
    assert resp.status_code == 409, resp.text
    assert "already in use" in resp.text


def test_a_malformed_key_is_rejected_by_validation(client: TestClient) -> None:
    resp = client.post(
        "/spec/specs",
        json={"epic_id": str(uuid.uuid4()), "name": "Bad", "key": "mod 893"},
    )
    assert resp.status_code == 422, resp.text

# --- malformed documents answer, rather than 500 (issue #103) -------------- #


def test_a_manifest_that_fails_validation_is_422_with_the_errors(
    client: TestClient,
) -> None:
    """A wrong field type used to be a bare 500 with the reason only in the log."""
    _create(client)
    # `constraints` is list[str]; send a list of objects.
    bad = (
        "id: SPEC-1\n"
        "name: Customer search\n"
        "constraints:\n"
        "  - id: C1\n"
        "    text: ships off by default\n"
    )
    resp = client.put("/spec/specs/SPEC-1/manifest", json={"content": bad})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    # Pydantic's own error list — the same shape FastAPI returns for a bad body.
    assert isinstance(detail, list) and detail
    assert any("constraints" in str(item.get("loc", "")) for item in detail)


def test_malformed_yaml_is_422_not_500(client: TestClient) -> None:
    _create(client)
    resp = client.put(
        "/spec/specs/SPEC-1/manifest",
        json={"content": "id: SPEC-1\nname: [unclosed\n"},
    )

    assert resp.status_code == 422, resp.text
    assert "YAML" in resp.text or "yaml" in resp.text


def test_unparseable_markdown_is_422_not_500(client: TestClient) -> None:
    """The markdown endpoint takes a whole document too, and had the same hole."""
    _create(client)
    resp = client.put(
        "/spec/specs/SPEC-1/markdown",
        json={"content": "no frontmatter, no headings, not a spec document"},
    )

    assert resp.status_code == 422, resp.text


def test_a_valid_manifest_still_saves(client: TestClient) -> None:
    """The guard must not swallow good input."""
    _create(client)
    good = (
        "id: SPEC-1\n"
        "name: Renamed via manifest\n"
        "status: draft\n"
        "constraints:\n"
        "  - ships off by default\n"
    )
    resp = client.put("/spec/specs/SPEC-1/manifest", json={"content": good})

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed via manifest"
    assert resp.json()["constraints"] == ["ships off by default"]
