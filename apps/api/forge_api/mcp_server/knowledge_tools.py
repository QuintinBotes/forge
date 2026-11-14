"""Direct knowledge reads over MCP (spec J §2.4.1, D-e).

One ``assemble_context`` call per question leaves an agent no way to follow
up, so two read-only tools sit next to it. Each calls the REST handler of the
same read (``POST /knowledge/search``, ``GET /knowledge/document``), so MCP and
REST cannot drift: the search envelope's ``legs``/``vector_leg``/``rerank``
come back as-is, both are workspace-scoped, and every passage carries its
source label (ADR-0014).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from forge_api.mcp_server.registry import McpTool, ToolContext
from forge_api.mcp_server.world_tools import _call
from forge_api.routers import knowledge as knowledge_router
from forge_api.routers.knowledge import MAX_DOCUMENT_TOKENS, get_knowledge_service
from forge_api.schemas.knowledge import KnowledgeDocumentResponse, KnowledgeSearchResponse
from forge_contracts import KnowledgeScope
from forge_contracts.enums import KnowledgeSourceKind

#: Most passages one ``knowledge_search`` call may return.
MAX_SEARCH_K = 50


class KnowledgeSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    k: int = Field(default=10, ge=1, le=MAX_SEARCH_K)
    kinds: list[KnowledgeSourceKind] | None = Field(
        default=None, description="Only these source kinds (repo, mcp, document, url)"
    )


class KnowledgeDocumentIn(BaseModel):
    source: str = Field(min_length=1, description="Knowledge source id (or its uri)")
    path: str = Field(min_length=1, description="Document path, as search results show it")
    max_tokens: int = Field(default=8_000, ge=1, le=MAX_DOCUMENT_TOKENS)


def _search(ctx: ToolContext, args: KnowledgeSearchIn) -> KnowledgeSearchResponse:
    request = knowledge_router.SearchRequest(
        query=args.query,
        k=args.k,
        scope=KnowledgeScope(source_types=[kind.value for kind in args.kinds or ()]),
    )
    return _call(
        lambda: knowledge_router.search(
            svc=ctx.provide(get_knowledge_service), principal=ctx.principal, request=request
        )
    )


def _document(ctx: ToolContext, args: KnowledgeDocumentIn) -> KnowledgeDocumentResponse:
    return _call(
        lambda: knowledge_router.document(
            principal=ctx.principal,
            session=ctx.session,
            source=args.source,
            path=args.path,
            max_tokens=args.max_tokens,
        )
    )


KNOWLEDGE_TOOLS: tuple[McpTool, ...] = (
    McpTool(
        "knowledge_search",
        "Hybrid search over this workspace's knowledge index (code, docs, issues, PRs, "
        "commits): up to 50 source-labelled passages with the honest legs/vector_leg/rerank "
        "envelope. Use it to follow up after assemble_context.",
        KnowledgeSearchIn,
        _search,
    ),
    McpTool(
        "knowledge_document",
        "One whole document (a file, issue or PR) from a knowledge source, its chunks in "
        "order, capped at max_tokens (ceil(chars / 4)); `truncated` says the rest was left out.",
        KnowledgeDocumentIn,
        _document,
        # Already capped at max_tokens with whole passages; never cut again.
        max_text_chars=None,
    ),
)

__all__ = ["KNOWLEDGE_TOOLS", "MAX_SEARCH_K"]
