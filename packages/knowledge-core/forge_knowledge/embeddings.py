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

* :class:`SentenceTransformerEmbeddingClient` — the *learned, local, no-key*
  embedder. It loads an open-weight ``sentence-transformers`` model (default
  ``all-MiniLM-L6-v2``, 384-dim) from the local Hugging Face cache and computes
  embeddings on-device — genuine semantic recall with **zero API spend and zero
  network at call time** once the model is cached. ``sentence-transformers`` is
  an *optional* dependency (the ``eval`` extra): the import is lazy, so the base
  package and the default hermetic test suite never require torch. This is the
  BETA-critical embedder for the honest real-corpus eval (HARD-04); the provider
  :class:`HttpEmbeddingClient` remains the BYOK path for hosted models.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np

from forge_contracts.constants import DEFAULT_EMBEDDING_DIM
from forge_knowledge.text import tokenize

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import of torch
    from sentence_transformers import SentenceTransformer

    from forge_contracts.protocols import EmbeddingClient

__all__ = [
    "DEFAULT_SENTENCE_TRANSFORMERS_MODEL",
    "DeterministicEmbeddingClient",
    "HttpEmbeddingClient",
    "SentenceTransformerEmbeddingClient",
    "build_embedding_client",
    "build_embedding_client_from_settings",
]

