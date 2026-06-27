"""F20 MCP resource fetcher over the F09 read-only gateway.

``GatewayMcpResourceFetcher`` adapts :class:`forge_mcp.MCPGatewayClient` to the
:class:`forge_knowledge.mcp_indexer.McpResourceFetcher` protocol. Every
``list_resources`` / ``read_resource`` flows through the gateway client, so F09's
namespace scoping, secret redaction, and per-call audit are inherited unchanged —
F20 never speaks MCP directly and only ever issues those two read operations
(MCP Security Rule 1: read-only).

The foundation gateway client returns the full in-scope resource list (no
server-cursor surface yet), so this adapter reports a single complete page
(``cursor=None``) — which the indexer treats as an exhausted enumeration, so the
tombstone sweep runs correctly.
"""

from __future__ import annotations

from typing import Any

from forge_contracts import MCPResource
from forge_knowledge.mcp_chunking import McpResourceSnapshot
from forge_knowledge.mcp_indexer import ResourceRef
from forge_mcp.client import MCPGatewayClient
from forge_mcp.security import namespace_of

__all__ = ["GatewayMcpResourceFetcher"]

_TOKEN_KEYS = ("change_token", "etag", "lastModified", "last_modified", "revision", "version")


def _change_token(resource: MCPResource) -> str | None:
    meta: dict[str, Any] = resource.metadata or {}
    for key in _TOKEN_KEYS:
        value = meta.get(key)
        if value is not None:
            return str(value)
    return None


class GatewayMcpResourceFetcher:
    """Read-only ``McpResourceFetcher`` over an audited F09 gateway client."""

    def __init__(self, client: MCPGatewayClient, *, connection_slug: str) -> None:
        self._client = client
        self._slug = connection_slug

    def list_resources(
        self, *, namespaces: list[str] | None, cursor: str | None
    ) -> tuple[list[ResourceRef], str | None]:
        # The gateway client filters to the connection's allowed namespaces; we
        # pass ``namespace=None`` to enumerate every in-scope resource at once.
        resources = self._client.list_resources(namespace=None)
        refs = [
            ResourceRef(
                uri=r.uri,
                namespace=r.namespace or namespace_of(r.uri),
                title=r.name,
                mime_type=r.mime_type,
                change_token=_change_token(r),
                url=(r.metadata or {}).get("url"),
            )
            for r in resources
        ]
        return refs, None

    def read_resource(self, uri: str) -> McpResourceSnapshot:
        content = self._client.read_resource(uri)  # already redacted by the gateway
        meta = content.metadata or {}
        return McpResourceSnapshot(
            uri=uri,
            content=content.content,
            connection_slug=self._slug,
            mime_type=content.mime_type,
            namespace=namespace_of(uri),
            title=meta.get("title"),
            url=meta.get("url"),
            change_token=meta.get("change_token"),
        )
