"""Unit tests for index-vs-live MCP retrieval routing (AC9)."""

from __future__ import annotations

from collections import Counter

from forge_contracts.dtos import KnowledgeScope, RetrievedChunk
from forge_contracts.enums import ChunkType, MCPIndexStrategy
from forge_knowledge.mcp_retrieval import retrieve_with_mcp


class _FakeSearch:
    """Stands in for KnowledgeService.search over the local hybrid index."""

    def __init__(self, results: list[RetrievedChunk]) -> None:
        self._results = results
        self.calls = 0

    def search(self, query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]:
        self.calls += 1
        return list(self._results)


class _FakeRouter:
    def __init__(self, strategies: dict[str, MCPIndexStrategy]) -> None:
        self._strategies = strategies
        self.strategy_calls: Counter[str] = Counter()
        self.query_through_calls: Counter[str] = Counter()

    def index_strategy(self, slug: str) -> MCPIndexStrategy | None:
        self.strategy_calls[slug] += 1
        return self._strategies.get(slug)

    def query_through(self, slug: str, query: str, k: int) -> list[RetrievedChunk]:
        self.query_through_calls[slug] += 1
        return [
            RetrievedChunk(
                id=f"{slug}-live",
                content="live result",
                chunk_type=ChunkType.MCP_RESOURCE,
                score=99.0,
                source_uri=f"mcp://{slug}/live",
            )
        ]


def _indexed_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        id="indexed",
        content="indexed mcp chunk",
        chunk_type=ChunkType.MCP_RESOURCE,
        score=5.0,
        source_uri="mcp://confluence-engineering/page-1",
    )


def test_sync_and_index_source_makes_zero_live_calls() -> None:
    search = _FakeSearch([_indexed_chunk()])
    router = _FakeRouter({"confluence-engineering": MCPIndexStrategy.SYNC_AND_INDEX})
    scope = KnowledgeScope(mcp_sources=["confluence-engineering"])

    results = retrieve_with_mcp(search, "rotate vault", scope, k=10, router=router)

    assert router.query_through_calls.total() == 0  # no live MCP call
    assert any(c.id == "indexed" for c in results)


def test_query_through_source_makes_exactly_one_live_call() -> None:
    search = _FakeSearch([])
    router = _FakeRouter({"live-confluence": MCPIndexStrategy.QUERY_THROUGH})
    scope = KnowledgeScope(mcp_sources=["live-confluence"])

    results = retrieve_with_mcp(search, "rotate vault", scope, k=10, router=router)

    assert router.query_through_calls["live-confluence"] == 1
    assert results and results[0].id == "live-confluence-live"


def test_mixed_scope_routes_each_source_independently() -> None:
    search = _FakeSearch([_indexed_chunk()])
    router = _FakeRouter(
        {
            "confluence-engineering": MCPIndexStrategy.SYNC_AND_INDEX,
            "live-confluence": MCPIndexStrategy.QUERY_THROUGH,
        }
    )
    scope = KnowledgeScope(mcp_sources=["confluence-engineering", "live-confluence"])

    results = retrieve_with_mcp(search, "q", scope, k=10, router=router)

    assert router.query_through_calls["confluence-engineering"] == 0
    assert router.query_through_calls["live-confluence"] == 1
    # Live result (score 99) outranks the indexed chunk (score 5).
    assert results[0].id == "live-confluence-live"
