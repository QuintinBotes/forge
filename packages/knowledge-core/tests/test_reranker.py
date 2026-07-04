"""Tests for ``forge_knowledge.reranker`` (plan Task 1.3, RAG spine).

Two implementations of the frozen
:class:`forge_contracts.protocols.RerankerClient` Protocol:

* :class:`FixtureRerankerClient` — the deterministic, dependency-free fake used
  by every retrieval/eval test and as an offline default. It reorders documents
  by an explicit fixture (query -> {document: score}) when provided, otherwise by
  a deterministic query/document token-overlap relevance. Either way it produces
  a *genuine* reordering — exactly what a cross-encoder reranker is for.

* :class:`JinaRerankerClient` — the provider-agnostic BYOK reference impl
  speaking the Jina Reranker v2 HTTP schema. Exercised hermetically through
  ``httpx.MockTransport`` (no network): request shape + response parsing.
"""

from __future__ import annotations

import json
import logging
import time
from itertools import pairwise

import httpx
import pytest

from forge_contracts.dtos import RerankResult
from forge_contracts.protocols import RerankerClient
from forge_knowledge.reranker import (
    FixtureRerankerClient,
    GracefulReranker,
    JinaRerankerClient,
    RerankerUnavailableError,
    RerankTelemetry,
    build_reranker,
    build_reranker_from_settings,
)

# --------------------------------------------------------------------------- #
# FixtureRerankerClient                                                        #
# --------------------------------------------------------------------------- #


def test_fixture_client_satisfies_protocol() -> None:
    assert isinstance(FixtureRerankerClient(), RerankerClient)


def test_fixture_reorders_documents_per_fixture() -> None:
    # Documents are supplied in a deliberately *wrong* order; the fixture scores
    # encode the desired relevance ranking, so the reranker must reorder them.
    documents = ["irrelevant filler", "the exact answer", "somewhat related"]
    fixture = {
        "what is the answer?": {
            "the exact answer": 0.95,
            "somewhat related": 0.40,
            "irrelevant filler": 0.01,
        }
    }
    reranker = FixtureRerankerClient(fixture)

    results = reranker.rerank("what is the answer?", documents, top_n=3)

    assert [r.index for r in results] == [1, 2, 0]
    assert [r.document for r in results] == [
        "the exact answer",
        "somewhat related",
        "irrelevant filler",
    ]
    assert results[0].score == 0.95
    # Scores are monotonically non-increasing.
    assert all(a.score >= b.score for a, b in pairwise(results))


def test_fixture_respects_top_n() -> None:
    documents = ["a", "b", "c", "d"]
    reranker = FixtureRerankerClient(
        {"q": {"a": 0.1, "b": 0.9, "c": 0.5, "d": 0.7}}
    )
    results = reranker.rerank("q", documents, top_n=2)
    assert [r.index for r in results] == [1, 3]
    assert len(results) == 2


def test_fixture_fallback_uses_token_overlap_relevance() -> None:
    # No fixture for this query -> deterministic token-overlap relevance ranks the
    # document that shares the most query terms first.
    reranker = FixtureRerankerClient()
    documents = [
        "kubernetes pod autoscaling nodes",
        "validate a jwt token during oauth authentication flow",
        "react component styling",
    ]
    results = reranker.rerank("oauth jwt token validation", documents, top_n=3)
    assert results[0].index == 1


def test_fixture_empty_documents_returns_empty() -> None:
    assert FixtureRerankerClient().rerank("q", [], top_n=5) == []


def test_fixture_is_deterministic() -> None:
    reranker = FixtureRerankerClient()
    docs = ["alpha beta", "beta gamma", "gamma delta"]
    first = reranker.rerank("beta gamma", docs, top_n=3)
    second = reranker.rerank("beta gamma", docs, top_n=3)
    assert [r.index for r in first] == [r.index for r in second]


# --------------------------------------------------------------------------- #
# JinaRerankerClient (BYOK reference impl)                                     #
# --------------------------------------------------------------------------- #


