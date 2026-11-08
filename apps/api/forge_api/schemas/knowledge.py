"""Knowledge search response schemas (spec J §2.5, ADR-0018).

The search envelope makes retrieval degradation *visible* instead of silent:
every response names the legs that actually ran (``legs``), whether the vector
leg is configured (``vector_leg``), and the rerank state (``rerank`` —
``"off"`` when no reranker is configured, the provider name when this call
was cross-encoded, or ``"fallback"`` when a configured reranker's call
degraded to weighted-RRF order). A fresh
install therefore searches honestly by BM25 alone, and no response ever
implies semantic ranking that did not happen.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from forge_contracts import RetrievedChunk

__all__ = [
    "KnowledgeDocumentResponse",
    "KnowledgePassage",
    "KnowledgeSearchResponse",
    "KnowledgeSyncEnqueued",
]


class KnowledgePassage(RetrievedChunk):
    """A retrieved chunk with its label ``"{source kind}:{source uri}:{path}"`` (ADR-0014)."""

    source: str


class KnowledgeSearchResponse(BaseModel):
    """Envelope for ``POST /knowledge/search``."""

    results: list[KnowledgePassage] = Field(default_factory=list)
    legs: list[str] = Field(default_factory=lambda: ["bm25"])
    vector_leg: str = "unconfigured"
    rerank: str = "off"


class KnowledgeDocumentResponse(BaseModel):
    """Envelope for ``GET /knowledge/document``: one document's chunks in order.

    ``passages`` are whole chunks in document order, each labelled with its
    source (ADR-0014), until the next would pass ``max_tokens``
    (``ceil(len / 4)`` per passage); ``truncated`` says the rest was left out.
    """

    source_id: str
    source: str
    path: str
    passages: list[KnowledgePassage] = Field(default_factory=list)
    tokens: int = 0
    total_chunks: int = 0
    truncated: bool = False


class KnowledgeSyncEnqueued(BaseModel):
    """``POST /knowledge/sync`` of a tracker source: the worker runs it (spec J §2.3)."""

    status: Literal["enqueued"] = "enqueued"
    source_id: str
