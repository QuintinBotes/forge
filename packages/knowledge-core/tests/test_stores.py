"""Tests for ``forge_knowledge.stores`` (plan Task 1.2, RAG spine).

Covers the two indexed stores that sit on top of the ``forge_db`` data model:

* :class:`PgVectorStore` — ``index``/``search`` with cosine similarity. On
  Postgres it uses pgvector's ``cosine_distance``; on SQLite (the hermetic
  unit-test backend) it computes cosine in Python over the JSON-degraded
  ``embedding`` column. Same code path, dialect-aware.
* :class:`Bm25Store` — keyword ``search`` (Postgres ``to_tsvector``/``ts_rank``;
  Okapi-BM25 in Python on SQLite).

The headline assertions:
- indexing 5 chunks writes 5 ``RetrievalChunk`` rows and is idempotent
  (re-index → all skipped);
- vector search returns the nearest chunk by deterministic fake embedding;
- BM25 returns an **exact-identifier** match that the vector search *misses*,
  proving the hybrid pipeline's keyword leg earns its keep.

Hermetic: in-memory SQLite, no network, no live services. The Postgres path is
exercised separately in ``test_stores_postgres.py`` (skips without Postgres).
"""

from __future__ import annotations

import uuid
from itertools import pairwise

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import Chunk, KnowledgeScope
from forge_contracts.enums import ChunkType
from forge_contracts.protocols import KnowledgeStore
from forge_db.base import Base
from forge_db.models import KnowledgeSource, RetrievalChunk, Workspace
from forge_db.session import create_session_factory
from forge_knowledge.embeddings import DeterministicEmbeddingClient
from forge_knowledge.stores import (
    Bm25Store,
    KnowledgeSourceNotFoundError,
    PgVectorStore,
)


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
    session_factory: sessionmaker[Session],
    workspace_id: uuid.UUID,
    *,
    uri: str = "github.com/org/api",
) -> uuid.UUID:
    with session_factory() as session:
        source = KnowledgeSource(
            workspace_id=workspace_id, kind="repo", name="api", uri=uri
        )
        session.add(source)
        session.flush()
        src_id = source.id
        session.commit()
    return src_id


def _chunk(content: str, path: str, chunk_type: ChunkType = ChunkType.CODE) -> Chunk:
    return Chunk(content=content, path=path, chunk_type=chunk_type)


# --------------------------------------------------------------------------- #
# PgVectorStore — indexing                                                     #
# --------------------------------------------------------------------------- #


def test_pgvector_store_satisfies_knowledge_store_protocol(
    session_factory: sessionmaker[Session],
) -> None:
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    assert isinstance(store, KnowledgeStore)


def test_index_writes_one_row_per_chunk(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    chunks = [_chunk(f"def fn_{i}(): return {i}", f"mod_{i}.py") for i in range(5)]

    result = store.index(source_id=str(source_id), chunks=chunks)

    assert result.indexed == 5
    assert result.skipped == 0
    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(RetrievalChunk)
            .where(RetrievalChunk.knowledge_source_id == source_id)
        )
    assert count == 5


def test_index_is_idempotent_by_content_hash(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    chunks = [_chunk(f"def fn_{i}(): return {i}", f"mod_{i}.py") for i in range(5)]

    store.index(source_id=str(source_id), chunks=chunks)
    second = store.index(source_id=str(source_id), chunks=chunks)

    assert second.indexed == 0
    assert second.skipped == 5
    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(RetrievalChunk)
            .where(RetrievalChunk.knowledge_source_id == source_id)
        )
    assert count == 5


def test_index_persists_embeddings_and_attribution(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=64))
    store.index(source_id=str(source_id), chunks=[_chunk("def x(): pass", "x.py")])

    with session_factory() as session:
        row = session.scalars(select(RetrievalChunk)).one()
    assert row.embedding is not None
    assert len(row.embedding) == 64
    assert row.path == "x.py"
    assert row.content_hash


def test_index_unknown_source_raises(
    session_factory: sessionmaker[Session],
) -> None:
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=64))
    with pytest.raises(KnowledgeSourceNotFoundError):
        store.index(source_id=str(uuid.uuid4()), chunks=[_chunk("x", "x.py")])


# --------------------------------------------------------------------------- #
# PgVectorStore — search                                                       #
# --------------------------------------------------------------------------- #


