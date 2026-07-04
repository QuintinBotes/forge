"""Tests for ``forge_knowledge.embeddings`` (plan Task 1.2, RAG spine).

Two embedding clients are covered:

* :class:`DeterministicEmbeddingClient` — the BYOK-free deterministic fake every
  store/eval test relies on. It must be deterministic, correctly dimensioned,
  L2-normalised, and *semantically meaningful* (texts that share tokens are
  closer in cosine space than unrelated texts) so the vector-store tests prove
  real nearest-neighbour behaviour rather than coincidence.
* :class:`HttpEmbeddingClient` — the provider-agnostic (OpenAI-compatible) BYOK
  reference impl. Exercised hermetically via ``httpx.MockTransport`` (no network):
  the request shape (model, input, auth header) and response parsing are verified.

All hermetic: no external services, no network.
"""

from __future__ import annotations

import json
import math

import httpx
import numpy as np
import pytest

from forge_contracts.constants import DEFAULT_EMBEDDING_DIM
from forge_contracts.protocols import EmbeddingClient
from forge_knowledge.embeddings import (
    DeterministicEmbeddingClient,
    HttpEmbeddingClient,
)


def _cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(va @ vb / (na * nb))


# --------------------------------------------------------------------------- #
# DeterministicEmbeddingClient                                                 #
# --------------------------------------------------------------------------- #


def test_deterministic_client_satisfies_protocol() -> None:
    assert isinstance(DeterministicEmbeddingClient(), EmbeddingClient)


def test_dimension_defaults_to_spec_constant() -> None:
    assert DeterministicEmbeddingClient().dimension == DEFAULT_EMBEDDING_DIM
    assert DeterministicEmbeddingClient(dimension=128).dimension == 128


def test_embed_is_deterministic_and_correctly_shaped() -> None:
    client = DeterministicEmbeddingClient(dimension=128)
    first = client.embed(["hello world", "second text"])
    second = client.embed(["hello world", "second text"])
    assert first == second
    assert len(first) == 2
    assert all(len(vec) == 128 for vec in first)


def test_embed_query_matches_embed_single() -> None:
    client = DeterministicEmbeddingClient(dimension=64)
    assert client.embed_query("a query string") == client.embed(["a query string"])[0]


def test_nonempty_vectors_are_l2_normalised() -> None:
    client = DeterministicEmbeddingClient(dimension=256)
    (vec,) = client.embed(["connection pool manager"])
    assert math.isclose(np.linalg.norm(vec), 1.0, rel_tol=1e-6)


def test_empty_text_yields_zero_vector_of_right_length() -> None:
    client = DeterministicEmbeddingClient(dimension=32)
    (vec,) = client.embed([""])
    assert len(vec) == 32
    assert all(component == 0.0 for component in vec)


def test_embed_empty_list_returns_empty() -> None:
    assert DeterministicEmbeddingClient().embed([]) == []


def test_embedding_is_semantically_meaningful() -> None:
    # Texts sharing tokens must be closer than unrelated texts — this is what
    # makes the deterministic fake usable for genuine nearest-neighbour tests.
    client = DeterministicEmbeddingClient(dimension=512)
    anchor = client.embed_query("database connection pooling for postgres")
    related = client.embed_query("postgres connection pool configuration")
    unrelated = client.embed_query("react frontend component styling with css")
    assert _cosine(anchor, related) > _cosine(anchor, unrelated)


# --------------------------------------------------------------------------- #
# HttpEmbeddingClient (BYOK reference impl)                                    #
# --------------------------------------------------------------------------- #


def _mock_client(captured: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        inputs = body["input"]
        data = [
            {"index": i, "embedding": [float(len(text)), 1.0, 0.0]}
            for i, text in enumerate(inputs)
        ]
        return httpx.Response(200, json={"data": data, "model": body["model"]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_client_satisfies_protocol() -> None:
    client = HttpEmbeddingClient("text-embed", dimension=3, client=_mock_client([]))
    assert isinstance(client, EmbeddingClient)


def test_http_client_sends_model_input_and_auth() -> None:
    captured: list[httpx.Request] = []
    client = HttpEmbeddingClient(
        "text-embed",
        api_key="sk-secret",
        base_url="https://vendor.example/v1",
        dimension=3,
        client=_mock_client(captured),
    )
    vectors = client.embed(["aa", "bbbb"])

    assert vectors == [[2.0, 1.0, 0.0], [4.0, 1.0, 0.0]]
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path.endswith("/embeddings")
    assert request.headers["authorization"] == "Bearer sk-secret"
    sent = json.loads(request.content)
    assert sent == {"model": "text-embed", "input": ["aa", "bbbb"]}


def test_http_client_embed_query_returns_single_vector() -> None:
    client = HttpEmbeddingClient("text-embed", dimension=3, client=_mock_client([]))
    assert client.embed_query("aaa") == [3.0, 1.0, 0.0]


def test_http_client_embed_empty_list_makes_no_request() -> None:
    captured: list[httpx.Request] = []
    client = HttpEmbeddingClient("text-embed", dimension=3, client=_mock_client(captured))
    assert client.embed([]) == []
    assert captured == []


def test_http_client_raises_on_error_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = HttpEmbeddingClient(
        "text-embed", dimension=3, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.embed(["x"])
