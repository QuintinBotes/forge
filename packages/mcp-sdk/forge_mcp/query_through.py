"""MCP query-through retrieval (plan Task 1.12; spec: "MCP query-through").

Calls the MCP server live at retrieval time (via the client's transport),
ranking in-scope resources against the query and normalising the top matches
into attributed :class:`~forge_contracts.RetrievedChunk` objects tagged
``mcp_resource``. This is the retrieval path for sources configured with
``index_strategy: query_through`` (always-fresh external data).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from forge_contracts import CHUNK_TYPE_WEIGHTS, ChunkType, MCPResource, RetrievedChunk

if TYPE_CHECKING:
    from forge_mcp.client import MCPGatewayClient

_MCP_WEIGHT = CHUNK_TYPE_WEIGHTS[ChunkType.MCP_RESOURCE]


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _score(query_tokens: set[str], resource: MCPResource) -> int:
    haystack = " ".join(
        filter(None, [resource.name, resource.uri, resource.namespace])
    )
    return len(query_tokens & _tokens(haystack))


def query_through(
    client: MCPGatewayClient, query: str, k: int = 10, *, namespace: str | None = None
) -> list[RetrievedChunk]:
    """Live-query the MCP source and return the top-``k`` attributed chunks."""
    resources = client.list_resources(namespace=namespace)
    query_tokens = _tokens(query)

    # Stable ordering: score desc, then original listing order for ties.
    ranked = sorted(
        enumerate(resources),
        key=lambda pair: (-_score(query_tokens, pair[1]), pair[0]),
    )
    top = [resource for _, resource in ranked[: max(k, 0)]]

    source_id = client.connection.id if client.connection else None
    chunks: list[RetrievedChunk] = []
    for rank, resource in enumerate(top):
        content = client.read_resource(resource.uri)
        chunks.append(
            RetrievedChunk(
                id=resource.uri,
                content=content.content,
                chunk_type=ChunkType.MCP_RESOURCE,
                path=resource.uri,
                score=float(len(top) - rank),
                weight=_MCP_WEIGHT,
                source_id=source_id,
                source_uri=resource.uri,
                metadata={"namespace": resource.namespace, "name": resource.name},
            )
        )
    return chunks


__all__ = ["query_through"]
