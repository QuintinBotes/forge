"""Knowledge / RAG router (Task 1.3 — knowledge-core).

Serves the hybrid-retrieval spine over HTTP:

* ``POST /knowledge/search`` — the proof-of-spine route: semantic (pgvector) +
  keyword (BM25) -> RRF fusion (k=60) -> cross-encoder rerank -> attributed
  top-k :class:`RetrievedChunk`.
* ``POST /knowledge/index`` — index a batch of chunks into a knowledge source.
* ``POST /knowledge/sync`` — full or incremental (git-diff) source sync
  (Task 1.4): full re-chunks + indexes a tree (pruning vanished files);
  incremental re-indexes only the files a git diff reports as changed. A
  GitHub tracker source (spec J §2.3) is enqueued for the worker instead.
* ``GET /knowledge/document`` — one document's chunks in order, capped at
  ``max_tokens`` (spec J §2.4.1, D-e; the ``knowledge_document`` MCP tool).

Handlers delegate to a :class:`KnowledgeService`. The default service is built
from the API's DB session factory with the *configured* production embedder and
reranker (spec J §2.5, ADR-0018): ``FORGE_EMBEDDING_PROVIDER`` selects the
embedder (unset -> honest BM25-only search whose responses carry
``legs: ["bm25"]`` / ``vector_leg: "unconfigured"``; never the hash embedder)
and ``FORGE_RERANK_PROVIDER`` defaults to ``none`` (``rerank: "off"``). Tests
swap in deterministic clients behind the same dependency via
``app.dependency_overrides``.
"""

from __future__ import annotations

import os
import uuid
from functools import lru_cache, partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api._stubs import NotImplementedResponse
from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import CurrentPrincipal, DbSession, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.knowledge import (
    KnowledgeDocumentResponse,
    KnowledgePassage,
    KnowledgeSearchResponse,
    KnowledgeSyncEnqueued,
)
from forge_api.security.ssrf import assert_safe_url
from forge_api.services.context_service import chunk_label, count_tokens, source_label
from forge_api.settings import Settings, get_settings
from forge_contracts import (
    Chunk,
    IndexResult,
    KnowledgeScope,
)
from forge_contracts import ChunkType as ContractChunkType
from forge_contracts.enums import SyncMode
from forge_contracts.protocols import EmbeddingClient
from forge_db.models import ChunkType, KnowledgeSource, RetrievalChunk
from forge_db.models.enums import KnowledgeSourceKind
from forge_knowledge import (
    KnowledgeService,
    VectorLegUnconfiguredError,
    build_embedding_client_from_settings,
    build_reranker_from_settings,
    full_sync,
    sync_source,
)
from forge_knowledge.sync import commit_limit, embed_backlog, is_tracker_source

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

# RBAC (Phase-2 bug fix r3): ``index`` and ``sync`` mutate the knowledge index,
# so they must authorize WRITE — a read-only ``viewer`` (and the ``agent-runner``,
# which lacks WRITE) is denied (403). ``search`` stays a READ open to any
# authenticated role. The gate yields the principal so the handler can still scope
# the source to the caller's workspace.
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]

#: Largest ``max_tokens`` one ``GET /knowledge/document`` may ask for.
MAX_DOCUMENT_TOKENS = 64_000

#: The worker's ``forge_worker.syncer.sync_source_task`` — by name, because
#: ``forge-api`` cannot import ``forge-worker`` (mirrors ``FULL_SYNC_TASK``).
SYNC_SOURCE_TASK = "forge.knowledge.sync_source"


def enqueue_source_sync(source_id: str) -> None:  # pragma: no cover - prod seam
    """Enqueue the worker sync of one source (monkeypatched in tests)."""
    from forge_api.services.mcp_index_service import _celery_app

    _celery_app().send_task(SYNC_SOURCE_TASK, args=[source_id])  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Service dependency (overridable for tests / BYOK swap)                       #
