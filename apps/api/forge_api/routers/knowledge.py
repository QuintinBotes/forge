"""Knowledge / RAG router (Task 1.3 — knowledge-core).

Serves the hybrid-retrieval spine over HTTP:

* ``POST /knowledge/search`` — the proof-of-spine route: semantic (pgvector) +
  keyword (BM25) -> RRF fusion (k=60) -> cross-encoder rerank -> attributed
  top-k :class:`RetrievedChunk`.
* ``POST /knowledge/index`` — index a batch of chunks into a knowledge source.
* ``POST /knowledge/sync`` — full or incremental (git-diff) source sync
  (Task 1.4): full re-chunks + indexes a tree (pruning vanished files);
  incremental re-indexes only the files a git diff reports as changed.

Handlers delegate to a :class:`KnowledgeService`. The default service is built
from the API's DB session factory with the offline-safe deterministic embedding
client and the fixture reranker (a real BYOK embedding client / Jina reranker is
swapped in behind the same dependency via ``app.dependency_overrides`` / config).
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from forge_api._stubs import NotImplementedResponse
from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import CurrentPrincipal, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_contracts import (
    Chunk,
    IndexResult,
    KnowledgeScope,
    RetrievedChunk,
)
from forge_contracts.enums import SyncMode
from forge_db.models import KnowledgeSource
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
    full_sync,
    sync_source,
)

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


# --------------------------------------------------------------------------- #
# Service dependency (overridable for tests / BYOK swap)                       #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _knowledge_service_singleton() -> KnowledgeService:
    return KnowledgeService.from_session_factory(
        get_session_factory(),
        DeterministicEmbeddingClient(),
        FixtureRerankerClient(),
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
) -> None:
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


# --------------------------------------------------------------------------- #
# Request bodies                                                              #
# --------------------------------------------------------------------------- #


class SearchRequest(BaseModel):
    """Body for ``POST /knowledge/search``."""

    query: str
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    k: int = 10


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


@router.post("/search", response_model=list[RetrievedChunk])
def search(
    svc: KnowledgeServiceDep, principal: CurrentPrincipal, request: SearchRequest
) -> list[RetrievedChunk]:
    # Per-workspace isolation (mandatory): the caller may only search their own
    # workspace. Any ``workspace_id`` in the request scope is overridden so a
    # client cannot read another tenant's knowledge, and a default (empty) scope
    # never spans all workspaces.
    scope = request.scope.model_copy(update={"workspace_id": principal.workspace_id})
    return svc.search(request.query, scope, k=request.k)


@router.post("/index", response_model=IndexResult)
def index(
    svc: KnowledgeServiceDep,
    factory: KnowledgeSessionFactoryDep,
    principal: WriterDep,
    request: IndexRequest,
) -> IndexResult:
    _assert_source_in_workspace(factory, request.source_id, principal.workspace_id)
    return svc.index(request.source_id, request.chunks)


@router.post("/sync", response_model=IndexResult)
def sync(
    svc: KnowledgeServiceDep,
    factory: KnowledgeSessionFactoryDep,
    principal: WriterDep,
    request: SyncRequest,
) -> IndexResult:
    """Full or incremental (git-diff) sync of a knowledge source."""
    _assert_source_in_workspace(factory, request.source_id, principal.workspace_id)
    try:
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
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="full sync requires either 'files' or 'root'",
        )
    except ValueError as exc:  # bad ref / unsupported mode surfaced as 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
