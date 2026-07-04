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

HARD-03 makes :meth:`rerank` **graceful**: if reranking is disabled, or the
reranker degrades (a :class:`~forge_knowledge.reranker.RerankerUnavailableError`,
a budget timeout, or a decorator that returns empty), the retriever falls back to
**weighted RRF** (the fused score x the chunk-type weight), sets ``rerank_score``
to ``None`` so degradation is visible, and never raises. It also records a
:class:`RerankDebug` on :attr:`last_rerank` (provider, latency, ``fallback_used``,
and the **rank delta** vs the fused order) for the FORGE_SPEC "reranker delta"
observability metric and the debug payload.

The orchestration that calls these in sequence lives in
:class:`forge_knowledge.service.KnowledgeService`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from forge_contracts.constants import RRF_K
from forge_contracts.dtos import KnowledgeScope, Ranked, RetrievedChunk
from forge_contracts.protocols import RerankerClient
from forge_knowledge.fusion import fuse
from forge_knowledge.reranker import RerankerUnavailableError, RerankTelemetry

__all__ = ["HybridRetriever", "RerankDebug"]


@dataclass(frozen=True)
class RerankDebug:
    """Server-side rerank telemetry for the debug payload / observability event.

    Carries no raw query and no secret. ``rank_delta_mean`` is the mean absolute
    position shift of the returned top-n vs their pre-rerank fused order;
    ``monotonic`` is true when the reranker preserved the fused order (a
    Kendall-tau concordant result / the fallback path).
    """

    provider: str
    model: str | None
    candidates: int
    latency_ms: float
    fallback_used: bool
    reason: str | None
    rank_delta_mean: float
    monotonic: bool


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
        *,
        rerank_enabled: bool = True,
    ) -> None:
        self._semantic_store = semantic_store
        self._keyword_store = keyword_store
        self._reranker = reranker
        self._rerank_enabled = rerank_enabled
        self._last_rerank: RerankDebug | None = None

    @property
    def last_rerank(self) -> RerankDebug | None:
        """Telemetry for the most recent :meth:`rerank` (redacted; debug payload)."""
        return self._last_rerank

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

        Degradation-safe (HARD-03): a disabled reranker, a
        :class:`RerankerUnavailableError`, a latency-budget timeout, or an empty
        degraded result all fall back to **weighted RRF** (fused score x weight)
        with ``rerank_score=None`` — never an exception.
        """
        scored = [c for c in candidates if c.chunk is not None]
        if not scored:
            self._last_rerank = None
            return []

        if self._reranker is None or not self._rerank_enabled:
            return self._weighted_rrf_fallback(scored, top_n, reason="rerank disabled")

        documents = [c.chunk.content for c in scored if c.chunk is not None]
        try:
            results = self._reranker.rerank(query, documents, len(documents))
        except RerankerUnavailableError as exc:
            # A non-graceful client (or one wired without the decorator) raised;
            # degrade defensively so a search is never failed by the reranker.
            from forge_knowledge.redaction import redact_secrets

            return self._weighted_rrf_fallback(scored, top_n, reason=redact_secrets(str(exc)))

        telemetry: RerankTelemetry | None = getattr(self._reranker, "last_call", None)
        if not results:
            # Empty over a non-empty candidate set == the GracefulReranker
            # signalled degradation. Fall back and surface its redacted reason.
            reason = telemetry.reason if telemetry is not None else "reranker returned no results"
            return self._weighted_rrf_fallback(scored, top_n, reason=reason, telemetry=telemetry)

        reranked: list[tuple[int, RetrievedChunk]] = []
        for result in results:
            source = scored[result.index].chunk
            if source is None:  # pragma: no cover - filtered above
                continue
            chunk = source.model_copy()
            chunk.rerank_score = result.score
            chunk.score = result.score * (chunk.weight or 1.0)
            reranked.append((result.index, chunk))

        reranked.sort(key=lambda pair: pair[1].score, reverse=True)
        top = reranked[: max(top_n, 0)]

        self._last_rerank = self._build_debug(
            [orig for orig, _ in top], len(scored), telemetry, fallback_used=False
        )
        return [chunk for _, chunk in top]

    # ---- fallback + telemetry helpers ------------------------------------- #

    def _weighted_rrf_fallback(
        self,
        scored: list[Ranked],
        top_n: int,
        *,
        reason: str | None,
        telemetry: RerankTelemetry | None = None,
    ) -> list[RetrievedChunk]:
        """Order by weighted RRF (fused score x chunk weight); ``rerank_score=None``.

        The single ordering shared by the disabled path and every degraded path,
        so a search returns a stable, sensible order without the reranker.
        """
        # ``scored`` is pre-filtered to ``c.chunk is not None`` by every caller.
        ordered = sorted(
            scored,
            key=lambda c: c.score * (c.chunk.weight or 1.0 if c.chunk else 1.0),
            reverse=True,
        )
        out: list[RetrievedChunk] = []
        for candidate in ordered[: max(top_n, 0)]:
            source = candidate.chunk
            if source is None:  # pragma: no cover - filtered by caller
                continue
            chunk = source.model_copy()
            chunk.rerank_score = None
            chunk.score = candidate.score * (chunk.weight or 1.0)
            out.append(chunk)

        provider = telemetry.provider if telemetry is not None else getattr(
            self._reranker, "provider", "unknown"
        )
        model = telemetry.model if telemetry is not None else getattr(self._reranker, "model", None)
        latency_ms = telemetry.latency_ms if telemetry is not None else 0.0
        self._last_rerank = RerankDebug(
            provider=provider,
            model=model,
            candidates=len(scored),
            latency_ms=latency_ms,
            fallback_used=True,
            reason=reason,
            rank_delta_mean=0.0,
            monotonic=True,
        )
        return out

    def _build_debug(
        self,
        orig_indices: list[int],
        candidates: int,
        telemetry: RerankTelemetry | None,
        *,
        fallback_used: bool,
    ) -> RerankDebug:
        """Compute the rank delta of the returned top-n vs their fused order."""
        mean_delta, monotonic = _rank_delta(orig_indices)
        provider = telemetry.provider if telemetry is not None else getattr(
            self._reranker, "provider", "unknown"
        )
        model = telemetry.model if telemetry is not None else getattr(self._reranker, "model", None)
        latency_ms = telemetry.latency_ms if telemetry is not None else 0.0
        reason = telemetry.reason if telemetry is not None else None
        return RerankDebug(
            provider=provider,
            model=model,
            candidates=candidates,
            latency_ms=latency_ms,
            fallback_used=fallback_used,
            reason=reason,
            rank_delta_mean=mean_delta,
            monotonic=monotonic,
        )


def _rank_delta(orig_indices: list[int]) -> tuple[float, bool]:
    """Mean |Δposition| of a returned order vs the fused order, + monotonic flag.

    ``orig_indices`` is the pre-rerank (fused) position of each returned item in
    its new order. The fused relative order of the same items is ``sorted(...)``;
    the delta is the mean absolute shift, and ``monotonic`` is true when the new
    order equals the fused order (no reordering / Kendall-tau concordant).
    """
    if not orig_indices:
        return 0.0, True
    fused_rank = {orig: rank for rank, orig in enumerate(sorted(orig_indices))}
    deltas = [abs(new_rank - fused_rank[orig]) for new_rank, orig in enumerate(orig_indices)]
    mean_delta = sum(deltas) / len(deltas)
    monotonic = orig_indices == sorted(orig_indices)
    return mean_delta, monotonic
