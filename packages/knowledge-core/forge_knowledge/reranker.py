"""Cross-encoder rerankers for the Forge Knowledge/RAG pipeline (Task 1.3, spine).

Reranking is the final quality lever in the retrieval pipeline (spec: *Jina
Reranker v2 ... 15-30% quality improvement*): after RRF fuses the semantic and
keyword legs, a cross-encoder re-scores each candidate against the *full* query
and the top-n survive. Implementations of the frozen
:class:`forge_contracts.protocols.RerankerClient` Protocol:

* :class:`FixtureRerankerClient` — a deterministic, dependency-free fake used by
  every retrieval/eval test and as an offline default. With an explicit fixture
  (``{query: {document: score}}``) it reproduces a recorded reranking exactly;
  without one it falls back to a deterministic query/document token-overlap
  relevance. Either way the reordering is *real*, never random.

* :class:`JinaRerankerClient` — the provider-agnostic BYOK reference impl. It
  speaks the Jina Reranker v2 HTTP schema (``POST {base_url}{path}`` with
  ``{"model","query","documents","top_n"}`` -> ``{"results":[{"index",
  "relevance_score"}]}``), which the **Cohere v2 rerank** API mirrors byte-for-
  byte (``POST https://api.cohere.com/v2/rerank``, model ``rerank-v3.5``), so it
  works against a self-hosted or hosted reranker by changing ``base_url`` /
  ``model`` alone. It opens no connection at construction and is fully testable
  by injecting an ``httpx.Client`` with a mock transport — never a real call. On
  a transport/timeout/HTTP failure it raises :class:`RerankerUnavailableError`
  (never a leaked ``httpx`` error), so the retriever can catch a single,
  intentional type.

* :class:`GracefulReranker` — a production decorator (HARD-03) wrapping any inner
  :class:`~forge_contracts.protocols.RerankerClient`. It enforces a hard
  wall-clock **latency budget** (a real cross-encoder adds ~200-800 ms; a slow
  or unhealthy one must never hang a search) and converts any degradation into a
  clean empty result the retriever reads as "fall back to weighted-RRF", plus a
  redacted :class:`RerankTelemetry` record. A reranker outage therefore degrades
  *quality*, never availability.

The :func:`build_reranker` / :func:`build_reranker_from_settings` factories pick
the offline fixture by default and only build a live, budgeted, SSRF-guarded
client when an operator opts in (``FORGE_RERANK_PROVIDER=jina|cohere|selfhosted``).
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from forge_contracts.dtos import RerankResult
from forge_knowledge.redaction import redact_secrets
from forge_knowledge.text import tokenize

if TYPE_CHECKING:
    from forge_contracts.protocols import RerankerClient

__all__ = [
    "DEFAULT_RERANK_TIMEOUT_MS",
    "FixtureRerankerClient",
    "GracefulReranker",
    "JinaRerankerClient",
    "RerankTelemetry",
    "RerankerUnavailableError",
    "build_reranker",
    "build_reranker_from_settings",
]

#: Default per-call latency budget (ms). A real hosted cross-encoder adds
#: ~200-800 ms; exceeding this triggers the weighted-RRF fallback, never a hang.
DEFAULT_RERANK_TIMEOUT_MS: int = 800

# query -> {document text -> relevance score}
RerankFixture = dict[str, dict[str, float]]


class RerankerUnavailableError(RuntimeError):
    """Raised by a live reranker on transport/timeout/HTTP failure.

    The retriever (and :class:`GracefulReranker`) catch this single, intentional
    type instead of bare ``httpx`` errors and degrade to weighted-RRF. Its
    message is always run through the shared redaction filter, so a key echoed in
    a provider error can never surface here.
    """


@dataclass(frozen=True)
class RerankTelemetry:
    """A redacted record of one rerank attempt (feeds the observability event).

    Never carries a raw query or a secret: ``reason`` is redaction-filtered and
    only set when ``fallback_used`` is true.
    """

    provider: str
    model: str | None
    candidates: int
    latency_ms: float
    fallback_used: bool
    reason: str | None = None


class FixtureRerankerClient:
    """Deterministic reranker (fixture-backed, token-overlap fallback).

    Implements :class:`forge_contracts.protocols.RerankerClient`. Suitable as a
    test double and an offline default; not a substitute for a learned
    cross-encoder in production (use :class:`JinaRerankerClient` there).
    """

    #: Provider tag for telemetry (the offline, network-free default).
    provider = "fixture"
    model: str | None = None

    def __init__(self, fixture: RerankFixture | None = None) -> None:
        self._fixture = fixture or {}

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        if not documents:
            return []
        scores = self._score(query, documents)
        ranked = sorted(enumerate(scores), key=lambda item: (item[1], -item[0]), reverse=True)
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
    """Jina Reranker v2 / Cohere v2 HTTP client (BYOK reference impl).

    Implements :class:`forge_contracts.protocols.RerankerClient`. The ``client``
    argument allows injecting an ``httpx.Client`` (e.g. with a mock transport)
    for hermetic tests; in production it is created lazily with ``timeout``.

    On a transport/timeout/HTTP-status failure :meth:`rerank` raises
    :class:`RerankerUnavailableError` with a redacted message — never a raw
    ``httpx`` error and never the ``Authorization`` header (the API key is only
    ever sent as a ``Bearer`` header, never echoed).
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.jina.ai/v1",
        path: str = "/rerank",
        timeout: float = 30.0,
        timeout_ms: int | None = None,
        provider: str = "jina",
        client: httpx.Client | None = None,
        url_validator: Callable[[str], object] | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}{path}"
        # ``timeout_ms`` (the HARD-03 latency budget) wins over ``timeout`` when
        # both are given, so a single knob bounds the real HTTP call.
        self._timeout = timeout if timeout_ms is None else max(timeout_ms, 1) / 1000.0
        self.provider = provider
        self._client = client
        self._owns_client = client is None
        # HARD-09 SSRF seam: the api/worker inject forge_api.security.ssrf.
        # assert_safe_url here so an admin-configured base_url can never target
        # the cloud metadata service or internal hosts. Default: no validation
        # (backwards compatible; this package stays decoupled from forge_api).
        if url_validator is not None:
            url_validator(self._url)

    @property
    def model(self) -> str:
        """The reranker model id (read-only; used for telemetry)."""
        return self._model

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        if not documents:
            return []
        try:
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
        except httpx.HTTPError as exc:
            # httpx.HTTPError covers TimeoutException, TransportError, and
            # HTTPStatusError. Convert to the single typed error the retriever
            # catches; redact defensively (the message never holds the header,
            # but a stray URL credential would be scrubbed here anyway).
            status = ""
            if isinstance(exc, httpx.HTTPStatusError):
                status = f" (HTTP {exc.response.status_code})"
            raise RerankerUnavailableError(
                redact_secrets(f"reranker request failed{status}: {type(exc).__name__}")
            ) from exc
        except (KeyError, ValueError, TypeError) as exc:
            # Malformed/unexpected provider response shape.
            raise RerankerUnavailableError(
                redact_secrets(f"reranker returned an unparseable response: {type(exc).__name__}")
            ) from exc

    def close(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


class GracefulReranker:
    """Latency-budgeted, fail-typed decorator over any ``RerankerClient``.

    Implements :class:`forge_contracts.protocols.RerankerClient`. It runs the
    inner reranker under a hard wall-clock budget (in a daemon thread so a slow
    or hung provider can never block the caller past ``timeout_ms``) and converts
    a timeout or :class:`RerankerUnavailableError` into an **empty** result list
    the retriever reads as "degrade to weighted-RRF", recording a redacted
    :class:`RerankTelemetry` on :attr:`last_call`. A healthy call returns the
    inner results verbatim and records ``fallback_used=False``.

    Fail-open-for-quality, fail-closed-for-secrets: a reranker outage degrades
    ordering but never raises into a search or an agent run.
    """

    def __init__(
        self, inner: RerankerClient, *, timeout_ms: int = DEFAULT_RERANK_TIMEOUT_MS
    ) -> None:
        self._inner = inner
        self._timeout_ms = max(timeout_ms, 1)
        self._provider: str = getattr(inner, "provider", "unknown")
        self._model: str | None = getattr(inner, "model", None)
        self._last_call: RerankTelemetry | None = None

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def last_call(self) -> RerankTelemetry | None:
        """Telemetry for the most recent :meth:`rerank` (redacted)."""
        return self._last_call

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        if not documents:
            self._last_call = RerankTelemetry(
                self._provider, self._model, 0, 0.0, fallback_used=False
            )
            return []

        box: dict[str, object] = {}

        def _run() -> None:
            try:
                box["result"] = self._inner.rerank(query, documents, top_n)
            except BaseException as exc:  # re-inspected on the caller thread
                box["error"] = exc

        start = time.perf_counter()
        thread = threading.Thread(target=_run, name="forge-reranker", daemon=True)
        thread.start()
        thread.join(self._timeout_ms / 1000.0)
        latency_ms = (time.perf_counter() - start) * 1000.0

        if thread.is_alive():
            # Budget exceeded: abandon the daemon thread and degrade. No secret,
            # no query text in the reason.
            self._last_call = RerankTelemetry(
                self._provider,
                self._model,
                len(documents),
                latency_ms,
                fallback_used=True,
                reason=f"reranker exceeded latency budget ({self._timeout_ms} ms)",
            )
            return []

        error = box.get("error")
        if error is not None:
            if isinstance(error, RerankerUnavailableError):
                self._last_call = RerankTelemetry(
                    self._provider,
                    self._model,
                    len(documents),
                    latency_ms,
                    fallback_used=True,
                    reason=redact_secrets(str(error)),
                )
                return []
            # An unexpected error is still degraded (never crash a search), but
            # the reason is redacted and typed so it is diagnosable.
            self._last_call = RerankTelemetry(
                self._provider,
                self._model,
                len(documents),
                latency_ms,
                fallback_used=True,
                reason=redact_secrets(f"reranker error: {type(error).__name__}"),
            )
            return []

        results = box.get("result")
        if not isinstance(results, list):  # pragma: no cover - defensive
            self._last_call = RerankTelemetry(
                self._provider,
                self._model,
                len(documents),
                latency_ms,
                fallback_used=True,
                reason="reranker returned no result",
            )
            return []
        self._last_call = RerankTelemetry(
            self._provider, self._model, len(documents), latency_ms, fallback_used=False
        )
        return results


# --------------------------------------------------------------------------- #
# Provider defaults + SSRF guard + factories (HARD-03)                         #
# --------------------------------------------------------------------------- #

#: Known provider defaults. ``host`` pins the allowed base_url host for hosted
#: providers (SSRF: a hosted provider can never be pointed off-host); it is
#: ``None`` for the self-hosted path, which the private-host guard covers.
_PROVIDER_DEFAULTS: dict[str, dict[str, str | None]] = {
    "jina": {
        "base_url": "https://api.jina.ai/v1",
        "path": "/rerank",
        "model": "jina-reranker-v2-base-multilingual",
        "host": "api.jina.ai",
    },
    "cohere": {
        "base_url": "https://api.cohere.com",
        "path": "/v2/rerank",
        "model": "rerank-v3.5",
        "host": "api.cohere.com",
    },
    "selfhosted": {
        "base_url": None,
        "path": "/rerank",
        "model": "jina-reranker-v2-base-multilingual",
        "host": None,
    },
}

_FIXTURE_PROVIDERS = frozenset({"", "fixture"})
_TRUE = frozenset({"1", "true", "yes", "on"})


def _is_private_host(host: str) -> bool:
    """True iff ``host`` is loopback / RFC1918-private / a private-DNS name.

    A conservative, network-free classification (no DNS resolution) used only by
    the self-hosted SSRF guard so ``build_reranker`` is testable offline. Cloud
    metadata (link-local ``169.254.0.0/16``) is *never* private here. The real
    :func:`forge_api.security.ssrf.assert_safe_url` (injected as ``url_validator``
    by the api/worker) remains the authoritative, DNS-resolving control.
    """
    candidate = host.strip().lower()
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        if candidate == "localhost":
            return True
        # Single-label docker/k8s service names (e.g. ``reranker``) and private
        # DNS suffixes are internal by convention.
        if "." not in candidate:
            return True
        return candidate.endswith((".local", ".internal", ".svc", ".cluster.local"))
    if ip.is_link_local:  # 169.254.0.0/16 — cloud metadata, always public-blocked
        return False
    return ip.is_loopback or ip.is_private


def _guard_base_url(provider: str, base_url: str, *, allow_insecure: bool) -> None:
    """SSRF guard for :func:`build_reranker` (AC7). Raises ``ValueError`` on reject."""
    host = (urlsplit(base_url).hostname or "").strip().lower()
    if provider in ("jina", "cohere"):
        known = _PROVIDER_DEFAULTS[provider]["host"]
        if host != known:
            raise ValueError(
                f"reranker provider {provider!r} base_url host must be {known!r}, "
                f"got {host!r}: refusing an off-host URL (SSRF guard)"
            )
        return
    # selfhosted
    if not host:
        raise ValueError("selfhosted reranker base_url must include a host")
    if allow_insecure:
        return
    if not _is_private_host(host):
        raise ValueError(
            f"selfhosted reranker base_url targets a public host {host!r}; set "
            "FORGE_RERANK_ALLOW_INSECURE_URL=true to allow it (SSRF guard)"
        )


def build_reranker(
    provider: str | None,
    *,
    enabled: bool = True,
    model: str | None = None,
    base_url: str | None = None,
    path: str | None = None,
    api_key: str | None = None,
    timeout_ms: int = DEFAULT_RERANK_TIMEOUT_MS,
    allow_insecure_url: bool = False,
    url_validator: Callable[[str], object] | None = None,
) -> RerankerClient:
    """Return the configured reranker (offline fixture by default).

    * ``provider in {None, "", "fixture"}`` or ``enabled=False`` -> a network-free
      :class:`FixtureRerankerClient` (no client is built, no key is read).
    * ``"jina" | "cohere" | "selfhosted"`` -> a :class:`GracefulReranker` wrapping
      a :class:`JinaRerankerClient` with the provider defaults, budgeted at
      ``timeout_ms`` and SSRF-guarded (:func:`_guard_base_url`, AC7).

    Raises ``ValueError`` for an unknown provider or an SSRF-rejected ``base_url``.
    """
    if not enabled or provider is None or provider.strip().lower() in _FIXTURE_PROVIDERS:
        return FixtureRerankerClient()

    key = provider.strip().lower()
    defaults = _PROVIDER_DEFAULTS.get(key)
    if defaults is None:
        raise ValueError(
            f"unknown reranker provider {provider!r} "
            f"(expected one of: fixture, {', '.join(_PROVIDER_DEFAULTS)})"
        )

    resolved_base = base_url or defaults["base_url"]
    if not resolved_base:
        raise ValueError(
            f"reranker provider {key!r} requires a base_url "
            "(set FORGE_RERANK_BASE_URL or JINA_RERANKER_URL)"
        )
    resolved_path = path or defaults["path"] or "/rerank"
    resolved_model = model or defaults["model"] or "jina-reranker-v2-base-multilingual"

    _guard_base_url(key, resolved_base, allow_insecure=allow_insecure_url)

    inner = JinaRerankerClient(
        resolved_model,
        api_key=api_key,
        base_url=resolved_base,
        path=resolved_path,
        timeout_ms=timeout_ms,
        provider=key,
        url_validator=url_validator,
    )
    return GracefulReranker(inner, timeout_ms=timeout_ms)


def build_reranker_from_settings(settings: object, *, api_key: str | None = None) -> RerankerClient:
    """Build a reranker from a ``FORGE_RERANK_*`` settings-like object.

    Reads (via ``getattr``, so this pure package stays decoupled from
    ``forge_api``): ``rerank_enabled``, ``rerank_provider``, ``rerank_model``,
    ``rerank_base_url``, ``rerank_timeout_ms``, ``rerank_allow_insecure_url``. The
    self-hosted base URL falls back to the non-secret ``JINA_RERANKER_URL`` env
    var when ``rerank_base_url`` is unset. The BYOK ``api_key`` is passed by the
    caller (resolved from the vault/env on demand) and never read from settings.
    """
    enabled = bool(getattr(settings, "rerank_enabled", True))
    provider = (getattr(settings, "rerank_provider", "fixture") or "fixture").strip().lower()
    model = getattr(settings, "rerank_model", None) or None
    base_url = getattr(settings, "rerank_base_url", None) or None
    timeout_ms = int(getattr(settings, "rerank_timeout_ms", DEFAULT_RERANK_TIMEOUT_MS))
    allow_insecure = bool(getattr(settings, "rerank_allow_insecure_url", False))
    url_validator = getattr(settings, "rerank_url_validator", None)

    if provider == "selfhosted" and not base_url:
        base_url = os.environ.get("JINA_RERANKER_URL") or None

    return build_reranker(
        provider,
        enabled=enabled,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_ms=timeout_ms,
        allow_insecure_url=allow_insecure,
        url_validator=url_validator if callable(url_validator) else None,
    )