# --------------------------------------------------------------------------- #


def _resolve_reranker_key(settings: Settings) -> str | None:
    """Resolve the BYOK reranker key for the configured provider (never logged).

    Read on demand from the process env (the integration lane's
    ``JINA_API_KEY`` / ``COHERE_API_KEY``) and handed straight to the client;
    the per-workspace encrypted vault (``SecretVault.get_secret``,
    ``APIKeyKind.MODEL_PROVIDER``) is the prod resolution behind this same seam.
    Returns ``None`` for the offline fixture / self-hosted providers so the
    default lane never requires a credential.
    """
    provider = (settings.rerank_provider or "none").strip().lower()
    if provider == "jina":
        return os.environ.get("JINA_API_KEY")
    if provider == "cohere":
        return os.environ.get("COHERE_API_KEY")
    return None


@lru_cache(maxsize=1)
def get_configured_embedder() -> EmbeddingClient | None:
    """Build (once) the configured production embedder, or ``None``.

    Split out of the service singleton so the API lifespan can warm it at
    startup: a bad embedder config (unknown provider, missing/mismatched
    dimension, unloadable local model) must refuse the *process* at boot
    (ADR-0018), not 500 every ``/knowledge/*`` call while health stays 200.
    Warming only the embedder avoids building the DB engine at boot. The
    base_url goes through the api's SSRF guard seam; the key is read from
    the env on demand, never stored on settings.
    """
    settings = get_settings()
    return build_embedding_client_from_settings(
        settings,
        api_key=os.environ.get("FORGE_EMBEDDING_API_KEY"),
        url_validator=partial(
            assert_safe_url,
            allow_private=settings.ssrf_allow_private,
            allowlist=settings.outbound_allowlist,
        ),
    )


@lru_cache(maxsize=1)
def _knowledge_service_singleton() -> KnowledgeService:
    settings = get_settings()
    # ``build_reranker_from_settings`` returns None by default
    # (FORGE_RERANK_PROVIDER=none -> rerank "off"), and only builds a budgeted,
    # SSRF-guarded live client when an operator opts in. The BYOK key is resolved
    # on demand and never stored on the settings object.
    reranker = build_reranker_from_settings(settings, api_key=_resolve_reranker_key(settings))
    return KnowledgeService.from_session_factory(
        get_session_factory(),
        get_configured_embedder(),
        reranker,
        rerank_candidates=settings.rerank_candidates,
        rerank_enabled=settings.rerank_enabled,
        rerank_budget_ms=settings.rerank_timeout_ms,
    )


def get_knowledge_service() -> KnowledgeService:
    """Return the process-wide knowledge service (override in tests via DI)."""
    return _knowledge_service_singleton()


def get_knowledge_session_factory() -> sessionmaker[Session]:
    """Session factory used to verify a source belongs to the caller's workspace.

    Separate (overridable) dependency so the tenant check reads the same database
    the :class:`KnowledgeService` writes to, including in-memory test backends.
    """
    return get_session_factory()


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]
KnowledgeSessionFactoryDep = Annotated[
    sessionmaker[Session], Depends(get_knowledge_session_factory)
]


def _assert_source_in_workspace(
    factory: sessionmaker[Session], source_id: str, workspace_id: uuid.UUID
) -> KnowledgeSource:
    """Guard cross-tenant writes: a source must belong to the caller's workspace.

    Raises 404 (not 403) for a missing *or* foreign source so the endpoint never
    leaks the existence of another tenant's knowledge source.
    """
    try:
        source_uuid = uuid.UUID(str(source_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="knowledge source not found"
        ) from exc
    with factory() as session:
        source = session.get(KnowledgeSource, source_uuid)
        if source is None or source.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="knowledge source not found",
            )
        return source


# --------------------------------------------------------------------------- #
# Request bodies                                                              #
# --------------------------------------------------------------------------- #


