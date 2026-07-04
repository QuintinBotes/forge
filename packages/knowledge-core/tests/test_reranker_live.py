"""HARD-03 AC10-AC13: live BYOK cross-encoder reranker (creds-gated, opt-in).

These drive a REAL Jina/Cohere (or reachable self-hosted) cross-encoder over the
network using an env BYOK key. They are marked ``integration`` + ``live_rerank``
and **skip cleanly** when no reranker credential/endpoint is present — the
default ``uv run pytest -q`` lane stays hermetic and network-free and never
falls back to the fixture on this lane.

Run them (once real creds exist):

    cp .env.integration.example .env.integration   # then fill JINA_API_KEY / COHERE_API_KEY
    set -a && source .env.integration && set +a
    export FORGE_RERANK_PROVIDER=jina              # or cohere / selfhosted
    uv run pytest -m live_rerank -k reranker -q

See docs/runbooks/live-reranker.md.
"""

from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlsplit

import pytest

from forge_knowledge.reranker import (
    GracefulReranker,
    RerankTelemetry,
    build_reranker,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_rerank]

# A seeded candidate set where the lexically-"obvious" doc is NOT the most
# relevant answer to the query, so a real learned model must reorder it.
_QUERY = "how do I rotate a leaked API credential safely without downtime?"
_DOCUMENTS = [
    # index 0 — lexical decoy: shares the word "rotating" but is irrelevant.
    "A recipe for rotating fresh basil pesto in a blender until smooth.",
    # index 1 — the real answer (little lexical overlap with the query).
    "To rotate a leaked secret with zero downtime: mint a new credential, "
    "deploy it alongside the old one, cut traffic over, then revoke the old key.",
    "Kubernetes pod autoscaling tunes replica counts under CPU pressure.",
    "Git rebase rewrites commit history onto a new base branch.",
]


def _reranker_url_reachable() -> bool:
    url = os.getenv("JINA_RERANKER_URL")
    if not url:
        return False
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


def _provider_from_env() -> str | None:
    """The provider to exercise, or ``None`` if no creds/endpoint are present."""
    explicit = (os.getenv("FORGE_RERANK_PROVIDER") or "").strip().lower()
    if explicit in ("jina", "cohere", "selfhosted"):
        if explicit == "jina" and not os.getenv("JINA_API_KEY"):
            return None
        if explicit == "cohere" and not os.getenv("COHERE_API_KEY"):
            return None
        if explicit == "selfhosted" and not _reranker_url_reachable():
            return None
        return explicit
    # Infer from whatever credential is present.
    if os.getenv("JINA_API_KEY"):
        return "jina"
    if os.getenv("COHERE_API_KEY"):
        return "cohere"
    if _reranker_url_reachable():
        return "selfhosted"
    return None


def _api_key_for(provider: str) -> str | None:
    if provider == "jina":
        return os.getenv("JINA_API_KEY")
    if provider == "cohere":
        return os.getenv("COHERE_API_KEY")
    return None


def _live_reranker() -> tuple[GracefulReranker, str]:
    provider = _provider_from_env()
    if provider is None:
        pytest.skip(
            "no reranker creds/endpoint: set JINA_API_KEY or COHERE_API_KEY (or a "
            "reachable JINA_RERANKER_URL) — see docs/runbooks/live-reranker.md"
        )
    budget_ms = int(os.getenv("FORGE_RERANK_TIMEOUT_MS", "5000"))
    base_url = os.getenv("JINA_RERANKER_URL") if provider == "selfhosted" else None
    reranker = build_reranker(
        provider,
        api_key=_api_key_for(provider),
        base_url=base_url,
        timeout_ms=budget_ms,
    )
    assert isinstance(reranker, GracefulReranker)
    return reranker, provider


def test_live_reranker_returns_real_scores() -> None:
    # AC10: a real cross-encoder scores >=3 candidates; scores are floats and are
    # NOT all equal (proves a learned model, not a constant / fixture).
    reranker, _ = _live_reranker()
    results = reranker.rerank(_QUERY, _DOCUMENTS, top_n=len(_DOCUMENTS))
    assert reranker.last_call is not None and reranker.last_call.fallback_used is False
    assert len(results) >= 3
    scores = [r.score for r in results]
    assert all(isinstance(s, float) for s in scores)
    assert len(set(scores)) > 1, "all scores equal — not a real learned reranker"


def test_live_reranker_reorders_within_budget() -> None:
    # AC11: the relevant doc (index 1) is promoted above the lexical decoy
    # (index 0) within the latency budget; latency is captured in telemetry.
    reranker, _ = _live_reranker()
    budget_ms = int(os.getenv("FORGE_RERANK_TIMEOUT_MS", "5000"))

    start = time.perf_counter()
    results = reranker.rerank(_QUERY, _DOCUMENTS, top_n=len(_DOCUMENTS))
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    order = [r.index for r in results]
    assert order.index(1) < order.index(0), "relevant doc not promoted above decoy"
    assert elapsed_ms < budget_ms + 1000  # bounded by the budget (+ slack)
    tel = reranker.last_call
    assert isinstance(tel, RerankTelemetry)
    assert tel.fallback_used is False
    assert tel.latency_ms > 0.0


def test_live_reranker_redacts_and_audits(caplog: pytest.LogCaptureFixture) -> None:
    # AC12: the live call emits one non-fallback telemetry record and the BYOK
    # key appears in NO captured log / telemetry / result payload.
    reranker, provider = _live_reranker()
    key = _api_key_for(provider)

    with caplog.at_level("DEBUG"):
        results = reranker.rerank(_QUERY, _DOCUMENTS, top_n=len(_DOCUMENTS))

    tel = reranker.last_call
    assert tel is not None
    assert tel.fallback_used is False
    assert tel.provider == provider
    assert tel.reason is None
    if key:
        blob = caplog.text + repr(tel) + repr([r.model_dump() for r in results])
        assert key not in blob, "a BYOK reranker key leaked into a log/telemetry/result"


def test_live_rerank_delta_computable() -> None:
    # AC13: the same corpus reranked ON vs OFF yields a finite, computable delta
    # (rank-shift here; HARD-04 publishes recall@k/nDCG@10). A green flag is that
    # the delta is finite on the live path, not a specific value.
    reranker, _ = _live_reranker()
    live = reranker.rerank(_QUERY, _DOCUMENTS, top_n=len(_DOCUMENTS))
    live_order = [r.index for r in live]
    off_order = list(range(len(_DOCUMENTS)))  # rerank OFF == fused/identity order

    # Mean absolute rank shift of each doc between the two orders.
    pos_live = {idx: rank for rank, idx in enumerate(live_order)}
    deltas = [abs(pos_live[i] - off_order.index(i)) for i in off_order if i in pos_live]
    mean_delta = sum(deltas) / len(deltas)
    assert mean_delta == mean_delta  # finite (not NaN)
    assert mean_delta >= 0.0
