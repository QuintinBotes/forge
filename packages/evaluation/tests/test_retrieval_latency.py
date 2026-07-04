"""HARD-11: retrieval-latency micro-bench (harness hermetic; budgeted run gated)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from forge_eval.perf.retrieval_latency import (
    LatencyReport,
    Percentiles,
    measure_retrieval_latency,
    percentiles,
    write_report,
)
from forge_eval.retrieval_eval import build_indexed_service

_BUDGETS = Path(__file__).resolve().parents[3] / "deploy" / "load" / "budgets.toml"


# --------------------------------------------------------------------------- #
# Pure percentile math (hermetic, always runs)                                 #
# --------------------------------------------------------------------------- #


def test_percentiles_nearest_rank() -> None:
    p = percentiles([float(i) for i in range(1, 101)])  # 1..100
    assert p.p50 == 50.0
    assert p.p95 == 95.0
    assert p.p99 == 99.0
    assert p.samples == 100


def test_percentiles_single_sample() -> None:
    p = percentiles([7.0])
    assert p.p50 == p.p95 == p.p99 == 7.0


def test_percentiles_empty_raises() -> None:
    with pytest.raises(ValueError):
        percentiles([])


# --------------------------------------------------------------------------- #
# Harness over the real pipeline (hermetic SQLite, small corpus)               #
# --------------------------------------------------------------------------- #


def test_measure_retrieval_latency_reports_stages() -> None:
    service, scope = build_indexed_service()
    report = measure_retrieval_latency(
        service=service,
        scope=scope,
        queries=["server", "config", "database"],
        corpus_size=3,
    )
    assert isinstance(report, LatencyReport)
    assert "total" in report.stages
    total = report.stages["total"]
    assert isinstance(total, Percentiles)
    assert total.p50 >= 0 and total.p95 >= total.p50 <= total.p99
    assert report.queries == 3


def test_measure_retrieval_latency_with_embedder_stage() -> None:
    from forge_knowledge import DeterministicEmbeddingClient

    service, scope = build_indexed_service()
    report = measure_retrieval_latency(
        service=service,
        scope=scope,
        queries=["server", "config"],
        corpus_size=2,
        embedder=DeterministicEmbeddingClient(),
        embedder_name="deterministic",
    )
    assert "embed" in report.stages
    assert report.embedder == "deterministic"


def test_write_report_roundtrip(tmp_path: Path) -> None:
    report = LatencyReport(
        corpus_size=1,
        queries=1,
        stages={"total": Percentiles(p50=1.0, p95=2.0, p99=3.0, samples=1)},
    )
    out = write_report(report, tmp_path / "r.json")
    assert out.is_file()
    import json

    loaded = json.loads(out.read_text())
    assert loaded["corpus_size"] == 1
    assert loaded["stages"]["total"]["p95"] == 2.0


# --------------------------------------------------------------------------- #
# Budgeted run — gated (runner + FORGE_RUN_PERF=1)                             #
# --------------------------------------------------------------------------- #


@pytest.mark.perf
def test_retrieval_latency_within_budget() -> None:
    if not os.environ.get("FORGE_RUN_PERF"):
        pytest.skip(
            "PARKED: set FORGE_RUN_PERF=1 on a resourced runner (ideally with a "
            "learned embedder + live pgvector) to run the budgeted latency bench; "
            "see docs/self-hosting/performance.md."
        )
    corpus_size = int(os.environ.get("FORGE_PERF_CORPUS_SIZE", "200"))
    service, scope = build_indexed_service()
    queries = ["server", "config", "database", "auth", "handler"] * 10
    report = measure_retrieval_latency(
        service=service, scope=scope, queries=queries, corpus_size=corpus_size
    )
    write_report(
        report,
        Path(__file__).resolve().parents[3]
        / "deploy"
        / "load"
        / "reports"
        / "retrieval_latency.json",
    )
    budgets = tomllib.loads(_BUDGETS.read_text())
    p95_budget_ms = budgets["retrieval"]["total_p95_ms"]
    assert report.stages["total"].p95 <= p95_budget_ms, (
        f"p95 {report.stages['total'].p95}ms exceeds budget {p95_budget_ms}ms"
    )