class SearchRequest(BaseModel):
    """Body for ``POST /knowledge/search``."""

    query: str
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    k: int = 10
    # Strict vector requirement (ADR-0018): when true, a search with no
    # configured embedder refuses with 503 instead of answering BM25-only.
    require_vector: bool = False


class IndexRequest(BaseModel):
    """Body for ``POST /knowledge/index``."""

    source_id: str
    chunks: list[Chunk] = Field(default_factory=list)


class SyncRequest(BaseModel):
    """Body for ``POST /knowledge/sync``.

    Full sync accepts either inline ``files`` (``{path: content}``) or a
    checked-out ``root`` directory. Incremental sync requires a ``root`` plus a
    ``base_ref`` to diff against (and an optional ``head_ref``).
    """

    source_id: str
    mode: SyncMode = SyncMode.FULL
    files: dict[str, str] | None = None
    root: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    prune: bool = True


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.post("/search", response_model=KnowledgeSearchResponse)
def search(
    svc: KnowledgeServiceDep, principal: CurrentPrincipal, request: SearchRequest
) -> KnowledgeSearchResponse:
    # Per-workspace isolation (mandatory): the caller may only search their own
    # workspace. Any ``workspace_id`` in the request scope is overridden so a
    # client cannot read another tenant's knowledge, and a default (empty) scope
    # never spans all workspaces.
    scope = request.scope.model_copy(update={"workspace_id": principal.workspace_id})
    if request.require_vector and not svc.vector_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "vector search is unconfigured (set FORGE_EMBEDDING_PROVIDER); "
                "BM25-only search remains available without require_vector"
            ),
        )
    try:
        results = svc.search(request.query, scope, k=request.k)
    except VectorLegUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    # The envelope names what *this call* did: a configured reranker whose
    # call degraded to weighted-RRF (every rerank_score None) reports
    # "fallback", not the provider name. Derived from this call's own
    # results — never svc.last_rerank, which a concurrent request on the
    # shared singleton can overwrite.
    rerank = svc.rerank_status
    if rerank != "off" and results and all(c.rerank_score is None for c in results):
        rerank = "fallback"
    return KnowledgeSearchResponse(
        results=[
            KnowledgePassage(**chunk.model_dump(), source=chunk_label(chunk)) for chunk in results
        ],
        legs=svc.legs,
        vector_leg=svc.vector_leg,
        rerank=rerank,
    )


@router.post("/index", response_model=IndexResult)
def index(
    svc: KnowledgeServiceDep,
    factory: KnowledgeSessionFactoryDep,
    principal: WriterDep,
    request: IndexRequest,
) -> IndexResult:
    _assert_source_in_workspace(factory, request.source_id, principal.workspace_id)
    result = svc.index(request.source_id, request.chunks)
    embedded, errors = embed_backlog(svc, request.source_id)
    return result.model_copy(
        update={"updated": result.updated + embedded, "errors": [*result.errors, *errors]}
    )


