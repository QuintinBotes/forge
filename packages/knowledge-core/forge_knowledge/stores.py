"""Indexed retrieval stores for the Forge Knowledge/RAG pipeline (Task 1.2, spine).

Two stores sit on top of the ``forge_db`` :class:`~forge_db.models.RetrievalChunk`
table and form the two legs of hybrid retrieval (the RRF fusion + reranking that
combines them lands in Task 1.3):

* :class:`PgVectorStore` — the *semantic* leg. ``index`` embeds chunk content via
  an injected :class:`~forge_contracts.protocols.EmbeddingClient` and writes
  ``RetrievalChunk`` rows; ``search`` ranks by cosine similarity. On Postgres it
  uses pgvector's ``cosine_distance`` operator (index-friendly, runs in the DB);
  on SQLite it computes cosine in Python over the JSON-degraded ``embedding``
  column so the same store is unit-testable without a live Postgres. Indexing is
  idempotent by ``content_hash`` — the foundation incremental sync builds on
  (Task 1.4). It structurally satisfies the frozen ``KnowledgeStore`` Protocol.

* :class:`Bm25Store` — the *lexical* leg. ``search`` ranks by exact Okapi BM25
  on **both** dialects (spec J §2.5, ADR-0017): on Postgres an OR ``tsquery``
  picks up to 200 candidates by ``ts_rank_cd`` (candidate-then-rescore) and the
  shared pure :func:`bm25_score` rescores them with corpus statistics that
  indexing maintains incrementally (``retrieval_term_stats``,
  ``retrieval_chunk.token_count``, ``retrieval_workspace_stats``); on SQLite
  every in-scope chunk is a candidate, scored by the same function with the
  same statistics — so both dialects rank identically on the same corpus. This
  recovers exact-identifier matches (e.g. a rare symbol) that a dense embedding
  dilutes — the documented reason Forge retrieval is always hybrid.

Both stores are dialect-aware so the identical call sites work against the SQLite
unit-test backend and the Postgres production/CI backend. The vector store's
embedding client is optional (ADR-0018): with no embedder configured, indexing
stores chunks without embeddings, vector ``search`` refuses with
:class:`VectorLegUnconfiguredError`, and callers run the BM25 leg alone — and
say so — instead of silently faking semantic relevance.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, cast

import numpy as np
from sqlalchemy import CursorResult, Select, delete, func, or_, select
from sqlalchemy.orm import Session

from forge_contracts.dtos import Chunk, IndexResult, KnowledgeScope, RetrievedChunk
from forge_contracts.enums import ChunkType
from forge_contracts.protocols import EmbeddingClient
from forge_db.models import (
    KnowledgeSource,
    RetrievalChunk,
    RetrievalTermStat,
    RetrievalWorkspaceStat,
)
from forge_knowledge.text import tokenize

__all__ = [
    "Bm25Store",
    "KnowledgeSourceNotFoundError",
    "PgVectorStore",
    "VectorLegUnconfiguredError",
    "bm25_score",
]

# A session factory: anything callable that returns a new ``Session`` (e.g. a
# SQLAlchemy ``sessionmaker``). Matches ``forge_db.create_session_factory``.
SessionFactory = Callable[[], Session]

# Okapi BM25 free parameters (standard defaults).
_BM25_K1 = 1.5
_BM25_B = 0.75

#: Postgres candidate-pool size for BM25 candidate-then-rescore (spec J §2.5).
_BM25_CANDIDATE_LIMIT = 200


class KnowledgeSourceNotFoundError(LookupError):
    """Raised when ``index`` targets a ``source_id`` that has no row."""


class VectorLegUnconfiguredError(RuntimeError):
    """Raised when vector search is attempted with no embedder configured.

    Fail-closed (ADR-0007/0018): an unconfigured vector leg refuses loudly
    instead of returning keyword (or empty) results dressed as semantic ones.
    Callers that can degrade honestly — ``KnowledgeService`` in BM25-only mode
    — never call the vector leg; callers that strictly need vectors (an API
    route with ``require_vector``) map this to HTTP 503.
    """


def bm25_score(
    query_terms: Iterable[str],
    doc_terms: Sequence[str],
    df: Mapping[str, int],
    n_docs: int,
    avg_len: float,
    *,
    k1: float = _BM25_K1,
    b: float = _BM25_B,
) -> float:
    """Exact Okapi BM25 score of one document for ``query_terms`` (pure).

    The single scorer shared by both dialects (spec J §2.5): ``df`` maps a term
    to its corpus document frequency, ``n_docs`` is the corpus size and
    ``avg_len`` its mean token length. Repeated query terms count once. Uses
    the non-negative IDF form ``log(1 + (N - df + 0.5) / (df + 0.5))``.
    """
    if n_docs <= 0 or not doc_terms:
        return 0.0
    doc_len = len(doc_terms)
    mean_len = avg_len if avg_len > 0.0 else 1.0
    term_freq = Counter(doc_terms)
    score = 0.0
    for term in set(query_terms):
        freq = term_freq.get(term, 0)
        if freq == 0:
            continue
        doc_freq = df.get(term, 0)
        idf = math.log(1 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
        denom = freq + k1 * (1 - b + b * doc_len / mean_len)
        score += idf * (freq * (k1 + 1)) / denom
    return score


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope_filter(stmt: Select, scope: KnowledgeScope | None) -> Select:
    """Apply ``KnowledgeScope`` predicates (workspace, source kind, repos).

    The statement must already select from / join ``RetrievalChunk``; this joins
    ``KnowledgeSource`` for source-level predicates.
    """
    stmt = stmt.join(KnowledgeSource, RetrievalChunk.knowledge_source_id == KnowledgeSource.id)
    if scope is None:
        return stmt
    if scope.workspace_id is not None:
        stmt = stmt.where(RetrievalChunk.workspace_id == scope.workspace_id)
    if scope.source_types:
        stmt = stmt.where(KnowledgeSource.kind.in_(list(scope.source_types)))
    if scope.repos:
        repo_match = [KnowledgeSource.uri.contains(repo) for repo in scope.repos]
        repo_match.append(KnowledgeSource.name.in_(list(scope.repos)))
        stmt = stmt.where(or_(*repo_match))
    if scope.namespaces:
        # F40 MCP ACL re-check: an indexed MCP chunk records its origin namespace
        # in ``metadata.mcp_namespace`` (see McpResourceChunker). Re-enforce the
        # connection's namespace entitlement at query time — an MCP chunk survives
        # only if its namespace is allow-listed. Chunks with no ``mcp_namespace``
        # (repo/other content) are unaffected. ``->>`` on Postgres JSONB /
        # ``json_extract`` on SQLite; NULL when the key is absent.
        mcp_ns = RetrievalChunk.chunk_metadata["mcp_namespace"].as_string()
        stmt = stmt.where(or_(mcp_ns.is_(None), mcp_ns.in_(list(scope.namespaces))))
    return stmt


def _to_retrieved(row: RetrievalChunk, *, score: float) -> RetrievedChunk:
    """Project a ``RetrievalChunk`` ORM row to a contract ``RetrievedChunk``.

    Called inside an open session so the ``source`` relationship can supply
    ``source_uri`` for attribution.
    """
    source_uri = row.source.uri if row.source is not None else None
    return RetrievedChunk(
        id=str(row.id),
        content=row.content,
        chunk_type=ChunkType(row.chunk_type.value),
        path=row.path,
        start_line=row.start_line,
        end_line=row.end_line,
        score=score,
        weight=row.weight,
        source_id=str(row.knowledge_source_id),
        source_uri=source_uri,
        metadata=dict(row.chunk_metadata or {}),
    )


# -- BM25 corpus statistics (maintained with every chunk write) ------------ #


def _bump_term_stats(
    session: Session, workspace_id: uuid.UUID, df_delta: Counter[str], *, sign: int
) -> None:
    """Apply signed document-frequency deltas for one workspace (uncommitted)."""
    # Terms beyond the column width (pathological single-token blobs) are not
    # tracked; ``bm25_score`` then reads their df as 0 (maximally rare).
    df_delta = Counter({t: d for t, d in df_delta.items() if len(t) <= 255})
    if not df_delta:
        return
    rows = {
        row.term: row
        for row in session.scalars(
            select(RetrievalTermStat).where(
                RetrievalTermStat.workspace_id == workspace_id,
                RetrievalTermStat.term.in_(list(df_delta)),
            )
        )
    }
    for term, delta in df_delta.items():
        row = rows.get(term)
        if row is None:
            if sign > 0:
                session.add(RetrievalTermStat(workspace_id=workspace_id, term=term, df=delta))
            continue
        row.df += sign * delta
        if row.df <= 0:
            session.delete(row)


def _bump_workspace_stat(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    chunks_delta: int,
    tokens_delta: int,
) -> None:
    """Apply signed corpus-total deltas for one workspace (uncommitted)."""
    if chunks_delta == 0 and tokens_delta == 0:
        return
    row = session.scalar(
        select(RetrievalWorkspaceStat).where(RetrievalWorkspaceStat.workspace_id == workspace_id)
    )
    if row is None:
        if chunks_delta <= 0:
            return
        session.add(
            RetrievalWorkspaceStat(
                workspace_id=workspace_id,
                chunk_count=chunks_delta,
                total_tokens=max(tokens_delta, 0),
            )
        )
        return
    row.chunk_count = max(row.chunk_count + chunks_delta, 0)
    row.total_tokens = max(row.total_tokens + tokens_delta, 0)


class PgVectorStore:
    """Semantic (dense) retrieval over pgvector. Implements ``KnowledgeStore``."""

    def __init__(
        self, session_factory: SessionFactory, embedding_client: EmbeddingClient | None
    ) -> None:
        self._session_factory = session_factory
        self._embedding = embedding_client

    @property
    def has_embedder(self) -> bool:
        """Whether a real embedding client is configured (ADR-0018)."""
        return self._embedding is not None

    # -- indexing -------------------------------------------------------- #

    def index(self, source_id: str, chunks: list[Chunk]) -> IndexResult:
        """Embed and persist ``chunks`` for ``source_id`` (idempotent by hash)."""
        source_uuid = uuid.UUID(str(source_id))
        with self._session_factory() as session:
            source = session.get(KnowledgeSource, source_uuid)
            if source is None:
                raise KnowledgeSourceNotFoundError(source_id)

            existing = set(
                session.scalars(
                    select(RetrievalChunk.content_hash).where(
                        RetrievalChunk.knowledge_source_id == source_uuid
                    )
                )
            )

            pending: list[tuple[Chunk, str]] = []
            seen: set[str] = set()
            skipped = 0
            for chunk in chunks:
                content_hash = chunk.content_hash or _hash(chunk.content)
                if content_hash in existing or content_hash in seen:
                    skipped += 1
                    continue
                seen.add(content_hash)
                pending.append((chunk, content_hash))

            # No embedder configured (ADR-0018): chunks are stored without
            # embeddings so BM25-only search still works — never faked vectors.
            vectors: list[list[float] | None]
            if self._embedding is not None:
                vectors = (
                    list(self._embedding.embed([chunk.content for chunk, _ in pending]))
                    if pending
                    else []
                )
            else:
                vectors = [None] * len(pending)

            is_postgres = session.get_bind().dialect.name == "postgresql"
            df_delta: Counter[str] = Counter()
            tokens_total = 0
            for (chunk, content_hash), vector in zip(pending, vectors, strict=True):
                tokens = tokenize(chunk.content)
                tokens_total += len(tokens)
                df_delta.update(set(tokens))
                row = RetrievalChunk(
                    workspace_id=source.workspace_id,
                    knowledge_source_id=source_uuid,
                    chunk_type=chunk.chunk_type.value,
                    weight=chunk.weight,
                    content=chunk.content,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    language=chunk.language,
                    content_hash=content_hash,
                    chunk_metadata=dict(chunk.metadata),
                    embedding=vector,
                    token_count=len(tokens),
                )
                if is_postgres:
                    row.tsv = func.to_tsvector("english", chunk.content)
                session.add(row)

            # BM25 statistics move in the same transaction as the chunk writes.
            _bump_term_stats(session, source.workspace_id, df_delta, sign=+1)
            _bump_workspace_stat(
                session,
                source.workspace_id,
                chunks_delta=len(pending),
                tokens_delta=tokens_total,
            )

            session.commit()

        return IndexResult(source_id=str(source_id), indexed=len(pending), skipped=skipped)

    # -- introspection / deletion (foundation for sync, Task 1.4) -------- #

    def source_paths(self, source_id: str) -> set[str]:
        """Return the distinct, non-null ``path`` values indexed for a source."""
        source_uuid = uuid.UUID(str(source_id))
        with self._session_factory() as session:
            rows = session.scalars(
                select(RetrievalChunk.path)
                .where(RetrievalChunk.knowledge_source_id == source_uuid)
                .where(RetrievalChunk.path.isnot(None))
                .distinct()
            )
            return {path for path in rows if path is not None}

    def delete_source_paths(self, source_id: str, paths: Iterable[str]) -> int:
        """Delete every chunk of ``source_id`` whose ``path`` is in ``paths``.

        Returns the number of rows removed. Used by incremental sync to drop the
        chunks of files that changed (before re-indexing) or were deleted.
        """
        path_list = list(dict.fromkeys(paths))
        if not path_list:
            return 0
        source_uuid = uuid.UUID(str(source_id))
        with self._session_factory() as session:
            # Read the doomed rows first so their BM25 statistics can be backed
            # out in the same transaction as the delete.
            doomed = list(
                session.scalars(
                    select(RetrievalChunk)
                    .where(RetrievalChunk.knowledge_source_id == source_uuid)
                    .where(RetrievalChunk.path.in_(path_list))
                )
            )
            if not doomed:
                return 0
            df_by_workspace: dict[uuid.UUID, Counter[str]] = {}
            totals_by_workspace: dict[uuid.UUID, tuple[int, int]] = {}
            for row in doomed:
                tokens = tokenize(row.content)
                df_by_workspace.setdefault(row.workspace_id, Counter()).update(set(tokens))
                chunks_n, tokens_n = totals_by_workspace.get(row.workspace_id, (0, 0))
                totals_by_workspace[row.workspace_id] = (
                    chunks_n + 1,
                    tokens_n + (row.token_count or len(tokens)),
                )

            result = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(RetrievalChunk)
                    .where(RetrievalChunk.knowledge_source_id == source_uuid)
                    .where(RetrievalChunk.path.in_(path_list))
                ),
            )
            removed = int(result.rowcount or 0)
            if removed:
                for workspace_id, df_delta in df_by_workspace.items():
                    _bump_term_stats(session, workspace_id, df_delta, sign=-1)
                for workspace_id, (chunks_n, tokens_n) in totals_by_workspace.items():
                    _bump_workspace_stat(
                        session,
                        workspace_id,
                        chunks_delta=-chunks_n,
                        tokens_delta=-tokens_n,
                    )
            session.commit()
            return removed

    # -- search ---------------------------------------------------------- #

    def search(self, query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]:
        """Return the ``k`` chunks most cosine-similar to ``query``.

        Raises :class:`VectorLegUnconfiguredError` when no embedder is
        configured (ADR-0018) — never a silent keyword or empty substitute.
        """
        if self._embedding is None:
            raise VectorLegUnconfiguredError(
                "vector search requires a configured embedder "
                "(set FORGE_EMBEDDING_PROVIDER); the BM25 leg remains available"
            )
        query_vector = self._embedding.embed_query(query)
        with self._session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                return self._search_postgres(session, query_vector, scope, k)
            return self._search_python(session, query_vector, scope, k)

    def _search_postgres(
        self, session: Session, query_vector: list[float], scope: KnowledgeScope, k: int
    ) -> list[RetrievedChunk]:
        distance = RetrievalChunk.embedding.cosine_distance(query_vector)
        stmt: Select = select(RetrievalChunk, distance.label("distance")).where(
            RetrievalChunk.embedding.isnot(None)
        )
        stmt = _scope_filter(stmt, scope).order_by(distance.asc()).limit(k)
        return [
            _to_retrieved(row, score=1.0 - float(dist)) for row, dist in session.execute(stmt).all()
        ]

    def _search_python(
        self, session: Session, query_vector: list[float], scope: KnowledgeScope, k: int
    ) -> list[RetrievedChunk]:
        stmt: Select = select(RetrievalChunk).where(RetrievalChunk.embedding.isnot(None))
        rows = list(session.scalars(_scope_filter(stmt, scope)))
        query_arr = np.asarray(query_vector, dtype=np.float64)
        query_norm = float(np.linalg.norm(query_arr))
        scored: list[tuple[float, RetrievalChunk]] = []
        for row in rows:
            vector = np.asarray(row.embedding, dtype=np.float64)
            norm = float(np.linalg.norm(vector))
            similarity = (
                float(query_arr @ vector / (query_norm * norm))
                if query_norm > 0.0 and norm > 0.0
                else 0.0
            )
            scored.append((similarity, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [_to_retrieved(row, score=score) for score, row in scored[:k]]


class Bm25Store:
    """Lexical retrieval: exact Okapi BM25 on both dialects (ADR-0017).

    Postgres runs *candidate-then-rescore* — an OR ``tsquery`` over the query
    terms selects up to 200 candidates by ``ts_rank_cd`` (workspace/scope
    filtered), then :func:`bm25_score` rescores them with the incrementally
    maintained corpus statistics. SQLite scores every in-scope chunk with the
    same function and statistics, so both dialects rank identically.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def search(self, query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]:
        """Return up to ``k`` chunks ranked by BM25 relevance to ``query``."""
        query_terms = list(dict.fromkeys(tokenize(query)))
        if not query_terms:
            return []
        with self._session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                candidates = self._candidates_postgres(session, query_terms, scope)
                corpus: list[RetrievalChunk] | None = None
            else:
                candidates = list(session.scalars(_scope_filter(select(RetrievalChunk), scope)))
                corpus = candidates
            if not candidates:
                return []

            df, n_docs, avg_len = self._load_stats(session, scope, query_terms)
            if n_docs <= 0:
                # No maintained statistics (rows written outside the store, or
                # a pre-migration index): derive them from the visible corpus
                # — identically on both dialects — instead of ranking blind.
                if corpus is None:
                    corpus = list(session.scalars(_scope_filter(select(RetrievalChunk), scope)))
                df, n_docs, avg_len = self._derive_stats(corpus, query_terms)
            if n_docs <= 0:
                return []

            scored = [
                (bm25_score(query_terms, tokenize(row.content), df, n_docs, avg_len), row)
                for row in candidates
            ]
            ranked = [(score, row) for score, row in scored if score > 0.0]
            # Deterministic across dialects, ties included: chunk ids are
            # random UUIDs, so they must never break a tie.
            ranked.sort(key=lambda item: (-item[0], item[1].path or "", item[1].content))
            return [_to_retrieved(row, score=score) for score, row in ranked[:k]]

    def _candidates_postgres(
        self, session: Session, query_terms: list[str], scope: KnowledgeScope
    ) -> list[RetrievalChunk]:
        # OR semantics keep BM25's recall (``plainto_tsquery`` ANDs and loses
        # it). Terms come from ``tokenize`` (``[a-z0-9_]`` only), so they are
        # safe bare tsquery lexemes; edge underscores are stripped because the
        # tsquery parser treats them as separators.
        lexemes = [term.strip("_") for term in query_terms]
        lexemes = [lexeme for lexeme in lexemes if lexeme]
        if not lexemes:
            return []
        ts_query = func.to_tsquery("english", " | ".join(dict.fromkeys(lexemes)))
        # The stored ``tsv`` is populated at index time; ``coalesce`` keeps any
        # legacy/direct-inserted row (``tsv IS NULL``) discoverable exactly as
        # the previous compute-on-read path did.
        ts_vector = func.coalesce(
            RetrievalChunk.tsv, func.to_tsvector("english", RetrievalChunk.content)
        )
        rank = func.ts_rank_cd(ts_vector, ts_query)
        stmt: Select = select(RetrievalChunk).where(ts_vector.op("@@")(ts_query))
        stmt = _scope_filter(stmt, scope).order_by(rank.desc()).limit(_BM25_CANDIDATE_LIMIT)
        return list(session.scalars(stmt))

    @staticmethod
    def _load_stats(
        session: Session, scope: KnowledgeScope, query_terms: list[str]
    ) -> tuple[dict[str, int], int, float]:
        """Read maintained BM25 statistics; ``n_docs == 0`` when none exist.

        Statistics are kept per workspace (spec J §2.5); a search with no
        workspace in scope reads the global sums, so the same corpus yields
        the same statistics — and therefore the same ranking — on both
        dialects.
        """
        tracked = [term for term in query_terms if len(term) <= 255]
        if scope.workspace_id is not None:
            totals = session.scalar(
                select(RetrievalWorkspaceStat).where(
                    RetrievalWorkspaceStat.workspace_id == scope.workspace_id
                )
            )
            if totals is None or totals.chunk_count <= 0:
                return {}, 0, 1.0
            n_docs = totals.chunk_count
            avg_len = totals.total_tokens / n_docs or 1.0
            df = (
                {
                    term: int(value)
                    for term, value in session.execute(
                        select(RetrievalTermStat.term, RetrievalTermStat.df).where(
                            RetrievalTermStat.workspace_id == scope.workspace_id,
                            RetrievalTermStat.term.in_(tracked),
                        )
                    ).all()
                }
                if tracked
                else {}
            )
            return df, n_docs, avg_len

        chunks_total, tokens_total = session.execute(
            select(
                func.coalesce(func.sum(RetrievalWorkspaceStat.chunk_count), 0),
                func.coalesce(func.sum(RetrievalWorkspaceStat.total_tokens), 0),
            )
        ).one()
        n_docs = int(chunks_total or 0)
        if n_docs <= 0:
            return {}, 0, 1.0
        avg_len = int(tokens_total or 0) / n_docs or 1.0
        df = (
            {
                term: int(value)
                for term, value in session.execute(
                    select(
                        RetrievalTermStat.term,
                        func.coalesce(func.sum(RetrievalTermStat.df), 0),
                    )
                    .where(RetrievalTermStat.term.in_(tracked))
                    .group_by(RetrievalTermStat.term)
                ).all()
            }
            if tracked
            else {}
        )
        return df, n_docs, avg_len

    @staticmethod
    def _derive_stats(
        corpus: list[RetrievalChunk], query_terms: list[str]
    ) -> tuple[dict[str, int], int, float]:
        """Corpus statistics computed in Python (no maintained stats present)."""
        if not corpus:
            return {}, 0, 1.0
        wanted = set(query_terms)
        doc_freq: Counter[str] = Counter()
        total_tokens = 0
        for row in corpus:
            tokens = tokenize(row.content)
            total_tokens += len(tokens)
            doc_freq.update(set(tokens) & wanted)
        n_docs = len(corpus)
        return dict(doc_freq), n_docs, (total_tokens / n_docs or 1.0)
