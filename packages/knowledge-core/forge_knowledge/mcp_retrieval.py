"""Index-vs-live retrieval routing for MCP sources (F20, AC9).

The foundation's :class:`~forge_knowledge.service.KnowledgeService` hybrid search
is served entirely from the local ``retrieval_chunk`` index — so an
``index_strategy = sync_and_index`` source (whose chunks F20 has persisted) is
already retrieved fast with **zero** live MCP calls. :func:`retrieve_with_mcp`
adds the complementary half: for any ``query_through`` source in the scope it
makes exactly one live gateway call and merges those always-fresh chunks into the
ranked result. The routing decision is made per source by its ``index_strategy``.
"""

from __future__ import annotations

from typing import Protocol

from forge_contracts.dtos import KnowledgeScope, RetrievedChunk
from forge_contracts.enums import MCPIndexStrategy

__all__ = ["McpRetrievalRouter", "retrieve_with_mcp"]


class _Searchable(Protocol):
    def search(self, query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]: ...


class McpRetrievalRouter(Protocol):
    """Resolves a connection slug to its strategy and (live) query-through chunks."""

    def index_strategy(self, slug: str) -> MCPIndexStrategy | None: ...

    def query_through(self, slug: str, query: str, k: int) -> list[RetrievedChunk]: ...


def retrieve_with_mcp(
    service: _Searchable,
    query: str,
    scope: KnowledgeScope,
    k: int = 10,
    *,
    router: McpRetrievalRouter | None = None,
) -> list[RetrievedChunk]:
    """Hybrid-search the local index, then merge live query-through MCP sources.

    ``sync_and_index`` sources are served purely from the local index (no live
    call); only ``query_through`` sources trigger a live gateway call — exactly
    one per such source in the scope.
    """
    results = list(service.search(query, scope, k))

    if router is not None:
        for slug in scope.mcp_sources:
            if router.index_strategy(slug) is MCPIndexStrategy.QUERY_THROUGH:
                results.extend(router.query_through(slug, query, k))

    results.sort(key=lambda chunk: chunk.score, reverse=True)
    return results[:k] if k >= 0 else results
