"""Retrieval-latency micro-benchmark (HARD-11, perf-gated).

Measures the hybrid retrieval pipeline's per-query latency and publishes
``p50/p95/p99`` for the ``embed`` and ``total`` stages (the two boundaries that
can be timed without instrumenting ``KnowledgeService`` internals; the finer
``semantic``/``keyword``/``fusion``/``rerank`` split needs in-pipeline timing
hooks and is a documented follow-up — see ``docs/self-hosting/performance.md``).

The measurement logic is pure and hermetic: it drives the *real*
:class:`~forge_knowledge.KnowledgeService` (over in-memory SQLite by default), so
the harness is CI-verifiable at a tiny corpus. A production/runner run swaps in a
learned ``sentence-transformers`` embedder + live pgvector (HARD-03b / HARD-01)
for honest absolute numbers; those numbers are then compared against
``deploy/load/budgets.toml``.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from forge_contracts.dtos import KnowledgeScope
    from forge_knowledge import KnowledgeService

__all__ = [
    "LatencyReport",
    "Percentiles",
    "measure_retrieval_latency",
    "percentiles",
    "write_report",
]


class Percentiles(BaseModel):
    """Latency percentiles for one stage, in milliseconds."""

    p50: float
    p95: float
    p99: float
    samples: int


class LatencyReport(BaseModel):
    """Per-stage latency percentiles at a given corpus size."""

    corpus_size: int
    queries: int
    embedder: str = "deterministic"
    stages: dict[str, Percentiles] = Field(default_factory=dict)


def percentiles(samples_ms: Sequence[float]) -> Percentiles:
    """Compute p50/p95/p99 (nearest-rank) over ``samples_ms`` in milliseconds."""
    if not samples_ms:
        raise ValueError("cannot compute percentiles over an empty sample set")
    ordered = sorted(samples_ms)
    return Percentiles(
        p50=_nearest_rank(ordered, 50),
        p95=_nearest_rank(ordered, 95),
        p99=_nearest_rank(ordered, 99),
        samples=len(ordered),
    )


def _nearest_rank(ordered: list[float], pct: int) -> float:
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 4)


def measure_retrieval_latency(
    *,
    service: KnowledgeService,
    scope: KnowledgeScope,
    queries: Sequence[str],
    corpus_size: int,
    embedder: object | None = None,
    k: int = 10,
    embedder_name: str = "deterministic",
) -> LatencyReport:
    """Drive ``queries`` through ``service`` and report per-stage percentiles.

    Times the full search per query, plus the standalone embed call when an
    ``embedder`` (anything exposing ``embed(list[str])``) is supplied — the same
    embedder the service's retriever uses, passed explicitly so the embed leg can
    be isolated. ``embedder_name`` labels the report (e.g. ``sentence-transformers``
    on a runner, ``deterministic`` in CI).
    """
    embed = getattr(embedder, "embed", None) if embedder is not None else None
    embed_ms: list[float] = []
    total_ms: list[float] = []
    for query in queries:
        if callable(embed):
            t0 = time.perf_counter()
            embed([query])
            embed_ms.append((time.perf_counter() - t0) * 1000.0)

        t1 = time.perf_counter()
        service.search(query, scope, k=k)
        total_ms.append((time.perf_counter() - t1) * 1000.0)

    stages = {"total": percentiles(total_ms)}
    if embed_ms:
        stages["embed"] = percentiles(embed_ms)
    return LatencyReport(
        corpus_size=corpus_size,
        queries=len(queries),
        embedder=embedder_name,
        stages=stages,
    )


def write_report(report: LatencyReport, path: str | Path) -> Path:
    """Write ``report`` as JSON to ``path`` (creating parent dirs)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.model_dump(), indent=2), encoding="utf-8")
    return out
