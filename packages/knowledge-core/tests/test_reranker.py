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
from itertools import pairwise

import httpx
import pytest

from forge_contracts.dtos import RerankResult
from forge_contracts.protocols import RerankerClient
from forge_knowledge.reranker import FixtureRerankerClient, JinaRerankerClient

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


def test_jina_raises_on_error_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = JinaRerankerClient(
        "jina-reranker-v2",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.rerank("q", ["d"], top_n=1)


def test_jina_results_are_reranke_results() -> None:
    client = JinaRerankerClient(
        "jina-reranker-v2", client=_mock_client([], ranking=[(0, 0.7)])
    )
    (result,) = client.rerank("q", ["only doc"], top_n=1)
    assert isinstance(result, RerankResult)
    assert result.document == "only doc"
