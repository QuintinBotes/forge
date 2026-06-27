"""F20 API tests: MCP index control plane (PATCH / reindex / GET index).

Covers AC2 (auto-provision + enqueue on switch, idempotent), AC14 (switch-away
purge), AC16 (RBAC: admin to mutate, member to read), plus the reindex 409 guard.

Hermetic: in-memory SQLite (StaticPool), an in-memory MCP manager with the
fixture transport (no live traffic), and a patched ``enqueue_full_sync`` so no
Celery/Redis broker is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.mcp import get_mcp_manager, get_mcp_session_factory
from forge_api.services import mcp_index_service
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import (
    KnowledgeSource,
    MCPIndexedResource,
    RetrievalChunk,
    Workspace,
)
from forge_db.models.enums import KnowledgeSourceKind, SyncMode
from forge_mcp import MCPConnectionManager
from forge_mcp.testing import sample_connection, sample_transport

WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.add(Workspace(id=WORKSPACE_ID, name="Acme", slug="acme"))
        session.commit()
    return factory


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(mcp_index_service, "enqueue_full_sync", lambda sid: calls.append(str(sid)))
    return calls


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    authenticate_app: Callable[..., FastAPI],
    enqueued: list[str],
) -> FastAPI:
    application = create_app()
    authenticate_app(application)  # admin in WORKSPACE_ID
    manager = MCPConnectionManager(transport_factory=lambda conn: sample_transport())
    application.dependency_overrides[get_mcp_manager] = lambda: manager
    application.dependency_overrides[get_mcp_session_factory] = lambda: session_factory
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _principal(role: UserRole) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        role=role,
        email="x@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def _register(client: TestClient) -> str:
    conn = sample_connection(freshness_sla_minutes=15)
    resp = client.post("/mcp/connections", json=conn.model_dump(mode="json"))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_switch_to_index_provisions_source_and_enqueues(
    client: TestClient, session_factory: sessionmaker[Session], enqueued: list[str]
) -> None:
    slug = _register(client)
    resp = client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "sync_and_index"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["index_strategy"] == "sync_and_index"

    with session_factory() as session:
        sources = list(
            session.scalars(select(KnowledgeSource).where(KnowledgeSource.uri == f"mcp://{slug}"))
        )
    assert len(sources) == 1
    source = sources[0]
    assert source.kind is KnowledgeSourceKind.MCP
    assert source.sync_mode is SyncMode.SYNC_AND_INDEX
    assert source.config["mcp_connection_id"] == slug
    assert source.config["allowed_namespaces"] == ["engineering", "architecture"]
    assert source.freshness_sla_minutes == 15
    assert enqueued == [str(source.id)]

    # AC2: re-issuing the same PATCH is idempotent (no duplicate source).
    client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "sync_and_index"})
    with session_factory() as session:
        again = list(
            session.scalars(select(KnowledgeSource).where(KnowledgeSource.uri == f"mcp://{slug}"))
        )
    assert len(again) == 1


def test_switch_away_purges_index(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    slug = _register(client)
    client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "sync_and_index"})
    with session_factory() as session:
        source = session.scalar(
            select(KnowledgeSource).where(KnowledgeSource.uri == f"mcp://{slug}")
        )
        assert source is not None
        source_id = source.id
        session.add(
            RetrievalChunk(
                workspace_id=WORKSPACE_ID,
                knowledge_source_id=source_id,
                chunk_type="mcp_resource",
                weight=1.0,
                content="indexed",
                path=f"mcp://{slug}/confluence://engineering/p",
                content_hash="h1",
            )
        )
        session.add(
            MCPIndexedResource(
                workspace_id=WORKSPACE_ID,
                knowledge_source_id=source_id,
                connection_slug=slug,
                resource_uri="confluence://engineering/p",
                content_hash="h1",
                chunk_count=1,
            )
        )
        session.commit()

    resp = client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "query_through"})
    assert resp.status_code == 200

    with session_factory() as session:
        chunks = list(
            session.scalars(
                select(RetrievalChunk).where(RetrievalChunk.knowledge_source_id == source_id)
            )
        )
        ledger = list(
            session.scalars(
                select(MCPIndexedResource).where(
                    MCPIndexedResource.knowledge_source_id == source_id
                )
            )
        )
        source = session.get(KnowledgeSource, source_id)
    assert chunks == []
    assert ledger == []
    assert source is not None and source.config["index_status"] == "disabled"


def test_get_index_status_reflects_source(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    slug = _register(client)
    # Before switch: no source -> query_through default status.
    before = client.get(f"/mcp/connections/{slug}/index")
    assert before.status_code == 200
    assert before.json()["source_id"] is None

    client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "sync_and_index"})
    after = client.get(f"/mcp/connections/{slug}/index")
    assert after.status_code == 200
    body = after.json()
    assert body["index_strategy"] == "sync_and_index"
    assert body["source_id"] is not None
    assert body["status"] == "pending"
    assert body["freshness_sla_minutes"] == 15
    assert body["stale"] is True  # never synced yet


def test_reindex_409_when_not_indexed(client: TestClient) -> None:
    slug = _register(client)  # default sample connection is sync_and_index? -> set query_through
    client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "query_through"})
    resp = client.post(f"/mcp/connections/{slug}/index/reindex")
    assert resp.status_code == 409


def test_reindex_enqueues_when_indexed(client: TestClient, enqueued: list[str]) -> None:
    slug = _register(client)
    client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "sync_and_index"})
    enqueued.clear()
    resp = client.post(f"/mcp/connections/{slug}/index/reindex")
    assert resp.status_code == 202
    assert len(enqueued) == 1


def test_patch_requires_admin_and_get_requires_member(app: FastAPI, client: TestClient) -> None:
    slug = _register(client)  # registered as admin

    # A member may not flip index_strategy (admin-only) -> 403.
    app.dependency_overrides[get_current_principal] = lambda: _principal(UserRole.MEMBER)
    denied = client.patch(f"/mcp/connections/{slug}", json={"index_strategy": "sync_and_index"})
    assert denied.status_code == 403
    # ...but a member may read the index status (READ).
    allowed = client.get(f"/mcp/connections/{slug}/index")
    assert allowed.status_code == 200

    # A viewer (READ only) may not reindex (admin-only) -> 403.
    app.dependency_overrides[get_current_principal] = lambda: _principal(UserRole.VIEWER)
    assert client.post(f"/mcp/connections/{slug}/index/reindex").status_code == 403
