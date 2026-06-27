"""Hybrid retriever: semantic + keyword + RRF + rerank (plan Task 1.3, spine).

:class:`HybridRetriever` ties the two indexed legs built in Task 1.2 — the
pgvector *semantic* store and the BM25 *keyword* store — to RRF fusion
(:mod:`forge_knowledge.fusion`) and a cross-encoder reranker
(:mod:`forge_knowledge.reranker`). It structurally satisfies the frozen
:class:`forge_contracts.protocols.Retriever` Protocol:

* :meth:`semantic` / :meth:`keyword` return ``Ranked`` lists (1-based ranks,
  each carrying its ``RetrievedChunk`` so attribution survives downstream);
* :meth:`fuse` is RRF (``score(d) = Σ 1/(k+rank_i(d))``, ``k=60``);
* :meth:`rerank` re-scores candidates with the cross-encoder, applies the
  chunk-type priority weight as a final boost (README 1.3, policy/AGENTS.md 1.5,
  spec 1.4, summary 1.2, default 1.0 — spec's *Chunk Types and Priority Weights*),
  and returns the attributed top-n.

The orchestration that calls these in sequence lives in
:class:`forge_knowledge.service.KnowledgeService`.
"""

from __future__ import annotations

from typing import Protocol

from forge_contracts.constants import RRF_K
from forge_contracts.dtos import KnowledgeScope, Ranked, RetrievedChunk
from forge_contracts.protocols import RerankerClient
from forge_knowledge.fusion import fuse

__all__ = ["HybridRetriever"]


class _SearchableStore(Protocol):
    """Minimal surface the retriever needs from each indexed store."""

    def search(
        self, query: str, scope: KnowledgeScope, k: int = 10
    ) -> list[RetrievedChunk]: ...


def _to_ranked(chunks: list[RetrievedChunk]) -> list[Ranked]:
    """Wrap an already-ordered ``RetrievedChunk`` list as 1-based ``Ranked``."""
    return [
        Ranked(
            chunk_id=chunk.id or f"_pos_{position}",
            score=chunk.score,
            rank=position,
            chunk=chunk,
        )
        for position, chunk in enumerate(chunks, start=1)
    ]


class HybridRetriever:
    """Hybrid retrieval primitives. Implements ``Retriever``."""

    def __init__(
        self,
        semantic_store: _SearchableStore,
        keyword_store: _SearchableStore,
        reranker: RerankerClient,
    ) -> None:
        self._semantic_store = semantic_store
        self._keyword_store = keyword_store
        self._reranker = reranker

    def semantic(self, query: str, scope: KnowledgeScope, k: int) -> list[Ranked]:
        return _to_ranked(self._semantic_store.search(query, scope, k))

    def keyword(self, query: str, scope: KnowledgeScope, k: int) -> list[Ranked]:
        return _to_ranked(self._keyword_store.search(query, scope, k))

    def fuse(self, rankings: list[list[Ranked]], k: int = RRF_K) -> list[Ranked]:
        return fuse(rankings, k=k)

    def rerank(
        self, query: str, candidates: list[Ranked], top_n: int
    ) -> list[RetrievedChunk]:
        """Cross-encode ``candidates`` and return the weight-boosted top-n.

        Each candidate must carry its ``chunk`` (the semantic/keyword legs always
        set it). The reranker scores every candidate; the final ``score`` blends
        the cross-encoder relevance with the chunk-type priority weight so a
        higher-priority chunk wins when relevance is comparable.
        """
        scored = [c for c in candidates if c.chunk is not None]
        if not scored:
            return []

        documents = [c.chunk.content for c in scored if c.chunk is not None]
        results = self._reranker.rerank(query, documents, len(documents))

        reranked: list[RetrievedChunk] = []
        for result in results:
            source = scored[result.index].chunk
            if source is None:  # pragma: no cover - filtered above
                continue
            chunk = source.model_copy()
            chunk.rerank_score = result.score
            chunk.score = result.score * (chunk.weight or 1.0)
            reranked.append(chunk)

        reranked.sort(key=lambda c: c.score, reverse=True)
        return reranked[: max(top_n, 0)]
