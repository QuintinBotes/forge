"""The Knowledge service: end-to-end hybrid search (plan Task 1.3, RAG spine).

:class:`KnowledgeService` is the public face of the retrieval pipeline and the
proof that the spine works end-to-end. It structurally satisfies the frozen
:class:`forge_contracts.protocols.KnowledgeStore` Protocol (``index`` + ``search``)
and orchestrates, per the spec's *Knowledge and Retrieval Architecture* diagram::

    query
      ├─ semantic search (pgvector, cosine)  → top-N
      ├─ keyword search   (Postgres BM25)    → top-N
      │            │
      │            ▼
      │   RRF fusion (k = 60)
      │            │
      │            ▼
      │   cross-encoder rerank (+ chunk-type weight boost)
      │            │
      │            ▼
      └─ attributed top-k  (source_id / source_uri carried through)

``index`` delegates to the pgvector store, which persists the rows both legs read
(on Postgres it also populates the ``tsvector`` the BM25 leg ranks over), so a
single index call feeds the whole hybrid pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable

from forge_contracts.dtos import Chunk, IndexResult, KnowledgeScope, RetrievedChunk
from forge_contracts.protocols import EmbeddingClient, RerankerClient
from forge_knowledge.retriever import HybridRetriever
from forge_knowledge.stores import Bm25Store, PgVectorStore, SessionFactory

__all__ = ["KnowledgeService"]

#: How many results each leg (semantic / keyword) contributes before fusion
#: (spec pipeline: "top-50" per leg).
DEFAULT_CANDIDATE_K: int = 50

#: How many fused candidates are sent to the (more expensive) reranker.
DEFAULT_RERANK_CANDIDATES: int = 50


class KnowledgeService:
    """Hybrid retrieval service. Implements ``KnowledgeStore``."""

    def __init__(
        self,
        retriever: HybridRetriever,
        *,
        vector_store: PgVectorStore | None = None,
        candidate_k: int = DEFAULT_CANDIDATE_K,
        rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    ) -> None:
        self._retriever = retriever
        self._vector_store = vector_store
        self._candidate_k = candidate_k
        self._rerank_candidates = rerank_candidates

    @classmethod
    def from_session_factory(
        cls,
        session_factory: SessionFactory,
        embedding_client: EmbeddingClient,
        reranker: RerankerClient,
        *,
        candidate_k: int = DEFAULT_CANDIDATE_K,
        rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    ) -> KnowledgeService:
        """Build a service from a DB session factory and BYOK clients."""
        vector_store = PgVectorStore(session_factory, embedding_client)
        keyword_store = Bm25Store(session_factory)
        retriever = HybridRetriever(vector_store, keyword_store, reranker)
        return cls(
            retriever,
            vector_store=vector_store,
            candidate_k=candidate_k,
            rerank_candidates=rerank_candidates,
        )

    def index(self, source_id: str, chunks: list[Chunk]) -> IndexResult:
        """Embed and persist ``chunks`` for ``source_id`` (idempotent by hash)."""
        return self._require_store().index(source_id, chunks)

    # -- sync surface (Task 1.4): makes the service a ``sync.SyncStore``) ---- #

    def source_paths(self, source_id: str) -> set[str]:
        """Distinct file paths currently indexed for ``source_id``."""
        return self._require_store().source_paths(source_id)

    def delete_source_paths(self, source_id: str, paths: Iterable[str]) -> int:
        """Delete every indexed chunk of ``source_id`` whose path is in ``paths``."""
        return self._require_store().delete_source_paths(source_id, paths)

    def _require_store(self) -> PgVectorStore:
        if self._vector_store is None:
            raise RuntimeError(
                "KnowledgeService has no vector store; build it with "
                "KnowledgeService.from_session_factory to enable indexing/sync."
            )
        return self._vector_store

    def search(
        self, query: str, scope: KnowledgeScope, k: int = 10
    ) -> list[RetrievedChunk]:
        """Hybrid search: semantic + keyword -> RRF -> rerank -> attributed top-k."""
        semantic = self._retriever.semantic(query, scope, self._candidate_k)
        keyword = self._retriever.keyword(query, scope, self._candidate_k)
        fused = self._retriever.fuse([semantic, keyword])
        candidates = fused[: self._rerank_candidates]
        return self._retriever.rerank(query, candidates, k)
