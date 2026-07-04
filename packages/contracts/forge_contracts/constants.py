"""Frozen numeric/table constants shared across the retrieval and agent spine.

These mirror the exact values fixed in ``docs/FORGE_SPEC.md`` and the plan's
Global Constraints. They live in contracts so every package (knowledge-core,
agent-runtime, workflow-engine, eval) references one source of truth.
"""

from __future__ import annotations

from forge_contracts.enums import ChunkType

#: Default embedding dimensionality (matches ``forge_db`` ``EMBEDDING_DIM``).
DEFAULT_EMBEDDING_DIM: int = 1536

#: Reciprocal Rank Fusion constant: ``score(d) = Σ 1 / (k + rank_i(d))``.
RRF_K: int = 60

#: Escalation confidence threshold (spec: escalation_policy.confidence_threshold).
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.72

#: Default retry budget (spec: retry_policy.max_retries).
DEFAULT_MAX_RETRIES: int = 3

#: Chunk-type priority weights (spec: Chunk Types and Priority Weights table).
CHUNK_TYPE_WEIGHTS: dict[ChunkType, float] = {
    ChunkType.MARKDOWN: 1.0,
    ChunkType.CODE: 1.0,
    ChunkType.SUMMARY: 1.2,
    ChunkType.README: 1.3,
    ChunkType.POLICY: 1.5,
    ChunkType.SPEC: 1.4,
    ChunkType.MCP_RESOURCE: 1.0,
}


__all__ = [
    "CHUNK_TYPE_WEIGHTS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_MAX_RETRIES",
    "RRF_K",
]
