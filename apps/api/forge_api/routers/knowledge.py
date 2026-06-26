"""Knowledge / RAG router stubs (filled by Tasks 1.1-1.4 — knowledge-core).

The hybrid search endpoint is the proof-of-spine route: semantic + keyword ->
RRF -> rerank -> attributed top-k.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import IndexResult, RetrievedChunk

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "knowledge"


@router.post(
    "/search",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(RetrievedChunk, "Hybrid search with source attribution."),
)
def search(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "search")


@router.post(
    "/index",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(IndexResult, "Index chunks into the knowledge store."),
)
def index(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "index")


@router.post(
    "/sync",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(IndexResult, "Full or incremental source sync."),
)
def sync(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "sync")
