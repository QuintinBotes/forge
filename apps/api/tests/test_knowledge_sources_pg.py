"""``GET /knowledge/sources`` on real Postgres: one grouped query over pgvector rows."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.main import create_app
from forge_api.routers.knowledge import get_knowledge_session_factory
from forge_db.base import Base
from forge_db.models import KnowledgeSource, RetrievalChunk, Workspace
from forge_db.models.enums import ChunkType
from forge_db.models.knowledge import EMBEDDING_DIM

pytestmark = pytest.mark.usefixtures("pg_engine")

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")  # the test principal's workspace


@pytest.fixture
def factory(pg_engine: Engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    Base.metadata.drop_all(pg_engine)


def _source(session: Session, workspace: uuid.UUID, name: str, embedded: list[bool]) -> uuid.UUID:
    source = KnowledgeSource(
        workspace_id=workspace, kind="repo", name=name, uri=f"github.com/{name}"
    )
    session.add(source)
    session.flush()
    for i, has_vector in enumerate(embedded):
        session.add(
            RetrievalChunk(
                workspace_id=workspace,
                knowledge_source_id=source.id,
                chunk_type=ChunkType.MARKDOWN,
                content=f"{name} {i}",
                path=f"{name}/{i}.md",
                content_hash=f"{name}-{i}",
                embedding=[0.1] * EMBEDDING_DIM if has_vector else None,
            )
        )
    return source.id


def test_sources_counts_and_coverage_on_postgres(
    factory: sessionmaker[Session], authenticate_app: Callable[..., FastAPI]
) -> None:
    other = uuid.uuid4()
    with factory() as session:
        session.add_all(
            [Workspace(id=WS, name="Mine", slug="mine"), Workspace(id=other, name="O", slug="o")]
        )
        session.flush()
        mine = _source(session, WS, "b-repo", [True, False, True])
        empty = _source(session, WS, "a-empty", [])
        _source(session, other, "c-foreign", [True])
        session.commit()

    app = create_app()
    authenticate_app(app)
    app.dependency_overrides[get_knowledge_session_factory] = lambda: factory
    with TestClient(app) as client:
        resp = client.get("/knowledge/sources")

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == [str(empty), str(mine)]
    assert [(r["chunk_count"], r["embedded_chunk_count"]) for r in rows] == [(0, 0), (3, 2)]
    assert rows[0]["newest_chunk_at"] is None and rows[1]["newest_chunk_at"] is not None
