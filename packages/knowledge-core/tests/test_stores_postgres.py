"""Postgres-backed integration tests for the stores (plan Task 1.2, RAG spine).

These exercise the *real* Postgres code paths the SQLite unit tests can only
approximate: pgvector ``cosine_distance`` ordering and ``to_tsvector``/``ts_rank``
keyword ranking. They use the shared ``pg_engine`` fixture (root ``conftest.py``),
which yields a pgvector-enabled Postgres from ``FORGE_TEST_DATABASE_URL`` or a
``testcontainers`` container, and otherwise **skips** with a parked reason — never
faked. This is the Phase-2 / CI verification of the live retrieval path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import Chunk, KnowledgeScope
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_knowledge.embeddings import DeterministicEmbeddingClient
from forge_knowledge.stores import Bm25Store, PgVectorStore

# pg_engine is a session-scoped Engine; importing the fixture name is enough.
pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def pg_session_factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        workspace = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(workspace)
        session.flush()
        source = KnowledgeSource(
            workspace_id=workspace.id, kind="repo", name="api", uri="github.com/org/api"
        )
        session.add(source)
        session.flush()
        ids = (workspace.id, source.id)
        session.commit()
    return ids


def test_pgvector_cosine_search_on_postgres(
    pg_session_factory: sessionmaker[Session],
) -> None:
    workspace_id, source_id = _seed(pg_session_factory)
    store = PgVectorStore(pg_session_factory, DeterministicEmbeddingClient())
    store.index(
        source_id=str(source_id),
        chunks=[
            Chunk(content="oauth2 jwt token refresh and validation", path="auth.py"),
            Chunk(content="kubernetes pod autoscaling node affinity", path="k8s.py"),
            Chunk(content="postgres connection pooling transactions", path="db.py"),
        ],
    )
    hits = store.search(
        "validate a jwt token for oauth", KnowledgeScope(workspace_id=workspace_id), k=2
    )
    assert hits
    assert hits[0].path == "auth.py"


def test_bm25_ts_rank_on_postgres(
    pg_session_factory: sessionmaker[Session],
) -> None:
    workspace_id, source_id = _seed(pg_session_factory)
    PgVectorStore(pg_session_factory, DeterministicEmbeddingClient()).index(
        source_id=str(source_id),
        chunks=[
            Chunk(content="retry retry timeout backoff helper", path="retry.py"),
            Chunk(content="single retry mention otherwise unrelated", path="misc.py"),
            Chunk(content="completely different subject", path="other.py"),
        ],
    )
    hits = Bm25Store(pg_session_factory).search(
        "retry", KnowledgeScope(workspace_id=workspace_id), k=5
    )
    assert hits
    assert hits[0].path == "retry.py"
