"""Postgres-backed integration tests for the stores (plan Task 1.2, RAG spine).

These exercise the *real* Postgres code paths the SQLite unit tests can only
approximate: pgvector ``cosine_distance`` ordering and BM25
candidate-then-rescore (OR ``tsquery`` candidates by ``ts_rank_cd``, rescored
with exact Okapi BM25 — spec J §2.5, ADR-0017), including the proof that
SQLite and Postgres rank the same corpus identically and that migration
``0042_retrieval_bm25_stats`` backfills statistics for pre-existing chunks.
They use the shared ``pg_engine`` fixture (root ``conftest.py``), which yields
a pgvector-enabled Postgres from ``FORGE_TEST_DATABASE_URL`` or a
``testcontainers`` container, and otherwise **skips** with a parked reason —
never faked. This is the Phase-2 / CI verification of the live retrieval path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import Chunk, KnowledgeScope
from forge_db.base import Base
from forge_db.models import (
    KnowledgeSource,
    RetrievalTermStat,
    RetrievalWorkspaceStat,
    Workspace,
)
from forge_db.session import create_session_factory
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


def test_bm25_candidate_rescore_on_postgres(
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

    # Indexing maintained the BM25 statistics in the same transaction.
    with pg_session_factory() as session:
        totals = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals is not None
        assert totals.chunk_count == 3
        retry_df = session.scalar(
            select(RetrievalTermStat.df).where(
                RetrievalTermStat.workspace_id == workspace_id,
                RetrievalTermStat.term == "retry",
            )
        )
        assert retry_df == 2


def test_bm25_or_query_keeps_recall_on_postgres(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # The old plainto_tsquery ANDed terms: "retry absentterm" matched nothing.
    # The OR tsquery keeps the partial match (BM25 recall, spec J §2.5).
    workspace_id, source_id = _seed(pg_session_factory)
    PgVectorStore(pg_session_factory, DeterministicEmbeddingClient()).index(
        source_id=str(source_id),
        chunks=[
            Chunk(content="retry timeout backoff helper", path="retry.py"),
            Chunk(content="completely different subject", path="other.py"),
        ],
    )
    hits = Bm25Store(pg_session_factory).search(
        "retry absentterm", KnowledgeScope(workspace_id=workspace_id), k=5
    )
    assert [h.path for h in hits] == ["retry.py"]


_PARITY_CORPUS = [
    Chunk(content="retry retry retry timeout backoff helper", path="retry.py"),
    Chunk(content="timeout timeout configuration network calls retry", path="timeouts.py"),
    Chunk(content="single retry mention among unrelated words timeout note", path="misc.py"),
    Chunk(content="database connection pooling transactions postgres", path="db.py"),
    Chunk(content="oauth token validation expiry refresh flow", path="auth.py"),
    Chunk(
        content=(
            "retry once inside a much longer document about scheduling workers "
            "queues leases heartbeats metrics tracing logging config rollout"
        ),
        path="long.py",
    ),
]


def _seed_sqlite(
    corpus: list[Chunk] | None = None,
) -> tuple[sessionmaker[Session], uuid.UUID, uuid.UUID]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    ids = _seed(factory)
    PgVectorStore(factory, DeterministicEmbeddingClient()).index(
        source_id=str(ids[1]), chunks=corpus if corpus is not None else _PARITY_CORPUS
    )
    return factory, ids[0], ids[1]


def _assert_parity(
    pg_factory: sessionmaker[Session],
    corpus: list[Chunk],
    queries: list[tuple[str, bool]],
) -> None:
    """Index ``corpus`` on both dialects; assert identical ordered hits.

    ``queries`` pairs each query with whether a match is expected (SQLite
    must be non-empty then, so parity cannot pass vacuously on ``[] == []``).
    Chunk ids are random UUIDs per database, so ordered paths are the id
    proxy (paths are unique per corpus).
    """
    pg_workspace_id, pg_source_id = _seed(pg_factory)
    PgVectorStore(pg_factory, DeterministicEmbeddingClient()).index(
        source_id=str(pg_source_id), chunks=corpus
    )
    sqlite_factory, sqlite_workspace_id, _ = _seed_sqlite(corpus)
    for query, expect_match in queries:
        pg_hits = Bm25Store(pg_factory).search(
            query, KnowledgeScope(workspace_id=pg_workspace_id), k=10
        )
        sqlite_hits = Bm25Store(sqlite_factory).search(
            query, KnowledgeScope(workspace_id=sqlite_workspace_id), k=10
        )
        if expect_match:
            assert sqlite_hits, f"SQLite unexpectedly empty for {query!r}"
        assert [h.path for h in pg_hits] == [h.path for h in sqlite_hits], query
        assert [h.score for h in pg_hits] == pytest.approx([h.score for h in sqlite_hits]), query


def test_sqlite_and_postgres_produce_identical_bm25_ranking(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # Spec J §2.5 / ADR-0017: one shared bm25_score + shared statistics means
    # both dialects rank the same corpus identically — order AND scores.
    pg_workspace_id, pg_source_id = _seed(pg_session_factory)
    PgVectorStore(pg_session_factory, DeterministicEmbeddingClient()).index(
        source_id=str(pg_source_id), chunks=_PARITY_CORPUS
    )
    sqlite_factory, sqlite_workspace_id, _ = _seed_sqlite()

    for query in ("retry timeout", "retry", "timeout database", "oauth token"):
        pg_hits = Bm25Store(pg_session_factory).search(
            query, KnowledgeScope(workspace_id=pg_workspace_id), k=10
        )
        sqlite_hits = Bm25Store(sqlite_factory).search(
            query, KnowledgeScope(workspace_id=sqlite_workspace_id), k=10
        )
        assert [h.path for h in pg_hits] == [h.path for h in sqlite_hits], query
        assert [h.score for h in pg_hits] == pytest.approx([h.score for h in sqlite_hits]), query


def test_parity_dotted_names_and_identifiers(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # The old 'english' tsv kept ``self.retry`` / ``obj.compute_rrf_score``
    # as single lexemes the tokeniser never emits, so Postgres never made
    # those chunks candidates (SQLite found them). Separate corpus: growing
    # _PARITY_CORPUS would change its statistics for the existing queries.
    corpus = [
        Chunk(content="call self.retry() on failure", path="task.py"),
        Chunk(content="retry the operation after a delay", path="helper.py"),
        Chunk(content="x = obj.compute_rrf_score(y)", path="calc.py"),
        Chunk(content="unrelated database pooling notes", path="db.py"),
    ]
    _assert_parity(
        pg_session_factory,
        corpus,
        [("retry", True), ("compute_rrf_score", True), ("self retry", True)],
    )


def test_parity_file_paths(pg_session_factory: sessionmaker[Session]) -> None:
    corpus = [
        Chunk(content="see src/forge/retry_helper.py for details", path="guide.md"),
        Chunk(content="the retry helper module overview", path="overview.md"),
        Chunk(content="nothing relevant in this chunk", path="other.md"),
    ]
    _assert_parity(
        pg_session_factory,
        corpus,
        [("retry_helper", True), ("forge retry_helper", True), ("retry", True)],
    )


def test_parity_stopword_queries(pg_session_factory: sessionmaker[Session]) -> None:
    corpus = [
        Chunk(content="the retry and the timeout helper", path="retry.py"),
        Chunk(content="of the and to in on at by", path="stopwords.py"),
        Chunk(content="plain database content chunk", path="db.py"),
    ]
    _assert_parity(
        pg_session_factory,
        corpus,
        [("the retry", True), ("the and of", False), ("the", False)],
    )


def test_parity_stem_flood_does_not_drown_exact_match(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # More flood chunks than the 200-candidate pool: under the old stemmed
    # tsquery they all matched "running", scored 0 in the rescore, and the
    # exact chunk fell out of the pool. Distinct contents (the indexer
    # dedups by content hash).
    from forge_knowledge.stores import _BM25_CANDIDATE_LIMIT

    corpus = [
        Chunk(
            content=f"run runs runner ran sprint{i} jog{i} dash{i}",
            path=f"flood{i}.py",
        )
        for i in range(_BM25_CANDIDATE_LIMIT + 5)
    ]
    corpus.append(Chunk(content="running the scheduler loop now", path="sched.py"))
    _assert_parity(pg_session_factory, corpus, [("running", True)])


def test_english_stopwords_match_postgres_english_config(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # Provenance guard for the shared stopword list: every entry is a
    # stopword of the Postgres 'english' config (empty tsvector), so the
    # list is the tsearch stop list, not an invented one.
    from forge_knowledge.text import ENGLISH_STOPWORDS

    with pg_session_factory() as session:
        for word in sorted(ENGLISH_STOPWORDS):
            vector = session.scalar(text("SELECT to_tsvector('english', :w)::text"), {"w": word})
            assert vector == "", word


def test_bm25_uses_workspace_wide_maintained_statistics_postgres(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # Item 11, Postgres half: scores come from the maintained workspace-wide
    # statistics even when the scope narrows to one of two sources.
    from forge_knowledge.stores import bm25_score
    from forge_knowledge.text import tokenize

    workspace_id, source_a = _seed(pg_session_factory)
    with pg_session_factory() as session:
        source_b_row = KnowledgeSource(
            workspace_id=workspace_id,
            kind="repo",
            name="api",
            uri="github.com/org/other",
        )
        session.add(source_b_row)
        session.flush()
        source_b = source_b_row.id
        session.commit()

    store = PgVectorStore(pg_session_factory, None)
    contents_a = ["retry retry timeout backoff", "single retry mention here"]
    contents_b = ["retry in another source entirely", "unrelated words only here now"]
    store.index(
        source_id=str(source_a),
        chunks=[Chunk(content=c, path=f"a{i}.py") for i, c in enumerate(contents_a)],
    )
    store.index(
        source_id=str(source_b),
        chunks=[Chunk(content=c, path=f"b{i}.py") for i, c in enumerate(contents_b)],
    )

    all_contents = contents_a + contents_b
    df_ws = {"retry": sum(1 for c in all_contents if "retry" in set(tokenize(c)))}
    n_ws = len(all_contents)
    avg_ws = sum(len(tokenize(c)) for c in all_contents) / n_ws

    hits = Bm25Store(pg_session_factory).search(
        "retry",
        KnowledgeScope(workspace_id=workspace_id, repos=["github.com/org/api"]),
        k=10,
    )
    assert {h.path for h in hits} == {"a0.py", "a1.py"}
    by_path = {f"a{i}.py": c for i, c in enumerate(contents_a)}
    for hit in hits:
        expected = bm25_score(["retry"], tokenize(by_path[hit.path or ""]), df_ws, n_ws, avg_ws)
        assert hit.score == pytest.approx(expected)


def test_concurrent_indexers_do_not_lose_statistic_updates(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # Item 5: two sessions indexing into one workspace concurrently. The
    # additive upserts must land every increment: shared-term df counts all
    # three chunks and the corpus totals count all three chunks.
    import threading

    workspace_id, source_a = _seed(pg_session_factory)
    with pg_session_factory() as session:
        source_b_row = KnowledgeSource(
            workspace_id=workspace_id,
            kind="repo",
            name="api-b",
            uri="github.com/org/api-b",
        )
        session.add(source_b_row)
        session.flush()
        source_b = source_b_row.id
        session.commit()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _index(source_id: uuid.UUID, chunks: list[Chunk]) -> None:
        try:
            barrier.wait(timeout=10)
            PgVectorStore(pg_session_factory, None).index(source_id=str(source_id), chunks=chunks)
        except BaseException as exc:  # collected and asserted below
            errors.append(exc)

    threads = [
        threading.Thread(
            target=_index,
            args=(
                source_a,
                [
                    Chunk(content="retry alpha shared", path="a1.py"),
                    Chunk(content="retry beta shared", path="a2.py"),
                ],
            ),
        ),
        threading.Thread(
            target=_index,
            args=(source_b, [Chunk(content="retry gamma shared", path="b1.py")]),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []

    with pg_session_factory() as session:
        totals = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
        assert totals is not None
        assert totals.chunk_count == 3
        assert totals.total_tokens == 9
        retry_df = session.scalar(
            select(RetrievalTermStat.df).where(
                RetrievalTermStat.workspace_id == workspace_id,
                RetrievalTermStat.term == "retry",
            )
        )
        assert retry_df == 3
        chunk_rows = session.scalar(
            text("SELECT count(*) FROM retrieval_chunk WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        assert chunk_rows == 3


def test_migration_backfills_bm25_stats_for_existing_chunks(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Upgrade a scratch database only to 0041, drop token_count + the stats
    # tables to stand in for a pre-0042 deployment schema (the metadata
    # baseline provisions them early on a fresh chain), insert chunks with
    # pre-J hash vectors, then upgrade to head: 0042 must backfill token
    # counts, document frequencies, corpus totals and (on Postgres) the
    # 'simple' tsv, and NULL the old hash vectors.
    from alembic import command

    from forge_db.migration_utils import build_alembic_config
    from forge_knowledge.text import tokenize

    base_url = make_url(postgres_url)
    scratch_name = f"forge_bm25_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{scratch_name}"'))
    finally:
        admin.dispose()

    scratch_url = base_url.set(database=scratch_name).render_as_string(hide_password=False)
    monkeypatch.setenv("FORGE_DATABASE_URL", scratch_url)
    cfg = build_alembic_config(scratch_url)

    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    contents = {
        "retry.py": "retry retry timeout backoff",
        "misc.py": "single retry mention",
        "other.py": "unrelated words entirely",
        # Item 3: a term longer than VARCHAR(255) aborted the old backfill
        # with StringDataRightTruncation (this repo's own design-system.html
        # has a 269-char token).
        "blob.txt": "retry " + "a" * 300,
    }
    fake_vector = "[" + ",".join(["0.0"] * 1536) + "]"
    try:
        command.upgrade(cfg, "0041_world_model")
        engine = create_engine(scratch_url, future=True)
        try:
            with engine.begin() as conn:
                # Exercise the real add-column path: the metadata baseline
                # provisions token_count + the stats tables ahead of 0042 on
                # a fresh chain, so drop them to stand in for a genuinely
                # pre-0042 deployment schema.
                conn.execute(text("ALTER TABLE retrieval_chunk DROP COLUMN IF EXISTS token_count"))
                conn.execute(text("DROP TABLE IF EXISTS retrieval_term_stats"))
                conn.execute(text("DROP TABLE IF EXISTS retrieval_workspace_stats"))
                conn.execute(
                    text(
                        "INSERT INTO workspace (id, name, slug, settings) "
                        "VALUES (:id, 'Acme', :slug, '{}')"
                    ),
                    {"id": workspace_id, "slug": f"acme-{workspace_id.hex[:8]}"},
                )
                conn.execute(
                    text(
                        "INSERT INTO knowledge_source "
                        "(id, workspace_id, kind, name, uri, sync_mode, config) "
                        "VALUES (:id, :ws, 'repo', 'api', 'github.com/org/api', "
                        "'full', '{}')"
                    ),
                    {"id": source_id, "ws": workspace_id},
                )
                for path, content in contents.items():
                    conn.execute(
                        text(
                            "INSERT INTO retrieval_chunk "
                            "(id, workspace_id, knowledge_source_id, chunk_type, "
                            "weight, content, path, content_hash, metadata, "
                            "embedding) VALUES (:id, :ws, :src, 'code', 1.0, "
                            ":content, :path, :hash, '{}', CAST(:emb AS vector))"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "ws": workspace_id,
                            "src": source_id,
                            "content": content,
                            "path": path,
                            "hash": f"hash-{path}",
                            # A pre-J (hash-embedder) vector: 0042 must NULL it.
                            "emb": fake_vector,
                        },
                    )

            command.upgrade(cfg, "head")

            with engine.connect() as conn:
                token_counts = dict(
                    conn.execute(text("SELECT path, token_count FROM retrieval_chunk")).all()
                )
                assert token_counts == {
                    path: len(tokenize(content)) for path, content in contents.items()
                }

                totals = conn.execute(
                    text(
                        "SELECT chunk_count, total_tokens FROM "
                        "retrieval_workspace_stats WHERE workspace_id = :ws"
                    ),
                    {"ws": workspace_id},
                ).one()
                assert totals.chunk_count == len(contents)
                assert totals.total_tokens == sum(len(tokenize(c)) for c in contents.values())

                retry_df = conn.execute(
                    text(
                        "SELECT df FROM retrieval_term_stats "
                        "WHERE workspace_id = :ws AND term = 'retry'"
                    ),
                    {"ws": workspace_id},
                ).scalar()
                assert retry_df == 3  # retry.py, misc.py, blob.txt

                # Item 3: the 300-char token counts toward token_count (above)
                # but no term row may exceed the VARCHAR(255) column width.
                long_terms = conn.execute(
                    text("SELECT count(*) FROM retrieval_term_stats WHERE length(term) > 255")
                ).scalar()
                assert long_terms == 0

                missing_tsv = conn.execute(
                    text("SELECT count(*) FROM retrieval_chunk WHERE tsv IS NULL")
                ).scalar()
                assert missing_tsv == 0

                # The backfilled tsv is the 'simple' token vector: it matches
                # a 'simple' tsquery for the exact token.
                simple_match = conn.execute(
                    text(
                        "SELECT count(*) FROM retrieval_chunk "
                        "WHERE tsv @@ to_tsquery('simple', 'retry')"
                    )
                ).scalar()
                assert simple_match == 3

                # Item 2: pre-0042 (hash-embedder) vectors are NULLed in the
                # add-column branch, to be re-embedded on the next sync.
                surviving_vectors = conn.execute(
                    text("SELECT count(*) FROM retrieval_chunk WHERE embedding IS NOT NULL")
                ).scalar()
                assert surviving_vectors == 0

            # Downgrade removes exactly what 0042 introduced.
            command.downgrade(cfg, "0041_world_model")
            inspector = inspect(engine)
            assert "retrieval_term_stats" not in inspector.get_table_names()
            assert "retrieval_workspace_stats" not in inspector.get_table_names()
            chunk_columns = {c["name"] for c in inspector.get_columns("retrieval_chunk")}
            assert "token_count" not in chunk_columns
        finally:
            engine.dispose()
    finally:
        teardown = create_engine(base_url, isolation_level="AUTOCOMMIT", future=True)
        try:
            with teardown.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :n AND pid <> pg_backend_pid()"
                    ),
                    {"n": scratch_name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}"'))
        finally:
            teardown.dispose()


def test_parity_snake_case_flood_keeps_exact_tokens(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # A text-search parser splits ``retry_count`` at the underscore, so every
    # flood chunk below matched ``retry`` and the exact chunk fell out of the
    # 200-candidate pool on Postgres. Lexemes are now the tokens themselves.
    from forge_knowledge.stores import _BM25_CANDIDATE_LIMIT

    corpus = [
        Chunk(
            content=f"self.retry_count{i} = retry_limit + retry_delay - retry_backoff",
            path=f"flood{i}.py",
        )
        for i in range(_BM25_CANDIDATE_LIMIT + 5)
    ]
    corpus.append(Chunk(content="on failure call retry once from the scheduler", path="exact.py"))
    _assert_parity(pg_session_factory, corpus, [("retry", True), ("retry_limit", True)])


def test_parity_identifiers_with_edge_underscores_and_long_tokens(
    pg_session_factory: sessionmaker[Session],
) -> None:
    long_token = "q" * 2100
    corpus = [
        Chunk(content="__all__ = ['search']", path="mod/__init__.py"),
        Chunk(content="for _ in range(3): pass", path="loop.py"),
        Chunk(content="retry_count = 3", path="worker.py"),
        Chunk(content="retry count is three", path="prose.md"),
        Chunk(content=f"blob = {long_token}", path="blob.py"),
    ]
    _assert_parity(
        pg_session_factory,
        corpus,
        [
            ("__all__", True),
            ("_", True),
            ("retry_count", True),
            (long_token, True),
            ("all", False),
        ],
    )


def test_parity_holds_for_queries_beyond_the_term_cap(
    pg_session_factory: sessionmaker[Session],
) -> None:
    # Postgres evaluates a tsquery recursively and overflowed its stack at ~15k
    # terms; both dialects keep the first MAX_QUERY_TERMS distinct terms.
    from forge_knowledge.text import MAX_QUERY_TERMS

    corpus = [
        Chunk(content="alpha retry handler", path="a.py"),
        Chunk(content="omega timeout handler", path="b.py"),
    ]
    filler = " ".join(f"t{i}" for i in range(20_000))
    _assert_parity(
        pg_session_factory,
        corpus,
        [(f"retry {filler}", True), (f"{filler} omega", False)],
    )
    assert MAX_QUERY_TERMS < 20_000


def test_concurrent_stat_upserts_add_up(pg_session_factory: sessionmaker[Session]) -> None:
    # Forced interleaving: session A holds an uncommitted increment while
    # session B increments the same term and blocks on the row lock. A
    # read-modify-write would lose one; the additive upsert cannot.
    import threading
    from collections import Counter

    from forge_knowledge.stores import _bump_term_stats, _bump_workspace_stat

    workspace_id, _ = _seed(pg_session_factory)
    first = pg_session_factory()
    _bump_term_stats(first, workspace_id, Counter({"retry": 1}), sign=+1)
    _bump_workspace_stat(first, workspace_id, chunks_delta=1, tokens_delta=5)

    def second() -> None:
        with pg_session_factory() as session:
            _bump_term_stats(session, workspace_id, Counter({"retry": 1}), sign=+1)
            _bump_workspace_stat(session, workspace_id, chunks_delta=1, tokens_delta=7)
            session.commit()

    thread = threading.Thread(target=second)
    thread.start()
    thread.join(timeout=1.0)
    assert thread.is_alive(), "second writer should wait on the first's row lock"
    first.commit()
    first.close()
    thread.join(timeout=10)
    assert not thread.is_alive()

    with pg_session_factory() as session:
        df = session.scalar(
            select(RetrievalTermStat.df).where(
                RetrievalTermStat.workspace_id == workspace_id, RetrievalTermStat.term == "retry"
            )
        )
        totals = session.scalar(
            select(RetrievalWorkspaceStat).where(
                RetrievalWorkspaceStat.workspace_id == workspace_id
            )
        )
    assert df == 2
    assert totals is not None
    assert (totals.chunk_count, totals.total_tokens) == (2, 12)
