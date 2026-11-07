"""Knowledge search response schemas (spec J §2.5, ADR-0018).

The search envelope makes retrieval degradation *visible* instead of silent:
every response names the legs that actually ran (``legs``), whether the vector
leg is configured (``vector_leg``), and the rerank state (``rerank`` —
``"off"`` when no reranker is configured, else the provider name). A fresh
install therefore searches honestly by BM25 alone, and no response ever
implies semantic ranking that did not happen.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from forge_contracts import RetrievedChunk

__all__ = ["KnowledgeSearchResponse"]


class KnowledgeSearchResponse(BaseModel):
    """Envelope for ``POST /knowledge/search``."""

    results: list[RetrievedChunk] = Field(default_factory=list)
    legs: list[str] = Field(default_factory=lambda: ["bm25"])
    vector_leg: str = "unconfigured"
    rerank: str = "off"