def test_vector_search_returns_nearest_by_embedding(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=512))
    store.index(
        source_id=str(source_id),
        chunks=[
            _chunk("database connection pooling and transactions in postgres", "db.py"),
            _chunk("react component lifecycle hooks useState useEffect render", "ui.py"),
            _chunk("kubernetes pod scheduling autoscaling and node affinity", "k8s.py"),
            _chunk("oauth2 authentication jwt token refresh and validation flow", "auth.py"),
            _chunk("gradient descent backpropagation training neural networks", "ml.py"),
        ],
    )
    scope = KnowledgeScope(workspace_id=workspace_id)

    results = store.search("validate a jwt token during oauth authentication", scope, k=3)

    assert results
    assert results[0].path == "auth.py"
    assert results[0].source_id == str(source_id)
    # Scores are sorted descending.
    assert all(a.score >= b.score for a, b in pairwise(results))


def test_vector_search_respects_k(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    store.index(
        source_id=str(source_id),
        chunks=[_chunk(f"topic number {i} content here", f"f_{i}.py") for i in range(6)],
    )
    results = store.search("topic content", KnowledgeScope(workspace_id=workspace_id), k=2)
    assert len(results) == 2


def test_vector_search_scopes_by_workspace(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    store.index(source_id=str(source_id), chunks=[_chunk("alpha beta gamma", "a.py")])

    other_ws = uuid.uuid4()
    results = store.search("alpha", KnowledgeScope(workspace_id=other_ws), k=5)
    assert results == []


# --------------------------------------------------------------------------- #
# Bm25Store + the hybrid value proposition (keyword beats vector)             #
# --------------------------------------------------------------------------- #


def _index_identifier_corpus(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> tuple[uuid.UUID, PgVectorStore]:
    """Build a corpus where the query's common words are high-document-frequency
    (every chunk carries the "please configure the settings" boilerplate, so
    BM25 gives them ~zero IDF) while the rare identifier ``q9zse_handler`` lives
    in exactly one, long, otherwise-diluted chunk.

    Consequence: the semantic leg is dominated by the short, common-word-dense
    distractor and ranks the target below it (a vector *miss*); the keyword leg's
    IDF makes the rare identifier dominate and recovers the target at rank 1.
    """
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=512))
    boiler = "please configure the settings"
    fillers = [
        _chunk(f"{boiler} for module {name} handling input output", f"{name}.py")
        for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
    ]
    target = _chunk(
        f"{boiler} then initialize the q9zse_handler by loading config "
        "validating schema connecting database opening sockets spawning workers "
        "registering routes mounting middleware starting scheduler",
        "handlers.py",
    )
    distractor = _chunk(f"{boiler} {boiler}", "boiler.md", ChunkType.MARKDOWN)
    store.index(source_id=str(source_id), chunks=[target, distractor, *fillers])
    return source_id, store


def test_keyword_search_finds_exact_identifier_vector_misses(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id, vector_store = _index_identifier_corpus(session_factory, workspace_id)
    keyword_store = Bm25Store(session_factory)
    scope = KnowledgeScope(workspace_id=workspace_id)
    query = "q9zse_handler please configure the settings"

    vector_hits = vector_store.search(query, scope, k=8)
    keyword_hits = keyword_store.search(query, scope, k=8)

    # The vector leg is dominated by common-word overlap and misses the target.
    assert vector_hits[0].path != "handlers.py"
    # The keyword leg's IDF recovers the exact-identifier chunk at rank 1.
    assert keyword_hits[0].path == "handlers.py"
    assert "q9zse_handler" in keyword_hits[0].content
    assert keyword_hits[0].source_id == str(source_id)


def test_bm25_search_ranks_by_relevance(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128)).index(
        source_id=str(source_id),
        chunks=[
            _chunk("retry retry retry timeout backoff helper", "retry.py"),
            _chunk("a single retry mention among other words here", "misc.py"),
            _chunk("completely different subject matter entirely", "other.py"),
        ],
    )
    hits = Bm25Store(session_factory).search(
        "retry", KnowledgeScope(workspace_id=workspace_id), k=5
    )
    assert hits[0].path == "retry.py"
    assert all(h.score >= n.score for h, n in pairwise(hits))


def test_bm25_search_no_match_returns_empty(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128)).index(
        source_id=str(source_id),
        chunks=[_chunk("alpha beta gamma", "a.py")],
    )
    hits = Bm25Store(session_factory).search(
        "zzzznotpresent", KnowledgeScope(workspace_id=workspace_id), k=5
    )
    assert hits == []