def _mock_client(
    captured: list[httpx.Request], *, ranking: list[tuple[int, float]]
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        top_n = body.get("top_n", len(body["documents"]))
        results = [
            {"index": idx, "relevance_score": score}
            for idx, score in ranking[:top_n]
        ]
        return httpx.Response(200, json={"model": body["model"], "results": results})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_jina_client_satisfies_protocol() -> None:
    client = JinaRerankerClient("jina-reranker-v2", client=_mock_client([], ranking=[]))
    assert isinstance(client, RerankerClient)


def test_jina_sends_expected_payload_and_auth() -> None:
    captured: list[httpx.Request] = []
    client = JinaRerankerClient(
        "jina-reranker-v2-base-multilingual",
        api_key="jina-secret",
        base_url="https://api.jina.ai/v1",
        client=_mock_client(captured, ranking=[(2, 0.9), (0, 0.5), (1, 0.1)]),
    )

    results = client.rerank("query text", ["doc a", "doc b", "doc c"], top_n=2)

    assert [r.index for r in results] == [2, 0]
    assert results[0].score == 0.9
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path.endswith("/rerank")
    assert request.headers["authorization"] == "Bearer jina-secret"
    sent = json.loads(request.content)
    assert sent == {
        "model": "jina-reranker-v2-base-multilingual",
        "query": "query text",
        "documents": ["doc a", "doc b", "doc c"],
        "top_n": 2,
    }


def test_jina_empty_documents_makes_no_request() -> None:
    captured: list[httpx.Request] = []
    client = JinaRerankerClient(
        "jina-reranker-v2", client=_mock_client(captured, ranking=[])
    )
    assert client.rerank("q", [], top_n=5) == []
    assert captured == []


def test_jina_raises_reranker_unavailable_on_error_status() -> None:
    # HARD-03: a non-2xx no longer leaks httpx.HTTPStatusError; it becomes the
    # single typed error the retriever catches to degrade to weighted-RRF.
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = JinaRerankerClient(
        "jina-reranker-v2",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RerankerUnavailableError):
        client.rerank("q", ["d"], top_n=1)


def test_jina_results_are_reranke_results() -> None:
    client = JinaRerankerClient(
        "jina-reranker-v2", client=_mock_client([], ranking=[(0, 0.7)])
    )
    (result,) = client.rerank("q", ["only doc"], top_n=1)
    assert isinstance(result, RerankResult)
    assert result.document == "only doc"


# --------------------------------------------------------------------------- #
# HARD-03: GracefulReranker (budget + typed fallback)                         #
# --------------------------------------------------------------------------- #


def test_graceful_reranker_satisfies_protocol() -> None:
    # AC1: the decorator is itself a RerankerClient.
    inner = JinaRerankerClient("jina-reranker-v2", client=_mock_client([], ranking=[]))
    assert isinstance(GracefulReranker(inner), RerankerClient)


def test_graceful_passes_through_healthy_results_and_records_telemetry() -> None:
    inner = JinaRerankerClient(
        "jina-reranker-v2-base-multilingual",
        provider="jina",
        client=_mock_client([], ranking=[(1, 0.9), (0, 0.4)]),
    )
    graceful = GracefulReranker(inner, timeout_ms=800)

    results = graceful.rerank("q", ["doc a", "doc b"], top_n=2)

    assert [r.index for r in results] == [1, 0]
    tel = graceful.last_call
    assert isinstance(tel, RerankTelemetry)
    assert tel.fallback_used is False
    assert tel.provider == "jina"
    assert tel.model == "jina-reranker-v2-base-multilingual"
    assert tel.candidates == 2
    assert tel.reason is None


def test_graceful_degrades_to_empty_on_503() -> None:
    # AC3: an upstream 503 -> RerankerUnavailableError inside -> empty + telemetry.
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    inner = JinaRerankerClient(
        "jina-reranker-v2", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    graceful = GracefulReranker(inner, timeout_ms=800)

    results = graceful.rerank("q", ["a", "b"], top_n=2)

    assert results == []
    assert graceful.last_call is not None
    assert graceful.last_call.fallback_used is True
    assert graceful.last_call.reason is not None


def test_graceful_enforces_latency_budget_without_hanging() -> None:
    # AC4: an inner call that sleeps far past the budget returns via the fallback
    # within (budget + slack) — never a multi-second hang.
    def slow_handler(request: httpx.Request) -> httpx.Response:
        time.sleep(2.0)  # far past the 50 ms budget
        return httpx.Response(200, json={"model": "m", "results": []})

    inner = JinaRerankerClient(
        "jina-reranker-v2", client=httpx.Client(transport=httpx.MockTransport(slow_handler))
    )
    graceful = GracefulReranker(inner, timeout_ms=50)

    start = time.perf_counter()
    results = graceful.rerank("q", ["a", "b"], top_n=2)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert results == []
    assert elapsed_ms < 800  # bounded by the budget, not the 2 s sleep
    assert graceful.last_call is not None
    assert graceful.last_call.fallback_used is True
    assert "budget" in (graceful.last_call.reason or "")


# --------------------------------------------------------------------------- #
# HARD-03: build_reranker factory + provider defaults                         #
# --------------------------------------------------------------------------- #


def test_build_reranker_fixture_and_disabled_make_no_client() -> None:
    # AC2: fixture / disabled -> the offline fixture, no network client built.
    assert isinstance(build_reranker("fixture"), FixtureRerankerClient)
    assert isinstance(build_reranker(None), FixtureRerankerClient)
    assert isinstance(build_reranker("jina", enabled=False), FixtureRerankerClient)


def test_build_reranker_provider_defaults() -> None:
    # AC2: jina/cohere/selfhosted -> GracefulReranker(JinaRerankerClient(...)).
    jina = build_reranker("jina")
    assert isinstance(jina, GracefulReranker)
    assert jina.provider == "jina"
    assert jina.model == "jina-reranker-v2-base-multilingual"

    cohere = build_reranker("cohere")
    assert isinstance(cohere, GracefulReranker)
    assert cohere.provider == "cohere"
    assert cohere.model == "rerank-v3.5"

    selfhosted = build_reranker("selfhosted", base_url="http://reranker:8080")
    assert isinstance(selfhosted, GracefulReranker)
    assert selfhosted.provider == "selfhosted"


def test_build_reranker_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown reranker provider"):
        build_reranker("voyage")


def test_build_reranker_from_settings_defaults_to_fixture() -> None:
    class _S:  # bare settings-like object, provider unset
        pass

    assert isinstance(build_reranker_from_settings(_S()), FixtureRerankerClient)


def test_build_reranker_from_settings_builds_configured_provider() -> None:
    class _S:
        rerank_enabled = True
        rerank_provider = "cohere"
        rerank_timeout_ms = 500

    reranker = build_reranker_from_settings(_S(), api_key="cohere-secret")
    assert isinstance(reranker, GracefulReranker)
    assert reranker.provider == "cohere"


# --------------------------------------------------------------------------- #
# HARD-03: SSRF guard (AC7)                                                    #
# --------------------------------------------------------------------------- #


def test_ssrf_guard_rejects_offhost_base_url_for_hosted_provider() -> None:
    with pytest.raises(ValueError, match="SSRF"):
        build_reranker("jina", base_url="http://169.254.169.254/latest/meta-data")


def test_ssrf_guard_rejects_public_selfhosted_without_optin() -> None:
    with pytest.raises(ValueError, match="public host"):
        build_reranker("selfhosted", base_url="https://reranker.example.com")


def test_ssrf_guard_allows_private_selfhosted() -> None:
    # loopback / RFC1918 / single-label docker names are allowed without opt-in.
    for url in ("http://127.0.0.1:8080", "http://10.0.0.5:8080", "http://reranker:8080"):
        assert isinstance(build_reranker("selfhosted", base_url=url), GracefulReranker)


def test_ssrf_guard_public_selfhosted_allowed_with_optin() -> None:
    reranker = build_reranker(
        "selfhosted", base_url="https://reranker.example.com", allow_insecure_url=True
    )
    assert isinstance(reranker, GracefulReranker)


# --------------------------------------------------------------------------- #
# HARD-03: Cohere parity (AC8)                                                 #
# --------------------------------------------------------------------------- #


def test_cohere_provider_payload_and_parsing() -> None:
    # AC8: Cohere posts the Jina-shaped body to /v2/rerank with a Bearer header
    # and parses {"results":[{"index","relevance_score"}]} identically.
    captured: list[httpx.Request] = []
    inner = JinaRerankerClient(
        "rerank-v3.5",
        api_key="cohere-secret",
        base_url="https://api.cohere.com",
        path="/v2/rerank",
        provider="cohere",
        client=_mock_client(captured, ranking=[(2, 0.88), (0, 0.30), (1, 0.10)]),
    )

    results = inner.rerank("q", ["doc a", "doc b", "doc c"], top_n=2)

    assert [r.index for r in results] == [2, 0]
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/v2/rerank"
    assert request.headers["authorization"] == "Bearer cohere-secret"
    sent = json.loads(request.content)
    assert sent == {
        "model": "rerank-v3.5",
        "query": "q",
        "documents": ["doc a", "doc b", "doc c"],
        "top_n": 2,
    }


# --------------------------------------------------------------------------- #
# HARD-03: secret redaction (AC6)                                             #
# --------------------------------------------------------------------------- #

# Obviously-fake, secret-shaped value used only to prove redaction. Never real.
_FAKE_RERANK_KEY = "jina-secret-abcdef0123456789"


def test_api_key_never_appears_in_error_on_401(caplog: pytest.LogCaptureFixture) -> None:
    # AC6: a forced 401 whose body even echoes the key must not leak it into the
    # RerankerUnavailableError message or any captured log record.
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"invalid key {_FAKE_RERANK_KEY}"})

    inner = JinaRerankerClient(
        "jina-reranker-v2",
        api_key=_FAKE_RERANK_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    graceful = GracefulReranker(inner, timeout_ms=800)

    with caplog.at_level(logging.DEBUG):
        try:
            inner.rerank("q", ["d"], top_n=1)
        except RerankerUnavailableError as exc:
            assert _FAKE_RERANK_KEY not in str(exc)
        graceful.rerank("q", ["d"], top_n=1)

    tel = graceful.last_call
    assert tel is not None
    assert tel.reason is not None
    assert _FAKE_RERANK_KEY not in (tel.reason or "")
    assert _FAKE_RERANK_KEY not in caplog.text
