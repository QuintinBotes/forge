"""Embedding clients for the Forge Knowledge/RAG pipeline (plan Task 1.2, spine).

Two implementations of the frozen
:class:`forge_contracts.protocols.EmbeddingClient` Protocol:

* :class:`DeterministicEmbeddingClient` — a dependency-free, deterministic
  embedding used by every store/eval test and as an offline default. It is a
  *signed feature-hashing bag-of-words* (the "hashing trick"): each token is
  hashed to a dimension with a sign, accumulated with sub-linear term-frequency
  weighting, and the vector is L2-normalised. Unlike random noise this is
  genuinely semantic — texts that share tokens land close in cosine space — so
  nearest-neighbour tests prove real behaviour, not luck. No network, no model.

* :class:`HttpEmbeddingClient` — the provider-agnostic BYOK reference impl. It
  speaks the ubiquitous OpenAI-compatible embeddings schema
  (``POST {base_url}/embeddings`` with ``{"model", "input"}`` →
  ``{"data":[{"index","embedding"}]}``), so it works against OpenAI, Jina,
  Voyage, or any self-hosted gateway by changing ``base_url``/``model``. It opens
  no connection at construction and is fully testable by injecting an
  ``httpx.Client`` backed by a mock transport — never a real overnight call.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter

import httpx
import numpy as np

from forge_contracts.constants import DEFAULT_EMBEDDING_DIM
from forge_knowledge.text import tokenize

__all__ = [
    "DeterministicEmbeddingClient",
    "HttpEmbeddingClient",
]


class DeterministicEmbeddingClient:
    """A deterministic, semantic, dependency-free embedding (signed hashing).

    Implements :class:`forge_contracts.protocols.EmbeddingClient`. Suitable as a
    test double and an offline default; not a substitute for a learned model in
    production (use :class:`HttpEmbeddingClient` with a real provider there).
    """

    def __init__(self, dimension: int = DEFAULT_EMBEDDING_DIM) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def _embed_one(self, text: str) -> list[float]:
        vector = np.zeros(self._dimension, dtype=np.float64)
        counts = Counter(tokenize(text))
        for token, count in counts.items():
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % self._dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            # Sub-linear term frequency damps the effect of repeated tokens.
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector.tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)


class HttpEmbeddingClient:
    """Provider-agnostic OpenAI-compatible embeddings client (BYOK reference).

    Implements :class:`forge_contracts.protocols.EmbeddingClient`. The ``client``
    argument allows injecting an ``httpx.Client`` (e.g. with a mock transport)
    for hermetic tests; in production it is created lazily with ``timeout``.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        dimension: int = DEFAULT_EMBEDDING_DIM,
        path: str = "/embeddings",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._dimension = dimension
        self._url = f"{base_url.rstrip('/')}{path}"
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def dimension(self) -> int:
        return self._dimension

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._http().post(
            self._url,
            json={"model": self._model, "input": texts},
            headers=self._headers(),
        )
        response.raise_for_status()
        rows = response.json()["data"]
        # Honour the provider's ``index`` field rather than assuming order.
        ordered = sorted(rows, key=lambda row: row.get("index", 0))
        return [list(row["embedding"]) for row in ordered]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def close(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
