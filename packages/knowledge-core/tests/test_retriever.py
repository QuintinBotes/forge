"""Tests for ``forge_knowledge.retriever`` — the hybrid retriever (Task 1.3).

:class:`HybridRetriever` structurally satisfies the frozen
:class:`forge_contracts.protocols.Retriever` Protocol and wires the two indexed
legs (pgvector semantic + BM25 keyword) to RRF fusion and the reranker:

* ``semantic`` / ``keyword`` return ``Ranked`` lists (1-based ranks, carrying the
  ``RetrievedChunk`` for downstream attribution);
* ``fuse`` is RRF (delegates to ``forge_knowledge.fusion``);
* ``rerank`` runs the cross-encoder over candidate contents, sets ``rerank_score``
  and a weight-boosted final ``score``, and returns the top-n attributed chunks.

Hermetic: in-memory SQLite, deterministic embedding + fixture reranker.
"""

from __future__ import annotations

import uuid
from itertools import pairwise

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import Chunk, KnowledgeScope, Ranked, RetrievedChunk
from forge_contracts.enums import ChunkType
from forge_contracts.protocols import Retriever
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Workspace
from forge_db.session import create_session_factory
from forge_knowledge.embeddings import DeterministicEmbeddingClient
from forge_knowledge.reranker import FixtureRerankerClient
from forge_knowledge.retriever import HybridRetriever
from forge_knowledge.stores import Bm25Store, PgVectorStore


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


def _retriever(
    session_factory: sessionmaker[Session],
    *,
    reranker: FixtureRerankerClient | None = None,
) -> HybridRetriever:
    vector = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=512))
    keyword = Bm25Store(session_factory)
    return HybridRetriever(vector, keyword, reranker or FixtureRerankerClient())


def _index(
    session_factory: sessionmaker[Session], source_id: uuid.UUID, chunks: list[Chunk]
) -> None:
    PgVectorStore(
        session_factory, DeterministicEmbeddingClient(dimension=512)
    ).index(str(source_id), chunks)


CORPUS = [
    Chunk(content="database connection pooling and transactions in postgres", path="db.py"),
    Chunk(content="react component lifecycle hooks useState useEffect", path="ui.py"),
    Chunk(content="kubernetes pod scheduling autoscaling node affinity", path="k8s.py"),
    Chunk(content="oauth2 authentication jwt token refresh validation flow", path="auth.py"),
    Chunk(content="gradient descent backpropagation training neural networks", path="ml.py"),
]


def test_hybrid_retriever_satisfies_protocol(
    session_factory: sessionmaker[Session],
) -> None:
    assert isinstance(_retriever(session_factory), Retriever)


def test_semantic_returns_ranked_with_chunks(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    _index(session_factory, source_id, CORPUS)
    retriever = _retriever(session_factory)
    scope = KnowledgeScope(workspace_id=workspace_id)

    ranked = retriever.semantic("validate a jwt oauth token", scope, k=5)

    assert ranked
    assert all(isinstance(r, Ranked) for r in ranked)
    # Ranks are 1-based and contiguous.
    assert [r.rank for r in ranked] == list(range(1, len(ranked) + 1))
    # Each Ranked carries its chunk for attribution.
    assert all(r.chunk is not None for r in ranked)
    assert ranked[0].chunk is not None and ranked[0].chunk.path == "auth.py"


def test_keyword_recovers_exact_identifier(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    _index(session_factory, source_id, CORPUS)
    retriever = _retriever(session_factory)
    scope = KnowledgeScope(workspace_id=workspace_id)

    ranked = retriever.keyword("useEffect", scope, k=5)

    assert ranked
    assert ranked[0].chunk is not None and ranked[0].chunk.path == "ui.py"


def test_fuse_delegates_to_rrf(session_factory: sessionmaker[Session]) -> None:
    retriever = _retriever(session_factory)
    a = [Ranked(chunk_id="x", score=1.0, rank=1, chunk=RetrievedChunk(id="x", content="x"))]
    b = [Ranked(chunk_id="x", score=1.0, rank=1, chunk=RetrievedChunk(id="x", content="x"))]
    fused = retriever.fuse([a, b])
    assert fused[0].chunk_id == "x"
    # Appears in both rankings at rank 1 -> 2 * 1/61.
    assert abs(fused[0].score - 2 / 61) < 1e-12


def test_rerank_returns_attributed_chunks_with_scores(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    _index(session_factory, source_id, CORPUS)
    retriever = _retriever(session_factory)
    scope = KnowledgeScope(workspace_id=workspace_id)

    candidates = retriever.semantic("oauth jwt token validation", scope, k=5)
    reranked = retriever.rerank("oauth jwt token validation", candidates, top_n=3)

    assert len(reranked) == 3
    assert all(isinstance(c, RetrievedChunk) for c in reranked)
    assert reranked[0].path == "auth.py"
    # Reranker populated the rerank score and the final score is sorted desc.
    assert all(c.rerank_score is not None for c in reranked)
    assert all(a.score >= b.score for a, b in pairwise(reranked))
    # Source attribution survives reranking.
    assert reranked[0].source_id == str(source_id)


def test_rerank_applies_chunk_weight_boost(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    # Two distinct chunks the fixture scores *equally* relevant, but with
    # different priority weights: the higher-weight (policy 1.5x) chunk must win
    # on the weight boost alone.
    note = "deployment runbook alpha notes"
    policy = "deployment runbook beta policy"
    _index(
        session_factory,
        source_id,
        [
            Chunk(content=note, path="notes.md",
                  chunk_type=ChunkType.MARKDOWN, weight=1.0),
            Chunk(content=policy, path="AGENTS.md",
                  chunk_type=ChunkType.POLICY, weight=1.5),
        ],
    )
    reranker = FixtureRerankerClient({"deployment runbook": {note: 0.5, policy: 0.5}})
    retriever = _retriever(session_factory, reranker=reranker)
    scope = KnowledgeScope(workspace_id=workspace_id)

    candidates = retriever.semantic("deployment runbook", scope, k=5)
    reranked = retriever.rerank("deployment runbook", candidates, top_n=2)

    assert reranked[0].path == "AGENTS.md"
    assert reranked[0].score >= reranked[1].score
    # Equal raw relevance, weight breaks the tie.
    assert reranked[0].rerank_score == reranked[1].rerank_score


def test_rerank_empty_candidates_returns_empty(
    session_factory: sessionmaker[Session],
) -> None:
    assert _retriever(session_factory).rerank("q", [], top_n=5) == []
