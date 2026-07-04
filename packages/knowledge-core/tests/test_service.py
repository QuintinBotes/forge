"""End-to-end tests for ``forge_knowledge.service.KnowledgeService`` (Task 1.3).

This is the proof of the RAG spine: index a tiny repository, then ``search``
chains semantic (pgvector) + keyword (BM25) -> RRF fusion (k=60) -> reranking ->
attributed top-k. Assertions:

* the service structurally satisfies the frozen ``KnowledgeStore`` Protocol;
* an end-to-end search on an indexed repo returns attributed, reranked chunks;
* the hybrid pipeline recovers an exact-identifier match that pure semantic
  search dilutes (the documented reason retrieval is always hybrid);
* the reranker reorders candidates per its fixture.

Hermetic: in-memory SQLite, deterministic embedding, fixture reranker. No
network, no live services.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import Chunk, KnowledgeScope, RetrievedChunk
from forge_contracts.protocols import KnowledgeStore
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_knowledge.embeddings import DeterministicEmbeddingClient
from forge_knowledge.reranker import FixtureRerankerClient
from forge_knowledge.service import KnowledgeService


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def workspace_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        workspace = Workspace(name="Acme", slug="acme")
        session.add(workspace)
        session.flush()
        ws_id = workspace.id
        session.commit()
    return ws_id


def _make_source(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> uuid.UUID:
    with session_factory() as session:
        source = KnowledgeSource(
            workspace_id=workspace_id, kind="repo", name="api", uri="github.com/org/api"
        )
        session.add(source)
        session.flush()
        src_id = source.id
        session.commit()
    return src_id


def _service(
    session_factory: sessionmaker[Session],
    *,
    reranker: FixtureRerankerClient | None = None,
) -> KnowledgeService:
    return KnowledgeService.from_session_factory(
        session_factory,
        DeterministicEmbeddingClient(dimension=512),
        reranker or FixtureRerankerClient(),
    )


REPO = [
    Chunk(content="def connect_postgres(): open a pooled database connection", path="db.py"),
    Chunk(content="def render_component(): react hooks useState useEffect lifecycle", path="ui.py"),
    Chunk(content="def schedule_pod(): kubernetes autoscaling and node affinity", path="k8s.py"),
    Chunk(content="def validate_jwt(token): verify oauth2 signature and expiry", path="auth.py"),
    Chunk(content="def train_model(): gradient descent and backpropagation", path="ml.py"),
    Chunk(content="def compute_rrf_score(rankings): reciprocal rank fusion", path="rank.py"),
]


def test_service_satisfies_knowledge_store_protocol(
    session_factory: sessionmaker[Session],
) -> None:
    assert isinstance(_service(session_factory), KnowledgeStore)


def test_end_to_end_search_returns_attributed_reranked_chunks(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    service = _service(session_factory)
    service.index(str(source_id), REPO)
    scope = KnowledgeScope(workspace_id=workspace_id)

    results = service.search("validate an oauth jwt token", scope, k=3)

    assert results
    assert len(results) <= 3
    assert all(isinstance(c, RetrievedChunk) for c in results)
    # Best match is the auth chunk, fully attributed.
    top = results[0]
    assert top.path == "auth.py"
    assert top.source_id == str(source_id)
    assert top.source_uri == "github.com/org/api"
    assert top.rerank_score is not None


def test_hybrid_recovers_exact_identifier_match(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    # An exact rare identifier query: the keyword (BM25) leg must surface
    # ``compute_rrf_score`` into the final results via RRF, proving hybrid wins.
    source_id = _make_source(session_factory, workspace_id)
    service = _service(session_factory)
    service.index(str(source_id), REPO)
    scope = KnowledgeScope(workspace_id=workspace_id)

    results = service.search("compute_rrf_score", scope, k=3)

    assert any(c.path == "rank.py" for c in results)


def test_reranker_reorders_per_fixture(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    # Fixture forces ml.py to outrank the lexically-closer auth.py for this query.
    fixture = {
        "token": {
            "def train_model(): gradient descent and backpropagation": 0.99,
            "def validate_jwt(token): verify oauth2 signature and expiry": 0.10,
        }
    }
    service = _service(session_factory, reranker=FixtureRerankerClient(fixture))
    service.index(str(source_id), REPO)
    scope = KnowledgeScope(workspace_id=workspace_id)

    results = service.search("token", scope, k=2)

    assert results[0].path == "ml.py"


def test_search_empty_index_returns_empty(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    _make_source(session_factory, workspace_id)
    service = _service(session_factory)
    scope = KnowledgeScope(workspace_id=workspace_id)
    assert service.search("anything", scope, k=5) == []


def test_index_delegates_and_is_idempotent(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    service = _service(session_factory)

    first = service.index(str(source_id), REPO)
    second = service.index(str(source_id), REPO)

    assert first.indexed == len(REPO)
    assert second.indexed == 0
    assert second.skipped == len(REPO)
