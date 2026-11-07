"""Postgres-backed integration tests for the stores (plan Task 1.2, RAG spine).

These exercise the *real* Postgres code paths the SQLite unit tests can only
approximate: pgvector ``cosine_distance`` ordering and BM25
candidate-then-rescore (OR ``tsquery`` candidates by ``ts_rank_cd``, rescored
with exact Okapi BM25 — spec J §2.5, ADR-0017), including the proof that
SQLite and Postgres rank the same corpus identically and that migration
``0041_retrieval_bm25_stats`` backfills statistics for pre-existing chunks.
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


def _seed_sqlite() -> tuple[sessionmaker[Session], uuid.UUID, uuid.UUID]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    ids = _seed(factory)
    PgVectorStore(factory, DeterministicEmbeddingClient()).index(
        source_id=str(ids[1]), chunks=_PARITY_CORPUS
    )
    return factory, ids[0], ids[1]


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


def test_migration_backfills_bm25_stats_for_existing_chunks(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Upgrade a scratch database only to 0040, insert chunks the way a
    # pre-migration deployment would have them (token_count at its 0 default,
    # no statistics rows), then upgrade to head: 0041 must backfill token
    # counts, document frequencies, corpus totals and (on Postgres) tsv.
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
    }
    try:
        command.upgrade(cfg, "0040_self_eval_baseline")
        engine = create_engine(scratch_url, future=True)
        try:
            with engine.begin() as conn:
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
                            "token_count) VALUES (:id, :ws, :src, 'code', 1.0, "
                            ":content, :path, :hash, '{}', 0)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "ws": workspace_id,
                            "src": source_id,
                            "content": content,
                            "path": path,
                            "hash": f"hash-{path}",
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
                assert totals.chunk_count == 3
                assert totals.total_tokens == sum(len(tokenize(c)) for c in contents.values())

                retry_df = conn.execute(
                    text(
                        "SELECT df FROM retrieval_term_stats "
                        "WHERE workspace_id = :ws AND term = 'retry'"
                    ),
                    {"ws": workspace_id},
                ).scalar()
                assert retry_df == 2

                missing_tsv = conn.execute(
                    text("SELECT count(*) FROM retrieval_chunk WHERE tsv IS NULL")
                ).scalar()
                assert missing_tsv == 0

            # Downgrade removes exactly what 0041 introduced.
            command.downgrade(cfg, "0040_self_eval_baseline")
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
