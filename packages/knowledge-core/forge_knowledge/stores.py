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

* :class:`Bm25Store` — the *lexical* leg. ``search`` ranks by keyword relevance:
  Postgres ``to_tsvector``/``plainto_tsquery``/``ts_rank`` natively, and a real
  Okapi-BM25 scorer in Python on SQLite. This recovers exact-identifier matches
  (e.g. a rare symbol) that a dense embedding dilutes — the documented reason
  Forge retrieval is always hybrid.

Both stores are dialect-aware so the identical call sites work against the SQLite
unit-test backend and the Postgres production/CI backend.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any, cast

import numpy as np
from sqlalchemy import CursorResult, Select, delete, func, or_, select
from sqlalchemy.orm import Session

from forge_contracts.dtos import Chunk, IndexResult, KnowledgeScope, RetrievedChunk
from forge_contracts.enums import ChunkType
from forge_contracts.protocols import EmbeddingClient
from forge_db.models import KnowledgeSource, RetrievalChunk
from forge_knowledge.text import tokenize

__all__ = [
    "Bm25Store",
    "KnowledgeSourceNotFoundError",
    "PgVectorStore",
]

# A session factory: anything callable that returns a new ``Session`` (e.g. a
# SQLAlchemy ``sessionmaker``). Matches ``forge_db.create_session_factory``.
SessionFactory = Callable[[], Session]

# Okapi BM25 free parameters (standard defaults).
_BM25_K1 = 1.5
_BM25_B = 0.75


class KnowledgeSourceNotFoundError(LookupError):
    """Raised when ``index`` targets a ``source_id`` that has no row."""


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


class PgVectorStore:
    """Semantic (dense) retrieval over pgvector. Implements ``KnowledgeStore``."""

    def __init__(self, session_factory: SessionFactory, embedding_client: EmbeddingClient) -> None:
        self._session_factory = session_factory
        self._embedding = embedding_client

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

            vectors = (
                self._embedding.embed([chunk.content for chunk, _ in pending]) if pending else []
            )

            is_postgres = session.get_bind().dialect.name == "postgresql"
            for (chunk, content_hash), vector in zip(pending, vectors, strict=True):
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
                )
                if is_postgres:
                    row.tsv = func.to_tsvector("english", chunk.content)
                session.add(row)

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
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(RetrievalChunk)
                    .where(RetrievalChunk.knowledge_source_id == source_uuid)
                    .where(RetrievalChunk.path.in_(path_list))
                ),
            )
            session.commit()
            return int(result.rowcount or 0)

    # -- search ---------------------------------------------------------- #

    def search(self, query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]:
        """Return the ``k`` chunks most cosine-similar to ``query``."""
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
    """Lexical (keyword) retrieval. Postgres ``ts_rank`` / Python Okapi-BM25."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def search(self, query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]:
        """Return up to ``k`` chunks ranked by keyword relevance to ``query``."""
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        with self._session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                return self._search_postgres(session, query, scope, k)
            return self._search_python(session, query_tokens, scope, k)

    def _search_postgres(
        self, session: Session, query: str, scope: KnowledgeScope, k: int
    ) -> list[RetrievedChunk]:
        ts_query = func.plainto_tsquery("english", query)
        ts_vector = func.to_tsvector("english", RetrievalChunk.content)
        rank = func.ts_rank(ts_vector, ts_query)
        stmt: Select = select(RetrievalChunk, rank.label("rank")).where(
            ts_vector.op("@@")(ts_query)
        )
        stmt = _scope_filter(stmt, scope).order_by(rank.desc()).limit(k)
        return [
            _to_retrieved(row, score=float(rank_value))
            for row, rank_value in session.execute(stmt).all()
        ]

    def _search_python(
        self, session: Session, query_tokens: list[str], scope: KnowledgeScope, k: int
    ) -> list[RetrievedChunk]:
        rows = list(session.scalars(_scope_filter(select(RetrievalChunk), scope)))
        if not rows:
            return []
        scored = self._bm25(query_tokens, rows)
        ranked = [(score, row) for score, row in scored if score > 0.0]
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [_to_retrieved(row, score=score) for score, row in ranked[:k]]

    @staticmethod
    def _bm25(
        query_tokens: list[str], rows: list[RetrievalChunk]
    ) -> list[tuple[float, RetrievalChunk]]:
        documents = [tokenize(row.content) for row in rows]
        n_docs = len(documents)
        avg_len = sum(len(doc) for doc in documents) / n_docs or 1.0

        doc_freq: Counter[str] = Counter()
        for doc in documents:
            doc_freq.update(set(doc))

        query_terms = set(query_tokens)
        scored: list[tuple[float, RetrievalChunk]] = []
        for row, doc in zip(rows, documents, strict=True):
            doc_len = len(doc) or 1
            term_freq = Counter(doc)
            score = 0.0
            for term in query_terms:
                freq = term_freq.get(term, 0)
                if freq == 0:
                    continue
                idf = math.log(1 + (n_docs - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                denom = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_len / avg_len)
                score += idf * (freq * (_BM25_K1 + 1)) / denom
            scored.append((score, row))
        return scored
