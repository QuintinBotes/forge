"""Tests for MCP query-through retrieval (plan Task 1.12; spec: MCP query-through).

Query-through calls the MCP server live at retrieval time and normalises the
returned resources into attributed :class:`~forge_contracts.RetrievedChunk`
objects tagged ``mcp_resource`` (chunk-type weight 1.0x per the spec table).
"""

from __future__ import annotations

from forge_contracts import ChunkType, RetrievedChunk
from forge_mcp import MCPGatewayClient, query_through
from forge_mcp.testing import sample_connection, sample_transport


def _client() -> MCPGatewayClient:
    client = MCPGatewayClient(transport=sample_transport())
    client.connect(sample_connection())
    return client


def test_query_through_returns_attributed_chunks() -> None:
    client = _client()
    chunks = query_through(client, "vault rotation", k=5)
    assert chunks
    assert all(isinstance(c, RetrievedChunk) for c in chunks)
    assert all(c.chunk_type is ChunkType.MCP_RESOURCE for c in chunks)
    # Source attribution: each chunk carries the originating resource URI.
    assert all(c.source_uri for c in chunks)


def test_query_through_respects_k() -> None:
    client = _client()
    chunks = query_through(client, "page", k=1)
    assert len(chunks) <= 1


def test_query_through_only_returns_in_scope_resources() -> None:
    client = _client()
    chunks = query_through(client, "page", k=10)
    # finance is outside the connection's allowed namespaces.
    assert all("finance" not in (c.source_uri or "") for c in chunks)
