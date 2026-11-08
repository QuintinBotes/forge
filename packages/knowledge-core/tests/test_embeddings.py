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
from forge_db.models.knowledge import EMBEDDING_DIM
from forge_knowledge.embeddings import (
    DeterministicEmbeddingClient,
    HttpEmbeddingClient,
    SentenceTransformerEmbeddingClient,
    build_embedding_client,
    build_embedding_client_from_settings,
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
            {"index": i, "embedding": [float(len(text)), 1.0, 0.0]} for i, text in enumerate(inputs)
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


def test_http_client_refuses_wrong_width_vectors() -> None:
    # The provider chooses the returned width (3072 for
    # text-embedding-3-large); a mismatched client must refuse loudly with
    # RuntimeError (not ValueError, which /knowledge/sync maps to 422)
    # instead of storing vectors Postgres would reject on insert.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = [
            {"index": i, "embedding": [1.0, 2.0, 3.0, 4.0]} for i, _ in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": data, "model": body["model"]})

    client = HttpEmbeddingClient(
        "text-embed", dimension=3, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(RuntimeError, match="returned 4-dimensional vectors, expected 3"):
        client.embed(["x"])


def test_http_client_raises_on_error_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = HttpEmbeddingClient(
        "text-embed", dimension=3, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.embed(["x"])


# --------------------------------------------------------------------------- #
# Production embedder factory (spec J §2.5, ADR-0018)                          #
# --------------------------------------------------------------------------- #


def test_builder_unset_provider_means_no_embedder() -> None:
    # Honest BM25-only mode: unset/empty/none never builds the hash embedder.
    assert build_embedding_client(None) is None
    assert build_embedding_client("") is None
    assert build_embedding_client("none") is None


def test_builder_openai_compatible_builds_http_client_with_ssrf_seam() -> None:
    validated: list[str] = []
    client = build_embedding_client(
        "openai-compatible",
        model="text-embedding-3-small",
        base_url="https://vendor.example/v1",
        dimension=EMBEDDING_DIM,
        api_key="sk-test",
        url_validator=validated.append,
    )
    assert isinstance(client, HttpEmbeddingClient)
    assert client.dimension == EMBEDDING_DIM
    # The SSRF url_validator seam saw the fully resolved embeddings URL.
    assert validated == ["https://vendor.example/v1/embeddings"]


def test_builder_openai_compatible_requires_model() -> None:
    with pytest.raises(ValueError, match="FORGE_EMBEDDING_MODEL"):
        build_embedding_client("openai-compatible")


def test_builder_openai_compatible_requires_dimension() -> None:
    # Guessing 1536 built a 1536-wide client for 3072-wide models and failed
    # on first insert; the width must be stated explicitly.
    with pytest.raises(ValueError, match="FORGE_EMBEDDING_DIMENSION"):
        build_embedding_client("openai-compatible", model="text-embedding-3-large")


def test_builder_refuses_dimension_mismatch_at_construction() -> None:
    with pytest.raises(ValueError, match="does not match"):
        build_embedding_client("openai-compatible", model="m", dimension=EMBEDDING_DIM + 1)
    with pytest.raises(ValueError, match="does not match"):
        build_embedding_client("local", dimension=384)


def test_builder_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown embedding provider"):
        build_embedding_client("voyage")


class _FakeSentenceTransformer:
    """Stand-in for a loaded sentence-transformers model (no torch, no I/O)."""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    def get_sentence_embedding_dimension(self) -> int:
        return self._dimension


def test_builder_local_refuses_model_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 384-dim local model cannot write to the 1536-dim column: the builder
    # loads it and refuses at construction instead of failing on first insert.
    def fake_load(self: SentenceTransformerEmbeddingClient) -> _FakeSentenceTransformer:
        self._dimension = 384
        return _FakeSentenceTransformer(384)

    monkeypatch.setattr(SentenceTransformerEmbeddingClient, "_load", fake_load)
    with pytest.raises(ValueError, match="does not match"):
        build_embedding_client("local", model="sentence-transformers/all-MiniLM-L6-v2")


def test_builder_local_accepts_matching_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(self: SentenceTransformerEmbeddingClient) -> _FakeSentenceTransformer:
        self._dimension = EMBEDDING_DIM
        return _FakeSentenceTransformer(EMBEDDING_DIM)

    monkeypatch.setattr(SentenceTransformerEmbeddingClient, "_load", fake_load)
    client = build_embedding_client("local", model="some-1536-model")
    assert isinstance(client, SentenceTransformerEmbeddingClient)
    assert client.dimension == EMBEDDING_DIM


def test_builder_from_settings_reads_embedding_fields() -> None:
    class _S:  # bare settings-like object, provider unset
        pass

    assert build_embedding_client_from_settings(_S()) is None

    class _Configured:
        embedding_provider = "openai-compatible"
        embedding_model = "text-embedding-3-small"
        embedding_base_url = "https://vendor.example/v1"
        embedding_dimension = EMBEDDING_DIM

    client = build_embedding_client_from_settings(_Configured(), api_key="sk-test")
    assert isinstance(client, HttpEmbeddingClient)
    assert client.dimension == EMBEDDING_DIM
