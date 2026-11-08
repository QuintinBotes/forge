"""Tests for ``forge_knowledge.stores`` (plan Task 1.2, RAG spine).

Covers the two indexed stores that sit on top of the ``forge_db`` data model:

* :class:`PgVectorStore` — ``index``/``search`` with cosine similarity. On
  Postgres it uses pgvector's ``cosine_distance``; on SQLite (the hermetic
  unit-test backend) it computes cosine in Python over the JSON-degraded
  ``embedding`` column. Same code path, dialect-aware.
* :class:`Bm25Store` — keyword ``search`` (exact Okapi BM25 via the shared
  ``bm25_score`` on both dialects; Postgres candidate-then-rescore is covered
  in ``test_stores_postgres.py``).

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
from forge_db.models import (
    KnowledgeSource,
    RetrievalChunk,
    RetrievalTermStat,
    RetrievalWorkspaceStat,
    Workspace,
)
from forge_db.session import create_session_factory
from forge_knowledge.embeddings import DeterministicEmbeddingClient
from forge_knowledge.stores import (
    Bm25Store,
    KnowledgeSourceNotFoundError,
    PgVectorStore,
    VectorLegUnconfiguredError,
    bm25_score,
    rebuild_bm25_stats,
)
from forge_knowledge.text import tokenize


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
        source = KnowledgeSource(workspace_id=workspace_id, kind="repo", name="api", uri=uri)
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


# --------------------------------------------------------------------------- #
# bm25_score — the shared pure scorer (spec J §2.5)                            #
# --------------------------------------------------------------------------- #


def test_bm25_score_matches_hand_computed_okapi() -> None:
    import math

    # N=3 docs, avg_len=4; doc has tf(retry)=2 over 3 tokens; df(retry)=2.
    doc = ["retry", "retry", "timeout"]
    idf = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))
    expected = idf * (2 * 2.5) / (2 + 1.5 * (1 - 0.75 + 0.75 * 3 / 4.0))
    assert bm25_score(["retry"], doc, {"retry": 2}, 3, 4.0) == pytest.approx(expected)


def test_bm25_score_edge_cases() -> None:
    assert bm25_score(["x"], [], {"x": 1}, 3, 4.0) == 0.0
    assert bm25_score(["x"], ["x"], {"x": 1}, 0, 4.0) == 0.0
    assert bm25_score(["absent"], ["x", "y"], {}, 3, 4.0) == 0.0
    # Repeated query terms count once; rarer terms (lower df) score higher.
    doc = ["alpha", "beta", "gamma"]
    single = bm25_score(["alpha"], doc, {"alpha": 1}, 10, 3.0)
    doubled = bm25_score(["alpha", "alpha"], doc, {"alpha": 1}, 10, 3.0)
    assert doubled == single
    assert bm25_score(["alpha"], doc, {"alpha": 1}, 10, 3.0) > bm25_score(
        ["alpha"], doc, {"alpha": 9}, 10, 3.0
    )


def test_bm25_partial_match_still_ranks_on_sqlite(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    # A query mixing a present term with an absent one still returns the
    # partial match — BM25's recall property. Note: the plainto/AND
    # regression this shape of query exposes exists only on Postgres (SQLite
    # scores in Python and cannot AND); test_bm25_or_query_keeps_recall_on_
    # postgres in test_stores_postgres.py guards that regression.
    source_id = _make_source(session_factory, workspace_id)
    PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128)).index(
        source_id=str(source_id),
        chunks=[_chunk("retry timeout backoff helper", "retry.py")],
    )
    hits = Bm25Store(session_factory).search(
        "retry absentterm", KnowledgeScope(workspace_id=workspace_id), k=5
    )
    assert [h.path for h in hits] == ["retry.py"]


# --------------------------------------------------------------------------- #
# BM25 statistics maintenance (same transaction as chunk writes)               #
# --------------------------------------------------------------------------- #


def test_index_maintains_bm25_statistics(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    contents = ["retry retry timeout", "single retry mention", "other words here"]
    store.index(
        source_id=str(source_id),
        chunks=[_chunk(c, f"f{i}.py") for i, c in enumerate(contents)],
    )

    with session_factory() as session:
        totals = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals is not None
        assert totals.chunk_count == 3
        assert totals.total_tokens == sum(len(tokenize(c)) for c in contents)

        df = {
            row.term: row.df
            for row in session.scalars(
                select(RetrievalTermStat).where(RetrievalTermStat.workspace_id == workspace_id)
            )
        }
        assert df["retry"] == 2  # in two documents, counted once per document
        assert df["timeout"] == 1

        token_counts = {
            row.path: row.token_count for row in session.scalars(select(RetrievalChunk))
        }
        assert token_counts == {f"f{i}.py": len(tokenize(c)) for i, c in enumerate(contents)}


def test_delete_source_paths_backs_out_bm25_statistics(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    store.index(
        source_id=str(source_id),
        chunks=[
            _chunk("retry retry timeout", "retry.py"),
            _chunk("single retry mention", "misc.py"),
            _chunk("other words here", "other.py"),
        ],
    )

    removed = store.delete_source_paths(str(source_id), ["retry.py"])
    assert removed == 1

    with session_factory() as session:
        totals = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals is not None
        assert totals.chunk_count == 2
        assert totals.total_tokens == len(tokenize("single retry mention")) + len(
            tokenize("other words here")
        )
        df = {
            row.term: row.df
            for row in session.scalars(
                select(RetrievalTermStat).where(RetrievalTermStat.workspace_id == workspace_id)
            )
        }
        assert df["retry"] == 1
        assert "timeout" not in df  # df hit zero -> row removed

    hits = Bm25Store(session_factory).search(
        "retry", KnowledgeScope(workspace_id=workspace_id), k=5
    )
    assert [h.path for h in hits] == ["misc.py"]


# --------------------------------------------------------------------------- #
# No embedder configured: BM25-only, honestly (ADR-0018)                       #
# --------------------------------------------------------------------------- #


def test_index_without_embedder_stores_no_vectors_but_bm25_works(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, None)
    assert store.has_embedder is False

    result = store.index(
        source_id=str(source_id),
        chunks=[_chunk("validate the jwt token here", "auth.py")],
    )
    assert result.indexed == 1

    with session_factory() as session:
        row = session.scalars(select(RetrievalChunk)).one()
    assert row.embedding is None
    assert row.token_count == len(tokenize("validate the jwt token here"))

    hits = Bm25Store(session_factory).search(
        "jwt token", KnowledgeScope(workspace_id=workspace_id), k=5
    )
    assert [h.path for h in hits] == ["auth.py"]


def test_vector_search_without_embedder_refuses_loudly(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    store = PgVectorStore(session_factory, None)
    with pytest.raises(VectorLegUnconfiguredError, match="requires a configured embedder"):
        store.search("jwt", KnowledgeScope(workspace_id=workspace_id), k=5)


# --------------------------------------------------------------------------- #
# Correction pass: re-embedding NULL rows (item 2)                             #
# --------------------------------------------------------------------------- #


def test_configuring_embedder_later_reembeds_null_rows(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    # Chunks indexed in BM25-only mode (or NULLed by migration 0042) must not
    # stay unembedded forever behind content-hash dedup: every sync runs the
    # backlog step after its own writes.
    source_id = _make_source(session_factory, workspace_id)
    chunks = [_chunk(f"chunk number {i} retry", f"f{i}.py") for i in range(5)]
    first = PgVectorStore(session_factory, None).index(source_id=str(source_id), chunks=chunks)
    assert first.indexed == 5

    with session_factory() as session:
        totals_before = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals_before is not None
        before = (totals_before.chunk_count, totals_before.total_tokens)
        df_before = {
            row.term: row.df
            for row in session.scalars(
                select(RetrievalTermStat).where(RetrievalTermStat.workspace_id == workspace_id)
            )
        }

    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    result = store.index(source_id=str(source_id), chunks=chunks)
    assert (result.indexed, result.skipped) == (0, 5)
    assert store.embed_missing(str(source_id)) == (5, [])
    assert store.embed_missing(str(source_id)) == (0, [])
    with session_factory() as session:
        embeddings = [row.embedding for row in session.scalars(select(RetrievalChunk))]
        assert all(emb is not None and len(emb) == 128 for emb in embeddings)
        # BM25 statistics untouched by the re-embed pass.
        totals_after = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals_after is not None
        assert (totals_after.chunk_count, totals_after.total_tokens) == before
        df_after = {
            row.term: row.df
            for row in session.scalars(
                select(RetrievalTermStat).where(RetrievalTermStat.workspace_id == workspace_id)
            )
        }
        assert df_after == df_before

    # The semantic leg now actually finds them.
    hits = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128)).search(
        "chunk retry", KnowledgeScope(workspace_id=workspace_id), k=3
    )
    assert hits


def test_sqlite_null_embedding_is_true_null_not_json_null(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    # Without JSON(none_as_null=True) SQLite stored the literal 'null',
    # which passed isnot(None) and resurrected rows as score-0 vector hits.
    source_id = _make_source(session_factory, workspace_id)
    PgVectorStore(session_factory, None).index(
        source_id=str(source_id), chunks=[_chunk("alpha beta", "a.py")]
    )
    store = PgVectorStore(session_factory, DeterministicEmbeddingClient(dimension=128))
    assert store.search("alpha", KnowledgeScope(workspace_id=workspace_id), k=5) == []


# --------------------------------------------------------------------------- #
# Correction pass: stopword query view (item 1)                                #
# --------------------------------------------------------------------------- #


def test_stopword_only_query_returns_nothing_on_sqlite(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    PgVectorStore(session_factory, None).index(
        source_id=str(source_id),
        chunks=[_chunk("the retry and the timeout", "retry.py")],
    )
    store = Bm25Store(session_factory)
    scope = KnowledgeScope(workspace_id=workspace_id)
    assert store.search("the and of", scope, k=5) == []
    # A mixed query behaves as its content terms alone.
    assert [h.path for h in store.search("the retry", scope, k=5)] == ["retry.py"]


# --------------------------------------------------------------------------- #
# Correction pass: maintained statistics are what query time reads (item 11)   #
# --------------------------------------------------------------------------- #


def test_bm25_uses_workspace_wide_maintained_statistics_sqlite(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    # Two sources share one workspace; the scope narrows results to one, but
    # scores must use the workspace-wide maintained df/N/avg_len. Stubbing
    # _load_stats to ({}, 0, 1.0) (derive-from-corpus fallback) changes the
    # score and fails this test.
    source_a = _make_source(session_factory, workspace_id, uri="github.com/org/api")
    source_b = _make_source(session_factory, workspace_id, uri="github.com/org/other")
    store = PgVectorStore(session_factory, None)
    contents_a = ["retry retry timeout backoff", "single retry mention here"]
    contents_b = ["retry in another source entirely", "unrelated words only here now"]
    store.index(
        source_id=str(source_a),
        chunks=[_chunk(c, f"a{i}.py") for i, c in enumerate(contents_a)],
    )
    store.index(
        source_id=str(source_b),
        chunks=[_chunk(c, f"b{i}.py") for i, c in enumerate(contents_b)],
    )

    all_contents = contents_a + contents_b
    df_ws = {term: sum(1 for c in all_contents if term in set(tokenize(c))) for term in ("retry",)}
    n_ws = len(all_contents)
    avg_ws = sum(len(tokenize(c)) for c in all_contents) / n_ws

    hits = Bm25Store(session_factory).search(
        "retry",
        KnowledgeScope(workspace_id=workspace_id, repos=["github.com/org/api"]),
        k=10,
    )
    assert {h.path for h in hits} == {"a0.py", "a1.py"}
    by_path = {f"a{i}.py": c for i, c in enumerate(contents_a)}
    for hit in hits:
        expected = bm25_score(["retry"], tokenize(by_path[hit.path or ""]), df_ws, n_ws, avg_ws)
        assert hit.score == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Correction pass: >32k distinct terms (item 6) + rebuild (implied)            #
# --------------------------------------------------------------------------- #


def test_index_many_distinct_terms_beyond_sqlite_variable_limit(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    # 40k distinct terms overflowed a single IN list on SQLite (32,766 bound
    # variables) and made the source unindexable forever.
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, None)
    big = " ".join(f"term{i}" for i in range(40_000))
    store.index(source_id=str(source_id), chunks=[_chunk(big, "big.py")])

    # Re-index with overlap: shared terms accumulate df rather than erroring.
    overlap = "term0 term1 term39999 extra_shared"
    store.index(source_id=str(source_id), chunks=[_chunk(overlap, "overlap.py")])

    with session_factory() as session:
        totals = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals is not None
        assert totals.chunk_count == 2
        df0 = session.scalar(
            select(RetrievalTermStat.df).where(
                RetrievalTermStat.workspace_id == workspace_id,
                RetrievalTermStat.term == "term0",
            )
        )
        assert df0 == 2
        term_rows = session.scalar(
            select(func.count())
            .select_from(RetrievalTermStat)
            .where(RetrievalTermStat.workspace_id == workspace_id)
        )
        assert term_rows == 40_001  # 40k big + extra_shared

    removed = store.delete_source_paths(str(source_id), ["big.py"])
    assert removed == 1
    with session_factory() as session:
        totals = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals is not None
        assert totals.chunk_count == 1
        df0 = session.scalar(
            select(RetrievalTermStat.df).where(
                RetrievalTermStat.workspace_id == workspace_id,
                RetrievalTermStat.term == "term0",
            )
        )
        assert df0 == 1
        term_rows = session.scalar(
            select(func.count())
            .select_from(RetrievalTermStat)
            .where(RetrievalTermStat.workspace_id == workspace_id)
        )
        assert term_rows == 4  # only overlap.py's terms survive


def test_rebuild_bm25_stats_repairs_drift_idempotently(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    store = PgVectorStore(session_factory, None)
    contents = ["retry retry timeout", "single retry mention", "other words here"]
    store.index(
        source_id=str(source_id),
        chunks=[_chunk(c, f"f{i}.py") for i, c in enumerate(contents)],
    )

    # Corrupt everything the way drift does: wrong totals, a phantom term,
    # zeroed token counts, a missing term row.
    with session_factory() as session:
        totals = session.scalar(select(RetrievalWorkspaceStat))
        assert totals is not None
        totals.chunk_count = 999
        totals.total_tokens = 12345
        session.add(RetrievalTermStat(workspace_id=workspace_id, term="phantomterm", df=7))
        for row in session.scalars(select(RetrievalChunk)):
            row.token_count = 0
        retry_row = session.scalar(
            select(RetrievalTermStat).where(RetrievalTermStat.term == "retry")
        )
        assert retry_row is not None
        session.delete(retry_row)
        session.commit()

    with session_factory() as session:
        rebuilt = rebuild_bm25_stats(session, workspace_id)
        session.commit()
    assert rebuilt == 3

    def _snapshot() -> tuple[tuple[int, int], dict[str, int], dict[str | None, int]]:
        with session_factory() as session:
            totals = session.scalar(select(RetrievalWorkspaceStat))
            assert totals is not None
            df = {row.term: row.df for row in session.scalars(select(RetrievalTermStat))}
            counts = {row.path: row.token_count for row in session.scalars(select(RetrievalChunk))}
            return (totals.chunk_count, totals.total_tokens), df, counts

    (chunk_count, total_tokens), df, token_counts = _snapshot()
    assert chunk_count == 3
    assert total_tokens == sum(len(tokenize(c)) for c in contents)
    assert df["retry"] == 2
    assert "phantomterm" not in df
    assert token_counts == {f"f{i}.py": len(tokenize(c)) for i, c in enumerate(contents)}

    # Idempotent: a second rebuild derives exactly the same state.
    with session_factory() as session:
        assert rebuild_bm25_stats(session, workspace_id) == 3
        session.commit()
    assert _snapshot() == ((chunk_count, total_tokens), df, token_counts)


class SpyEmbedder:
    """Deterministic vectors; records each call; refuses any text with "poison"."""

    def __init__(self) -> None:
        self._inner = DeterministicEmbeddingClient(dimension=16)
        self.dimension = 16
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if any("poison" in text for text in texts):
            raise RuntimeError("provider rejected the batch")
        return [list(v) for v in self._inner.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return list(self._inner.embed_query(text))


def test_new_chunks_are_embedded_in_bounded_batches(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    source_id = _make_source(session_factory, workspace_id)
    spy = SpyEmbedder()
    chunks = [_chunk(f"chunk {i} body", f"f{i}.py") for i in range(600)]
    PgVectorStore(session_factory, spy).index(source_id=str(source_id), chunks=chunks)
    assert [len(call) for call in spy.calls] == [256, 256, 88]


def test_a_backlog_failure_never_undoes_new_chunks_and_skips_only_bad_rows(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    from forge_knowledge.sync import full_sync

    source_id = str(_make_source(session_factory, workspace_id))
    old = {"a.py": "def alpha(): pass", "b.py": "poison pill", "c.py": "def gamma(): pass"}
    full_sync(PgVectorStore(session_factory, None), source_id, old)

    spy = SpyEmbedder()
    result = full_sync(PgVectorStore(session_factory, spy), source_id, {**old, "d.py": "new"})

    assert result.indexed == 1
    # a.py and c.py, each a code chunk plus its symbol summary (D-c); b.py is
    # reported, not retried forever.
    assert result.updated == 4
    assert len(result.errors) == 1
    with session_factory() as session:
        by_path = {row.path: row.embedding for row in session.scalars(select(RetrievalChunk))}
    assert by_path["d.py"] is not None
    assert by_path["a.py"] is not None and by_path["c.py"] is not None
    assert by_path["b.py"] is None


def test_full_sync_never_sends_a_vanished_files_text_to_the_embedder(
    session_factory: sessionmaker[Session], workspace_id: uuid.UUID
) -> None:
    from forge_knowledge.sync import full_sync

    source_id = str(_make_source(session_factory, workspace_id))
    full_sync(
        PgVectorStore(session_factory, None),
        source_id,
        {"keep.py": "def keep(): pass", "gone.py": "def vanished_secret_file(): pass"},
    )
    spy = SpyEmbedder()
    result = full_sync(
        PgVectorStore(session_factory, spy), source_id, {"keep.py": "def keep(): pass"}
    )
    sent = [text for call in spy.calls for text in call]
    assert not any("vanished_secret_file" in text for text in sent)
    # One code chunk plus one symbol summary (D-c) per file.
    assert (result.deleted, result.updated) == (2, 2)
