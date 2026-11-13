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
  (``'simple'`` config over the shared tokeniser's output, never raw content —
  so dotted/code lexemes match the rescorer exactly) picks up to 200
  candidates by ``ts_rank_cd`` (candidate-then-rescore) and the
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
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from sqlalchemy import (
    Select,
    bindparam,
    case,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy import cast as cast_
from sqlalchemy.dialects.postgresql import TSQUERY, TSVECTOR
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy import Table

from forge_contracts.dtos import Chunk, IndexResult, KnowledgeScope, RetrievedChunk
from forge_contracts.enums import ChunkType
from forge_contracts.protocols import EmbeddingClient
from forge_db.models import (
    KnowledgeSource,
    RetrievalChunk,
    RetrievalTermStat,
    RetrievalWorkspaceStat,
)
from forge_knowledge.text import query_terms as _query_terms_of
from forge_knowledge.text import tokenize, tsquery_literal, tsvector_literal

__all__ = [
    "Bm25Store",
    "KnowledgeSourceNotFoundError",
    "PgVectorStore",
    "VectorLegUnconfiguredError",
    "bm25_score",
    "rebuild_bm25_stats",
]

# A session factory: anything callable that returns a new ``Session`` (e.g. a
# SQLAlchemy ``sessionmaker``). Matches ``forge_db.create_session_factory``.
SessionFactory = Callable[[], Session]

# Okapi BM25 free parameters (standard defaults).
_BM25_K1 = 1.5
_BM25_B = 0.75

#: Postgres candidate-pool size for BM25 candidate-then-rescore (spec J §2.5).
_BM25_CANDIDATE_LIMIT = 200

#: Page size for ``IN`` lists and bulk cleanups. SQLite caps bound variables
#: at 32,766 and Postgres at 65,535 per statement, so every term/path list
#: that can grow with corpus size is paged well below both.
_IN_PAGE = 5_000

#: Texts per ``embed`` call, for new chunks and the NULL-embedding backlog
#: alike (``HttpEmbeddingClient`` sends one POST per call; OpenAI accepts at
#: most 2,048 inputs per request).
_EMBED_BATCH = 256

#: Keyset page size for :func:`rebuild_bm25_stats`.
_REBUILD_PAGE = 2_000

#: Batch size for bulk term-stat inserts in :func:`rebuild_bm25_stats`.
_TERM_INSERT_BATCH = 10_000

#: ``retrieval_term_stats.term`` column width; longer terms are not tracked.
_TERM_MAX_LEN = 255

#: Tokens from :func:`~forge_knowledge.text.tokenize` are ``[a-z0-9_]+``, so
#: they are safe inside a quoted tsvector/tsquery lexeme. Asserted at query time.
_SAFE_LEXEME_RE = re.compile(r"[a-z0-9_]+")


def _pages(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    """Yield ``items`` in slices of at most ``size`` (IN-list paging)."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _dialect_insert(session: Session, model: Any) -> Any:
    """The dialect ``insert`` for ``model`` (for ``on_conflict_do_update``)."""
    if session.get_bind().dialect.name == "postgresql":
        return pg_insert(model)
    return sqlite_insert(model)


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
    """Apply signed document-frequency deltas for one workspace (uncommitted).

    A single additive dialect upsert (``df = df + excluded.df``) evaluated by
    the database, so two concurrent indexers in one workspace cannot lose
    each other's increments the way a Python read-modify-write under READ
    COMMITTED did; it also closes the get-or-create race. Rows run in sorted
    term order for a stable lock order. Decrements send a negative ``df``
    (a not-yet-existing term briefly lands at a negative value inside this
    transaction) and are followed by a paged ``df <= 0`` cleanup.
    """
    # Terms beyond the column width (pathological single-token blobs) are not
    # tracked; ``bm25_score`` then reads their df as 0 (maximally rare).
    items = sorted(
        (term, sign * delta)
        for term, delta in df_delta.items()
        if len(term) <= _TERM_MAX_LEN and delta
    )
    if not items:
        return
    ins = _dialect_insert(session, RetrievalTermStat)
    stmt = ins.on_conflict_do_update(
        index_elements=["workspace_id", "term"],
        set_={
            "df": RetrievalTermStat.df + ins.excluded.df,
            "updated_at": func.now(),
        },
    )
    session.execute(
        stmt,
        [{"workspace_id": workspace_id, "term": term, "df": signed} for term, signed in items],
    )
    if sign < 0:
        for page in _pages([term for term, _ in items], _IN_PAGE):
            session.execute(
                delete(RetrievalTermStat).where(
                    RetrievalTermStat.workspace_id == workspace_id,
                    RetrievalTermStat.term.in_(list(page)),
                    RetrievalTermStat.df <= 0,
                )
            )


def _bump_workspace_stat(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    chunks_delta: int,
    tokens_delta: int,
) -> None:
    """Apply signed corpus-total deltas for one workspace (uncommitted).

    Additive dialect upsert keyed on ``workspace_id`` (see
    :func:`_bump_term_stats` for why the arithmetic must happen in the
    database); totals are clamped at zero with ``case`` (``greatest``/``max``
    are not portable across the dialects). The insert side carries the
    clamped delta so a decrement against a missing row inserts zeros.
    """
    if chunks_delta == 0 and tokens_delta == 0:
        return
    ins = _dialect_insert(session, RetrievalWorkspaceStat).values(
        workspace_id=workspace_id,
        chunk_count=max(chunks_delta, 0),
        total_tokens=max(tokens_delta, 0),
    )
    stmt = ins.on_conflict_do_update(
        index_elements=["workspace_id"],
        set_={
            "chunk_count": case(
                (RetrievalWorkspaceStat.chunk_count + chunks_delta < 0, 0),
                else_=RetrievalWorkspaceStat.chunk_count + chunks_delta,
            ),
            "total_tokens": case(
                (RetrievalWorkspaceStat.total_tokens + tokens_delta < 0, 0),
                else_=RetrievalWorkspaceStat.total_tokens + tokens_delta,
            ),
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)


def rebuild_bm25_stats(session: Session, workspace_id: uuid.UUID) -> int:
    """Recompute one workspace's BM25 rows and statistics from scratch.

    Idempotent repair for drifted statistics (concurrent-writer losses from
    before the additive upserts, the purge/cascade gap, pre-J pods writing
    ``token_count=0`` chunks during a rolling upgrade): deletes the
    workspace's term/workspace statistics, then re-derives them — plus every
    chunk's ``token_count`` and, on Postgres, its ``'simple'``-config ``tsv``
    over the tokeniser's output — in keyset pages of
    :data:`_REBUILD_PAGE` rows, tokenising each row once in Python and
    writing one executemany per page. Terms longer than the stats column
    width are excluded from the df table but still count toward
    ``token_count``/``total_tokens``, exactly as the store maintains them.

    Runs inside the caller's transaction (flushed, not committed) so the
    delete/rebuild swap is atomic. Returns the number of chunks rebuilt.
    """
    is_postgres = session.get_bind().dialect.name == "postgresql"
    session.execute(delete(RetrievalTermStat).where(RetrievalTermStat.workspace_id == workspace_id))
    session.execute(
        delete(RetrievalWorkspaceStat).where(RetrievalWorkspaceStat.workspace_id == workspace_id)
    )

    doc_freq: Counter[str] = Counter()
    chunk_count = 0
    total_tokens = 0
    last_id: uuid.UUID | None = None
    while True:
        stmt = select(RetrievalChunk.id, RetrievalChunk.content).where(
            RetrievalChunk.workspace_id == workspace_id
        )
        if last_id is not None:
            stmt = stmt.where(RetrievalChunk.id > last_id)
        rows = session.execute(stmt.order_by(RetrievalChunk.id.asc()).limit(_REBUILD_PAGE)).all()
        if not rows:
            break
        updates: list[dict[str, Any]] = []
        for row in rows:
            tokens = tokenize(row.content or "")
            chunk_count += 1
            total_tokens += len(tokens)
            doc_freq.update({t for t in set(tokens) if len(t) <= _TERM_MAX_LEN})
            entry: dict[str, Any] = {"b_id": row.id, "b_count": len(tokens)}
            if is_postgres:
                entry["b_text"] = tsvector_literal(tokens)
            updates.append(entry)
        # Core (table-level) UPDATE: bindparam names must not equal column
        # names in an executemany UPDATE, and the ORM bulk path would demand
        # the pk under its own name.
        chunk_table = cast("Table", RetrievalChunk.__table__)
        if is_postgres:
            update_stmt = (
                update(chunk_table)
                .where(chunk_table.c.id == bindparam("b_id"))
                .values(
                    token_count=bindparam("b_count"),
                    tsv=cast_(bindparam("b_text"), TSVECTOR),
                )
            )
        else:
            update_stmt = (
                update(chunk_table)
                .where(chunk_table.c.id == bindparam("b_id"))
                .values(token_count=bindparam("b_count"))
            )
        session.execute(update_stmt, updates)
        last_id = rows[-1].id

    if chunk_count:
        session.add(
            RetrievalWorkspaceStat(
                workspace_id=workspace_id,
                chunk_count=chunk_count,
                total_tokens=total_tokens,
            )
        )
    term_rows = [
        {"workspace_id": workspace_id, "term": term, "df": df}
        for term, df in sorted(doc_freq.items())
    ]
    for page in _pages(term_rows, _TERM_INSERT_BATCH):
        session.execute(insert(RetrievalTermStat), list(page))
    session.flush()
    return chunk_count


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
            # Embedded before any statistics row is locked, in bounded batches.
            vectors: list[list[float] | None]
            if self._embedding is not None:
                vectors = []
                for batch in _pages(pending, _EMBED_BATCH):
                    vectors.extend(
                        list(v) for v in self._embedding.embed([c.content for c, _ in batch])
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
                    # The tokens themselves as lexemes, written without any
                    # text-search parser (see ``forge_knowledge.text``).
                    tsv: Any = cast_(tsvector_literal(tokens), TSVECTOR)
                    row.tsv = tsv
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

    def embed_missing(self, source_id: str) -> tuple[int, list[str]]:
        """Embed this source's ``embedding IS NULL`` rows; ``(embedded, errors)``.

        Rows indexed before an embedder was configured (BM25-only mode, or
        pre-J hash vectors NULLed by migration 0042) are never re-indexed, since
        content-hash dedup skips them, so every sync calls this after its own
        writes. Each batch of :data:`_EMBED_BATCH` rows commits on its own and
        touches only ``embedding``, so no statistics lock is held while the
        provider answers and a failure never undoes newly indexed chunks. A
        batch the provider rejects is retried one row at a time; a row that
        still fails is skipped and reported, so one bad row cannot block the
        rest of the backlog.
        """
        if self._embedding is None:
            return 0, []
        source_uuid = uuid.UUID(str(source_id))
        embedded = 0
        errors: list[str] = []
        last_id: uuid.UUID | None = None
        while True:
            with self._session_factory() as session:
                stmt = select(RetrievalChunk.id, RetrievalChunk.content).where(
                    RetrievalChunk.knowledge_source_id == source_uuid,
                    RetrievalChunk.embedding.is_(None),
                )
                if last_id is not None:
                    stmt = stmt.where(RetrievalChunk.id > last_id)
                batch = session.execute(
                    stmt.order_by(RetrievalChunk.id.asc()).limit(_EMBED_BATCH)
                ).all()
            if not batch:
                return embedded, errors
            last_id = batch[-1].id
            vectors = self._embed_rows(batch, errors)
            if vectors:
                with self._session_factory() as session:
                    for row_id, vector in vectors.items():
                        row = session.get(RetrievalChunk, row_id)
                        if row is not None and row.embedding is None:
                            row.embedding = vector
                            embedded += 1
                    session.commit()

    def _embed_rows(self, batch: Sequence[Any], errors: list[str]) -> dict[uuid.UUID, list[float]]:
        assert self._embedding is not None
        try:
            vectors = self._embedding.embed([row.content for row in batch])
            return {row.id: list(v) for row, v in zip(batch, vectors, strict=True)}
        except Exception as exc:
            if len(batch) == 1:
                errors.append(f"chunk {batch[0].id}: {type(exc).__name__}")
                return {}
        out: dict[uuid.UUID, list[float]] = {}
        for row in batch:
            out.update(self._embed_rows([row], errors))
        return out

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
        # Sorted, so concurrent deletes page through rows in the same order.
        path_list = sorted(set(paths))
        if not path_list:
            return 0
        source_uuid = uuid.UUID(str(source_id))
        with self._session_factory() as session:
            # DELETE … RETURNING (no pre-SELECT): statistics are backed out
            # for exactly the rows this statement removed, so a row committed
            # concurrently between a read and a delete can neither be deleted
            # unaccounted nor backed out twice. The path IN list is paged.
            df_by_workspace: dict[uuid.UUID, Counter[str]] = {}
            totals_by_workspace: dict[uuid.UUID, tuple[int, int]] = {}
            removed = 0
            for page in _pages(path_list, _IN_PAGE):
                rows = session.execute(
                    delete(RetrievalChunk)
                    .where(RetrievalChunk.knowledge_source_id == source_uuid)
                    .where(RetrievalChunk.path.in_(list(page)))
                    .returning(
                        RetrievalChunk.workspace_id,
                        RetrievalChunk.content,
                        RetrievalChunk.token_count,
                    )
                ).all()
                removed += len(rows)
                for row in rows:
                    tokens = tokenize(row.content)
                    df_by_workspace.setdefault(row.workspace_id, Counter()).update(set(tokens))
                    chunks_n, tokens_n = totals_by_workspace.get(row.workspace_id, (0, 0))
                    totals_by_workspace[row.workspace_id] = (
                        chunks_n + 1,
                        tokens_n + (row.token_count or len(tokens)),
                    )
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
        # Shared query view (stopwords dropped identically on both dialects):
        # a stopword-only query returns [] everywhere.
        query_terms = _query_terms_of(query)
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
        # it). The query is the exact tokens as quoted lexemes, matching the
        # stored vector built the same way, so a candidate contains a query
        # token exactly as ``tokenize`` sees it (``retry`` never matches
        # ``retry_count``; ``__all__`` matches itself).
        assert all(_SAFE_LEXEME_RE.fullmatch(term) for term in query_terms), (
            "query terms must be tokenizer output ([a-z0-9_]+)"
        )
        ts_query = cast_(tsquery_literal(query_terms), TSQUERY)
        # ``tsv`` is set by every writer and backfilled for every pre-existing
        # row by migration 0042, so query it directly — the GIN index
        # ``ix_retrieval_chunk_tsv`` is usable (a ``coalesce`` wrapper was not).
        rank = func.ts_rank_cd(RetrievalChunk.tsv, ts_query)
        stmt: Select = select(RetrievalChunk).where(RetrievalChunk.tsv.op("@@")(ts_query))
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
        tracked = [term for term in query_terms if len(term) <= _TERM_MAX_LEN]
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
            df: dict[str, int] = {}
            for page in _pages(tracked, _IN_PAGE):
                df.update(
                    {
                        term: int(value)
                        for term, value in session.execute(
                            select(RetrievalTermStat.term, RetrievalTermStat.df).where(
                                RetrievalTermStat.workspace_id == scope.workspace_id,
                                RetrievalTermStat.term.in_(list(page)),
                            )
                        ).all()
                    }
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
        df = {}
        for page in _pages(tracked, _IN_PAGE):
            df.update(
                {
                    term: int(value)
                    for term, value in session.execute(
                        select(
                            RetrievalTermStat.term,
                            func.coalesce(func.sum(RetrievalTermStat.df), 0),
                        )
                        .where(RetrievalTermStat.term.in_(list(page)))
                        .group_by(RetrievalTermStat.term)
                    ).all()
                }
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
