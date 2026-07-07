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

import time
import uuid
from itertools import pairwise

import httpx
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
from forge_knowledge.reranker import (
    FixtureRerankerClient,
    GracefulReranker,
    JinaRerankerClient,
)
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


def _make_source(session_factory: sessionmaker[Session], workspace_id: uuid.UUID) -> uuid.UUID:
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
    PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=512)).index(
        str(source_id), chunks
    )


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
            Chunk(content=note, path="notes.md", chunk_type=ChunkType.MARKDOWN, weight=1.0),
            Chunk(content=policy, path="AGENTS.md", chunk_type=ChunkType.POLICY, weight=1.5),
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


# --------------------------------------------------------------------------- #
# HARD-03: graceful fallback, latency budget, disabled path, rank delta        #
# --------------------------------------------------------------------------- #


def _cand(cid: str, content: str, score: float, weight: float = 1.0) -> Ranked:
    return Ranked(
        chunk_id=cid,
        score=score,
        rank=0,
        chunk=RetrievedChunk(id=cid, content=content, weight=weight),
    )


# Fused-order candidates whose weighted-RRF order (score x weight) is c1, c0, c2.
def _weighted_candidates() -> list[Ranked]:
    return [
        _cand("c0", "alpha", score=0.5, weight=1.0),  # weighted 0.50
        _cand("c1", "bravo", score=0.4, weight=1.5),  # weighted 0.60
        _cand("c2", "charlie", score=0.3, weight=1.0),  # weighted 0.30
    ]


def _bare_retriever(reranker, *, rerank_enabled: bool = True) -> HybridRetriever:
    # rerank() only touches the reranker + candidates, not the stores.
    return HybridRetriever(None, None, reranker, rerank_enabled=rerank_enabled)  # type: ignore[arg-type]


def _mock_503_reranker() -> GracefulReranker:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    inner = JinaRerankerClient(
        "jina-reranker-v2",
        provider="jina",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return GracefulReranker(inner, timeout_ms=800)


def test_rerank_falls_back_to_weighted_rrf_on_503() -> None:
    # AC3: a degraded reranker -> weighted-RRF order, rerank_score None, fallback.
    retriever = _bare_retriever(_mock_503_reranker())
    out = retriever.rerank("q", _weighted_candidates(), top_n=3)

    assert [c.id for c in out] == ["c1", "c0", "c2"]
    assert all(c.rerank_score is None for c in out)
    assert out[0].score == pytest.approx(0.6)  # 0.4 * 1.5
    assert retriever.last_rerank is not None
    assert retriever.last_rerank.fallback_used is True
    assert retriever.last_rerank.provider == "jina"


def test_rerank_falls_back_on_latency_budget() -> None:
    # AC4: an inner reranker that sleeps past the budget degrades within bounds.
    def slow(_: httpx.Request) -> httpx.Response:
        time.sleep(2.0)
        return httpx.Response(200, json={"model": "m", "results": []})

    inner = JinaRerankerClient(
        "jina-reranker-v2", client=httpx.Client(transport=httpx.MockTransport(slow))
    )
    retriever = _bare_retriever(GracefulReranker(inner, timeout_ms=50))

    start = time.perf_counter()
    out = retriever.rerank("q", _weighted_candidates(), top_n=3)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert [c.id for c in out] == ["c1", "c0", "c2"]
    assert all(c.rerank_score is None for c in out)
    assert elapsed_ms < 800
    assert retriever.last_rerank is not None
    assert retriever.last_rerank.fallback_used is True


def test_rerank_disabled_equals_weighted_rrf_and_never_calls_reranker() -> None:
    # AC5: rerank_enabled=False -> weighted-RRF order; the reranker is untouched.
    class _Boom:
        provider = "boom"
        model = None

        def rerank(self, *_a: object, **_k: object) -> list:
            raise AssertionError("reranker must not be called when disabled")

    retriever = _bare_retriever(_Boom(), rerank_enabled=False)
    out = retriever.rerank("q", _weighted_candidates(), top_n=3)

    assert [c.id for c in out] == ["c1", "c0", "c2"]
    assert all(c.rerank_score is None for c in out)


def test_rerank_enabled_reorders_and_sets_final_score() -> None:
    # AC5 (enabled): a fixture that reorders yields an order != fused, and
    # final score == rerank_score * weight.
    fixture = FixtureRerankerClient({"q": {"alpha": 0.1, "bravo": 0.9, "charlie": 0.5}})
    retriever = _bare_retriever(fixture)
    out = retriever.rerank("q", _weighted_candidates(), top_n=3)

    assert [c.id for c in out] == ["c1", "c2", "c0"]  # bravo, charlie, alpha
    top = out[0]
    assert top.rerank_score == 0.9
    assert top.score == 0.9 * 1.5  # weight boost applied


def test_rank_delta_positive_on_reorder() -> None:
    # AC9: a reordering fixture -> mean |Δpos| > 0 and monotonic False.
    fixture = FixtureRerankerClient({"q": {"alpha": 0.1, "bravo": 0.9, "charlie": 0.5}})
    retriever = _bare_retriever(fixture)
    candidates = [
        _cand("c0", "alpha", score=0.9),
        _cand("c1", "bravo", score=0.8),
        _cand("c2", "charlie", score=0.7),
    ]
    retriever.rerank("q", candidates, top_n=3)

    debug = retriever.last_rerank
    assert debug is not None
    assert debug.rank_delta_mean > 0
    assert debug.monotonic is False
    assert debug.fallback_used is False


def test_rank_delta_zero_on_identity() -> None:
    # AC9: an order-preserving fixture -> delta 0, monotonic True.
    fixture = FixtureRerankerClient({"q": {"alpha": 0.9, "bravo": 0.5, "charlie": 0.1}})
    retriever = _bare_retriever(fixture)
    candidates = [
        _cand("c0", "alpha", score=0.9),
        _cand("c1", "bravo", score=0.8),
        _cand("c2", "charlie", score=0.7),
    ]
    retriever.rerank("q", candidates, top_n=3)

    debug = retriever.last_rerank
    assert debug is not None
    assert debug.rank_delta_mean == 0.0
    assert debug.monotonic is True


def test_rerank_degrades_on_raw_unavailable_error() -> None:
    # AC3 (defense-in-depth): a bare client that RAISES RerankerUnavailableError
    # (no GracefulReranker) is still caught by the retriever -> weighted-RRF.
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    raw = JinaRerankerClient(
        "jina-reranker-v2",
        provider="jina",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    retriever = _bare_retriever(raw)
    out = retriever.rerank("q", _weighted_candidates(), top_n=3)

    assert [c.id for c in out] == ["c1", "c0", "c2"]
    assert all(c.rerank_score is None for c in out)
    assert retriever.last_rerank is not None
    assert retriever.last_rerank.fallback_used is True
