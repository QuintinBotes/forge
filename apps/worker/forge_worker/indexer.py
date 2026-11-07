"""Knowledge indexer task (plan Task 1.3 — RAG spine, background half).

The indexer chunks a knowledge source's files and writes them into the hybrid
retrieval store, so ``/knowledge/search`` can serve them. The logic is split so
it is fully unit-testable without Celery or a live database:

* :func:`chunk_files` — pure: route each file through ``forge_knowledge.chunk_file``
  (Python -> AST chunks, everything else -> markdown/paragraph chunks).
* :func:`index_source` — pure: chunk ``files`` and index them via any
  :class:`~forge_contracts.protocols.KnowledgeStore` (idempotent by content hash).
* :func:`index_source_task` — the thin Celery task that builds the default
  service from configuration and delegates to :func:`index_source`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge_contracts import Chunk, IndexResult
from forge_contracts.protocols import KnowledgeStore
from forge_knowledge import (
    KnowledgeService,
    build_embedding_client_from_settings,
    build_reranker_from_settings,
    chunk_file,
)
from forge_worker.celery_app import celery_app
from forge_worker.reliability import ForgeTask

__all__ = [
    "build_knowledge_service",
    "chunk_files",
    "index_source",
    "index_source_task",
]


def chunk_files(files: Mapping[str, str]) -> list[Chunk]:
    """Chunk ``{path: source}`` into a flat list of :class:`Chunk`."""
    chunks: list[Chunk] = []
    for path, source in files.items():
        chunks.extend(chunk_file(path, source))
    return chunks


def index_source(store: KnowledgeStore, source_id: str, files: Mapping[str, str]) -> IndexResult:
    """Chunk ``files`` and index them into ``store`` for ``source_id``."""
    return store.index(source_id, chunk_files(files))


def build_knowledge_service() -> KnowledgeService:
    """Build the default knowledge service from configuration.

    Uses the *configured* production embedder (spec J §2.5, ADR-0018):
    ``FORGE_EMBEDDING_PROVIDER`` unset means no embedder, so indexing stores
    chunks without embeddings for honest BM25-only search — never the hash
    embedder. The reranker is resolved by the shared
    :func:`build_reranker_from_settings` factory (HARD-03) so a worker-side
    ``search`` uses the same budgeted, SSRF-guarded live reranker the API does
    when ``FORGE_RERANK_PROVIDER`` is set — and no reranker (``"off"``)
    otherwise. Reranking is query-time only, so indexing throughput is
    unaffected.
    """
    import os
    from functools import partial

    from forge_api.security.ssrf import assert_safe_url
    from forge_api.settings import get_settings
    from forge_db import create_session_factory

    settings = get_settings()
    provider = (settings.rerank_provider or "none").strip().lower()
    api_key = None
    if provider == "jina":
        api_key = os.environ.get("JINA_API_KEY")
    elif provider == "cohere":
        api_key = os.environ.get("COHERE_API_KEY")

    embedder = build_embedding_client_from_settings(
        settings,
        api_key=os.environ.get("FORGE_EMBEDDING_API_KEY"),
        url_validator=partial(
            assert_safe_url,
            allow_private=settings.ssrf_allow_private,
            allowlist=settings.outbound_allowlist,
        ),
    )

    return KnowledgeService.from_session_factory(
        create_session_factory(),
        embedder,
        build_reranker_from_settings(settings, api_key=api_key),
        rerank_candidates=settings.rerank_candidates,
        rerank_enabled=settings.rerank_enabled,
        rerank_budget_ms=settings.rerank_timeout_ms,
    )


@celery_app.task(bind=True, base=ForgeTask, name="forge.knowledge.index_source")
def index_source_task(
    self: ForgeTask,
    source_id: str,
    files: dict[str, str],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Celery entrypoint: index ``files`` for ``source_id`` and return the result.

    Indexing is already content-hash idempotent at the store; the optional
    ``idempotency_key`` adds *enqueue-level* dedup so a re-delivered message does
    not re-chunk/re-embed the same payload.
    """
    if self.is_duplicate(idempotency_key):
        return {"deduplicated": True, "idempotency_key": idempotency_key}
    service = build_knowledge_service()
    return index_source(service, source_id, files).model_dump()