@router.post("/sync", response_model=IndexResult | KnowledgeSyncEnqueued)
def sync(
    svc: KnowledgeServiceDep,
    factory: KnowledgeSessionFactoryDep,
    principal: WriterDep,
    request: SyncRequest,
    response: Response,
) -> IndexResult | KnowledgeSyncEnqueued:
    """Full or incremental (git-diff) sync of a knowledge source.

    A GitHub tracker source reads GitHub through the workspace's connection,
    so it is enqueued for the worker (202) rather than synced in the request.
    """
    source = _assert_source_in_workspace(factory, request.source_id, principal.workspace_id)
    if is_tracker_source(source):
        enqueue_source_sync(str(source.id))
        response.status_code = status.HTTP_202_ACCEPTED
        return KnowledgeSyncEnqueued(source_id=str(source.id))
    try:
        max_commits = commit_limit(source.config)
        if request.mode == SyncMode.INCREMENTAL:
            if not request.root or not request.base_ref:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="incremental sync requires 'root' and 'base_ref'",
                )
            return sync_source(
                svc,
                request.source_id,
                request.root,
                mode=SyncMode.INCREMENTAL,
                base_ref=request.base_ref,
                head_ref=request.head_ref,
                max_commits=max_commits,
            )

        if request.files is not None:
            return full_sync(svc, request.source_id, request.files, prune=request.prune)
        if request.root is not None:
            return sync_source(
                svc,
                request.source_id,
                request.root,
                mode=SyncMode.FULL,
                prune=request.prune,
                max_commits=max_commits,
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="full sync requires either 'files' or 'root'",
        )
    except ValueError as exc:  # bad ref / unsupported mode surfaced as 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def _workspace_source(session: Session, workspace_id: uuid.UUID, source: str) -> KnowledgeSource:
    """The caller's source by id, uri, or a passage label's ``kind:uri``; 404 for a
    missing *or* foreign one."""
    kind, _, uri = source.partition(":")
    try:
        match = KnowledgeSource.id == uuid.UUID(source)
    except ValueError:
        if kind in {k.value for k in KnowledgeSourceKind} and uri:
            match = and_(
                KnowledgeSource.kind == KnowledgeSourceKind(kind), KnowledgeSource.uri == uri
            )
        else:
            match = KnowledgeSource.uri == source
    found = session.scalars(
        select(KnowledgeSource)
        .where(KnowledgeSource.workspace_id == workspace_id, match)
        .order_by(KnowledgeSource.id)
        .limit(2)
    ).all()
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="knowledge source not found"
        )
    if len(found) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="several knowledge sources have this uri; pass the source id",
        )
    return found[0]


@router.get("/document", response_model=KnowledgeDocumentResponse)
def document(
    principal: ReaderDep,
    session: DbSession,
    source: Annotated[str, Query(min_length=1, description="Knowledge source id or uri")],
    path: Annotated[str, Query(min_length=1, description="Document path in that source")],
    max_tokens: Annotated[int, Query(ge=1, le=MAX_DOCUMENT_TOKENS)] = 8_000,
) -> KnowledgeDocumentResponse:
    """A whole document's chunks in order, capped at ``max_tokens`` (READ).

    Whole chunks only, never a cut one; ``truncated`` says the rest was left
    out. Symbol summaries are index entries, not document text, so they are
    skipped. Workspace-scoped: another workspace's source is not found.
    """
    found = _workspace_source(session, principal.workspace_id, source)
    rows = session.scalars(
        select(RetrievalChunk)
        .where(
            RetrievalChunk.knowledge_source_id == found.id,
            RetrievalChunk.workspace_id == principal.workspace_id,
            RetrievalChunk.path == path,
            RetrievalChunk.chunk_type != ChunkType.SUMMARY,
        )
        .order_by(
            RetrievalChunk.start_line.asc().nulls_last(),
            RetrievalChunk.end_line,
            RetrievalChunk.content_hash,
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    passages: list[KnowledgePassage] = []
    tokens = 0
    for row in rows:
        cost = count_tokens(row.content)
        if tokens + cost > max_tokens:
            break
        tokens += cost
        passages.append(
            KnowledgePassage(
                id=str(row.id),
                content=row.content,
                chunk_type=ContractChunkType(row.chunk_type.value),
                path=row.path,
                start_line=row.start_line,
                end_line=row.end_line,
                weight=row.weight,
                source_id=str(found.id),
                source_uri=found.uri,
                metadata={**(row.chunk_metadata or {}), "source_kind": found.kind.value},
                source=source_label(found.kind.value, found.uri, row.path),
            )
        )
    return KnowledgeDocumentResponse(
        source_id=str(found.id),
        source=found.uri,
        path=path,
        passages=passages,
        tokens=tokens,
        total_chunks=len(rows),
        truncated=len(passages) < len(rows),
    )
