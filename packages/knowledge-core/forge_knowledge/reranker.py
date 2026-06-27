"""Cross-encoder rerankers for the Forge Knowledge/RAG pipeline (Task 1.3, spine).

Reranking is the final quality lever in the retrieval pipeline (spec: *Jina
Reranker v2 ... 15-30% quality improvement*): after RRF fuses the semantic and
keyword legs, a cross-encoder re-scores each candidate against the *full* query
and the top-n survive. Two implementations of the frozen
:class:`forge_contracts.protocols.RerankerClient` Protocol:

* :class:`FixtureRerankerClient` — a deterministic, dependency-free fake used by
  every retrieval/eval test and as an offline default. With an explicit fixture
  (``{query: {document: score}}``) it reproduces a recorded reranking exactly;
  without one it falls back to a deterministic query/document token-overlap
  relevance. Either way the reordering is *real*, never random.

* :class:`JinaRerankerClient` — the provider-agnostic BYOK reference impl. It
  speaks the Jina Reranker v2 HTTP schema (``POST {base_url}/rerank`` with
  ``{"model","query","documents","top_n"}`` -> ``{"results":[{"index",
  "relevance_score"}]}``), which the Cohere/Voyage rerank APIs mirror closely, so
  it works against a self-hosted or hosted reranker by changing ``base_url`` /
  ``model``. It opens no connection at construction and is fully testable by
  injecting an ``httpx.Client`` with a mock transport — never a real call.
"""

from __future__ import annotations

import httpx

from forge_contracts.dtos import RerankResult
from forge_knowledge.text import tokenize

__all__ = [
    "FixtureRerankerClient",
    "JinaRerankerClient",
]

# query -> {document text -> relevance score}
RerankFixture = dict[str, dict[str, float]]


class FixtureRerankerClient:
    """Deterministic reranker (fixture-backed, token-overlap fallback).

    Implements :class:`forge_contracts.protocols.RerankerClient`. Suitable as a
    test double and an offline default; not a substitute for a learned
    cross-encoder in production (use :class:`JinaRerankerClient` there).
    """

    def __init__(self, fixture: RerankFixture | None = None) -> None:
        self._fixture = fixture or {}

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[RerankResult]:
        if not documents:
            return []
        scores = self._score(query, documents)
        ranked = sorted(
            enumerate(scores), key=lambda item: (item[1], -item[0]), reverse=True
        )
        return [
            RerankResult(index=index, score=score, document=documents[index])
            for index, score in ranked[: max(top_n, 0)]
        ]

    def _score(self, query: str, documents: list[str]) -> list[float]:
        recorded = self._fixture.get(query)
        if recorded is not None:
            return [float(recorded.get(doc, 0.0)) for doc in documents]
        return [self._overlap(query, doc) for doc in documents]

    @staticmethod
    def _overlap(query: str, document: str) -> float:
        """Deterministic relevance: fraction of query terms present in the doc.

        A small bonus rewards documents that contain the query as a contiguous
        phrase, so an exact match ranks above a bag-of-words match.
        """
        query_terms = set(tokenize(query))
        if not query_terms:
            return 0.0
        doc_terms = set(tokenize(document))
        overlap = len(query_terms & doc_terms) / len(query_terms)
        phrase_bonus = 0.25 if query.strip().lower() in document.lower() else 0.0
        return overlap + phrase_bonus


class JinaRerankerClient:
    """Jina Reranker v2 HTTP client (BYOK reference impl).

    Implements :class:`forge_contracts.protocols.RerankerClient`. The ``client``
    argument allows injecting an ``httpx.Client`` (e.g. with a mock transport)
    for hermetic tests; in production it is created lazily with ``timeout``.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.jina.ai/v1",
        path: str = "/rerank",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}{path}"
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[RerankResult]:
        if not documents:
            return []
        response = self._http().post(
            self._url,
            json={
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            },
            headers=self._headers(),
        )
        response.raise_for_status()
        results = response.json()["results"]
        return [
            RerankResult(
                index=int(row["index"]),
                score=float(row["relevance_score"]),
                document=documents[int(row["index"])],
            )
            for row in results
        ]

    def close(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