#: Default open-weight local model: small, fast, 384-dim, permissive licence.
DEFAULT_SENTENCE_TRANSFORMERS_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


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
            # Feature hashing, not cryptography: SHA-1 here only buckets tokens
            # deterministically (bandit B324 — usedforsecurity=False).
            # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
            digest = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).digest()
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
        url_validator: Callable[[str], object] | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._dimension = dimension
        self._url = f"{base_url.rstrip('/')}{path}"
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        # HARD-09 SSRF seam: the api/worker inject forge_api.security.ssrf.
        # assert_safe_url here so an admin-configured base_url can never target
        # the cloud metadata service or internal hosts. Default: no validation
        # (backwards compatible; this package stays decoupled from forge_api).
        if url_validator is not None:
            url_validator(self._url)

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
        vectors = [list(row["embedding"]) for row in ordered]
        # The provider chooses the returned width (e.g. text-embedding-3-large
        # returns 3072); the factory can only check the *configured* width.
        # Refuse a mismatch here — RuntimeError, not ValueError, because
        # /knowledge/sync maps ValueError to 422 — instead of storing vectors
        # Postgres would reject on insert (and SQLite would store silently).
        for vector in vectors:
            if len(vector) != self._dimension:
                raise RuntimeError(
                    f"embedding model {self._model!r} returned "
                    f"{len(vector)}-dimensional vectors, expected "
                    f"{self._dimension}; set FORGE_EMBEDDING_DIMENSION to the "
                    "model's output width or choose a matching model"
                )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def close(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


class SentenceTransformerEmbeddingClient:
    """Learned, local, no-key embedder over an open-weight sentence-transformer.

    Implements :class:`forge_contracts.protocols.EmbeddingClient`. The model is
    loaded lazily from the local Hugging Face cache on first use; with a warm
    cache no network I/O happens at call time (set ``HF_HUB_OFFLINE=1`` to prove
    it). Embeddings are L2-normalised so cosine similarity matches the store's
    dot-product ranking. ``sentence-transformers`` is an optional dependency; a
    clear ``RuntimeError`` is raised if it (or the model) cannot be loaded, so the
    eval *skips* rather than silently falling back to a fake embedder.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_SENTENCE_TRANSFORMERS_MODEL,
        *,
        device: str | None = None,
        normalize: bool = True,
        batch_size: int = 32,
        model: SentenceTransformer | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._normalize = normalize
        self._batch_size = batch_size
        self._model = model
        self._dimension: int | None = None
        if model is not None:
            self._dimension = int(model.get_sentence_embedding_dimension() or 0) or None

    def _load(self) -> SentenceTransformer:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - exercised only w/o the extra
                raise RuntimeError(
                    "sentence-transformers is not installed; install the 'eval' "
                    "extra (pip install 'sentence-transformers>=3.0') to use the "
                    "local learned embedder."
                ) from exc
            try:
                self._model = SentenceTransformer(self._model_name, device=self._device)
            except Exception as exc:
                raise RuntimeError(
                    f"could not load sentence-transformers model {self._model_name!r} "
                    f"(is it cached? set HF_HUB_OFFLINE=0 for the first download): {exc}"
                ) from exc
            self._dimension = int(self._model.get_sentence_embedding_dimension() or 0) or None
        return self._model

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._load()
        assert self._dimension is not None
        return self._dimension

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors: Any = model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=self._normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in np.asarray(vectors, dtype=np.float64)]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._encode(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text])[0]


# --------------------------------------------------------------------------- #
# Production embedder factory (spec J §2.5, ADR-0018)                          #
# --------------------------------------------------------------------------- #

#: Providers that mean "no embedder configured": search runs BM25-only and
#: says so, instead of silently falling back to the hash embedder.
_UNCONFIGURED_PROVIDERS = frozenset({"", "none"})


def _expected_dimension() -> int:
    """The ``retrieval_chunk.embedding`` column width (``EMBEDDING_DIM``)."""
    try:
        from forge_db.models.knowledge import EMBEDDING_DIM

        return int(EMBEDDING_DIM)
    except ImportError:  # pragma: no cover - forge_db is a workspace sibling
        return DEFAULT_EMBEDDING_DIM


def _refuse_dimension_mismatch(configured: int, expected: int) -> ValueError:
    return ValueError(
        f"embedding dimension {configured} does not match the "
        f"retrieval_chunk.embedding column dimension {expected} "
        "(forge_db EMBEDDING_DIM); refusing to start with an embedder whose "
        "vectors would fail on insert — set FORGE_EMBEDDING_DIMENSION to "
        f"{expected} or choose a {expected}-dimensional model"
    )


def build_embedding_client(
    provider: str | None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    dimension: int | None = None,
    api_key: str | None = None,
    url_validator: Callable[[str], object] | None = None,
) -> EmbeddingClient | None:
    """Return the configured production embedder, or ``None`` when unset.

    * ``provider in {None, "", "none"}`` -> ``None`` (honest BM25-only mode;
      the deterministic hash embedder is never a production default).
    * ``"openai-compatible"`` -> :class:`HttpEmbeddingClient`; ``base_url``
      is passed through the caller's SSRF ``url_validator`` seam. Requires an
      explicit ``dimension``: the provider's output width cannot be known
      without a paid probe call, and silently defaulting it built a
      1536-wide client for a 3072-wide model.
    * ``"local"`` -> :class:`SentenceTransformerEmbeddingClient` (its model
      is loaded now so a dimension mismatch refuses at construction).

    Raises ``ValueError`` for an unknown provider, a missing model, a
    missing dimension for ``openai-compatible``, or any dimension that does
    not equal the vector column's ``EMBEDDING_DIM``.
    """
    key = (provider or "").strip().lower()
    if key in _UNCONFIGURED_PROVIDERS:
        return None

    expected = _expected_dimension()
    if dimension is not None and dimension != expected:
        raise _refuse_dimension_mismatch(dimension, expected)

    if key == "openai-compatible":
        if not model:
            raise ValueError(
                "FORGE_EMBEDDING_PROVIDER=openai-compatible requires "
                "FORGE_EMBEDDING_MODEL to name the embedding model"
            )
        if dimension is None:
            raise ValueError(
                "FORGE_EMBEDDING_PROVIDER=openai-compatible requires "
                "FORGE_EMBEDDING_DIMENSION to state the model's output "
                f"width (the vector column is {expected}-dimensional); "
                "refusing to guess it and fail on first insert"
            )
        return HttpEmbeddingClient(
            model,
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            dimension=dimension,
            url_validator=url_validator,
        )

    if key == "local":
        local = SentenceTransformerEmbeddingClient(model or DEFAULT_SENTENCE_TRANSFORMERS_MODEL)
        actual = local.dimension  # loads the model; fail closed here, not on insert
        if actual != expected:
            raise _refuse_dimension_mismatch(actual, expected)
        return local

    raise ValueError(
        f"unknown embedding provider {provider!r} "
        "(expected one of: openai-compatible, local, or unset for BM25-only)"
    )


def build_embedding_client_from_settings(
    settings: object,
    *,
    api_key: str | None = None,
    url_validator: Callable[[str], object] | None = None,
) -> EmbeddingClient | None:
    """Build an embedder from a ``FORGE_EMBEDDING_*`` settings-like object.

    Reads (via ``getattr``, so this package stays decoupled from
    ``forge_api``): ``embedding_provider``, ``embedding_model``,
    ``embedding_base_url``, ``embedding_dimension``. The BYOK ``api_key`` is
    passed by the caller (resolved from the env/vault on demand) and never
    read from settings; ``url_validator`` is the api's SSRF guard seam.
    """
    raw_dimension = getattr(settings, "embedding_dimension", None)
    return build_embedding_client(
        getattr(settings, "embedding_provider", None),
        model=getattr(settings, "embedding_model", None) or None,
        base_url=getattr(settings, "embedding_base_url", None) or None,
        dimension=int(raw_dimension) if raw_dimension is not None else None,
        api_key=api_key,
        url_validator=url_validator,
    )
