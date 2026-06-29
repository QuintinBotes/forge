# HARD-03 — Live Cross-Encoder Reranker (Jina / Cohere) in Hybrid Retrieval

> Phase: hardening · Blocker(s): #1 (no real external systems exercised), #2 (eval numbers offline/deterministic — fixture reranker) · Status target: **DONE (offline)** = the provider-agnostic live reranker client, env/vault credential resolution, latency-budget enforcement, graceful weighted-RRF fallback, and reranker-delta instrumentation all land behind the **frozen** `forge_contracts.protocols.RerankerClient` and pass in the hermetic default suite with **no network and no creds** (the existing `FixtureRerankerClient` stays the offline default). **VERIFIED-LIVE (needs real creds)** = an `@pytest.mark.integration` test, gated on `JINA_API_KEY` *or* `COHERE_API_KEY` (or a self-hosted reference reranker reachable at `JINA_RERANKER_URL`), drives a real cross-encoder over a sample query, asserts it returns real relevance scores, measurably reorders candidates vs the fused order within the latency budget, and emits a redacted telemetry/audit event — and the measured reranker delta (rank-shift now; recall@k/nDCG@10 delta via HARD-04) is recorded. The whole-suite green gate (`uv run pytest -q` + `uv run ruff check .` + `make typecheck` + `cd apps/web && pnpm test`) is green at the end of the workstream; the live test **skips clean** when creds are absent so the default suite stays green and network-free.
>
> **Scope note.** This slice is the *reranker* half of SPEC HARD-03. The *learned embedder* half (local open-weight `sentence-transformers` for honest eval without an API key, and the `Vector(1536)` dimension reconciliation) is its sibling slice (**HARD-03-live-embedder**) and a dependency of HARD-04; the reranker is text-in / score-out and is therefore independent of embedding dimensionality. The published recall@k / MRR / nDCG@10 reranker-delta *numbers* are owned by **HARD-04**; this slice ships the live reranker and the delta-measurement hooks HARD-04 consumes.

---

## 1. Intent — what & why

Reranking is the last and highest-leverage quality lever in Forge's retrieval pipeline. `docs/FORGE_SPEC.md` (Technology Stack, line 153) fixes *"Reranking | Jina Reranker v2 (self-hosted, open-weight) | 15-30% quality improvement"* and the pipeline diagram (FORGE_SPEC line 408) puts a cross-encoder between RRF fusion and the attributed top-k. After RRF fuses the semantic (pgvector cosine) and keyword (BM25) legs, a cross-encoder re-scores each surviving candidate against the *full* query text — recovering relevance that bag-of-words fusion cannot.

Today (MORNING_REPORT §1.3, §5 item 4, §6) the reranker on every code path is `FixtureRerankerClient`: a deterministic token-overlap fake. The real `JinaRerankerClient` exists and is wired, but is exercised **only** through `httpx.MockTransport` — *"Real-provider path not called."* That makes two release blockers true at once:

- **Blocker #1** — the reranker, like every other external boundary, has never spoken to a real system.
- **Blocker #2** — the eval's perfect `recall@5 = 1.000 / MRR = 1.000` (MORNING_REPORT §4) are partly an artifact of a fixture reranker whose token-overlap score *is* the same signal the golden set was built from. The FORGE_SPEC observability metric **"reranker delta"** (line 924) is, today, meaningless.

HARD-03 (reranker) closes this by making a **real learned cross-encoder** a first-class, swappable implementation of the frozen `RerankerClient` protocol on the live retrieval path, with three production properties the fixture never needed: a **latency budget** (a real cross-encoder adds ~200-800 ms; the pipeline must bound it), **graceful fallback** (a slow/unhealthy reranker must degrade to weighted-RRF, never crash a search or an agent run), and a **measured reranker delta** (the slice instruments how much the reranker actually moved results so HARD-04 can publish an honest, non-fixture quality number). It does this without forking `forge_knowledge` — it *extends* the existing `forge_knowledge.reranker` module and the `HybridRetriever`/`KnowledgeService` orchestration.

## 2. User-facing / operator behavior

- **Operator — choose a reranker by env, zero code change.** A self-hoster sets `FORGE_RERANK_PROVIDER=jina` (or `cohere`, or the default `fixture`) plus a key (`JINA_API_KEY` / `COHERE_API_KEY`) or a self-hosted URL (`JINA_RERANKER_URL`). On next boot, `/knowledge/search` and agent task-scoped retrieval rerank with the real cross-encoder. With `FORGE_RERANK_PROVIDER=fixture` (default) or `FORGE_RERANK_ENABLED=false`, behavior is exactly today's — offline, deterministic, network-free.
- **Operator — bounded latency, never a hang.** `FORGE_RERANK_TIMEOUT_MS` (default 800) caps each rerank call. If the reranker exceeds the budget, errors, or is unhealthy, the search still returns — ordered by weighted-RRF — and an operator-visible signal (`fallback_used=true`, reason redacted) is logged/audited and surfaced in the debug payload. A search is never failed *because* the reranker was slow.
- **Member — same results contract, better ordering.** A member searching the knowledge box (`apps/web` knowledge panel) gets the same `RetrievedChunk[]` shape (path, line range, snippet, `score`, `rerank_score`, source attribution). With debug on, they additionally see the reranker provider, whether the live reranker or the fallback ran, the per-call latency, and the **rank delta** (how far each result moved vs the fused order). This feeds the FORGE_SPEC "reranker delta" and "retrieval latency p50/p95/p99" observability metrics.
- **Agent — unchanged interface.** The single-execution agent's `load_context` retrieval (consumer of `KnowledgeService`) calls the same `search()`; it transparently benefits from the live reranker and is protected by the same fallback, so a reranker outage degrades retrieval quality but never blocks an agent run.
- **Security — no secret ever surfaces.** The BYOK reranker key is read from env/vault at call time, sent only as a `Bearer` header, and never appears in a log line, audit row, trace, debug payload, error message, or the lockfile.

## 3. Vertical slice

### 3.1 Data model

**No schema migration.** The reranker is stateless and operates on chunk *text*, so it touches no table and is independent of the `retrieval_chunk.embedding` `Vector(1536)` column (`packages/db/forge_db/models/knowledge.py`, `EMBEDDING_DIM = 1536`). The reranker's output is already representable in the **frozen** `forge_contracts.dtos.RetrievedChunk`: `score` (final = `rerank_score * weight`) and `rerank_score: float | None` (set to `None` on the fallback path). No new columns, no new enum members (`APIKeyKind` is frozen — the BYOK reranker key reuses `APIKeyKind.MODEL_PROVIDER` with `provider="jina"|"cohere"`).

Reranker telemetry (provider, candidate count, per-call latency, `fallback_used`, rank-delta) is emitted to the **existing** observability/audit writer (`apps/api/forge_api/routers/observability.py` audit path) as a structured event — not a new table — keeping the foundation rule "no duplicate packages / conform to the real schema". If a durable metrics table is later wanted, it is a HARD-13 (perf) concern, not this slice.

### 3.2 Backend

All work **extends** `packages/knowledge-core/forge_knowledge` and the `apps/api` wiring; nothing is duplicated.

**`forge_knowledge/reranker.py` (extend, do not rewrite):**

1. Keep `FixtureRerankerClient` (offline default) and `JinaRerankerClient` (already provider-agnostic via `base_url`/`path`/`model`/`api_key`) verbatim — their frozen exports and tests stay green.
2. Add a typed `RerankerUnavailableError(RuntimeError)` raised by the live client on transport/timeout/HTTP errors, so the retriever can catch a single, intentional exception type instead of bare `httpx` errors.
3. Make `JinaRerankerClient` budget-aware and fail-typed: its constructor already takes `timeout: float`; add an explicit `timeout_ms: int | None` convenience and convert `httpx.TimeoutException` / `httpx.HTTPError` / `HTTPStatusError` into `RerankerUnavailableError` inside `rerank()` (the existing `raise_for_status()` path is wrapped). The Jina schema (`POST {base_url}{path}` `{"model","query","documents","top_n"}` → `{"results":[{"index","relevance_score"}]}`) is byte-for-byte mirrored by **Cohere v2 rerank** (`POST https://api.cohere.com/v2/rerank`, model `rerank-v3.5`, `Authorization: Bearer`), so Cohere needs no new client — only provider-specific defaults.
4. Add a `GracefulReranker(RerankerClient)` decorator that wraps any inner `RerankerClient`, enforces the latency budget, catches `RerankerUnavailableError`, and signals degradation to the retriever (returns an empty result list *and* records `fallback_used=True` + redacted reason on a small `last_call: RerankTelemetry` attribute the retriever reads). It implements the frozen sync `rerank(query, documents, top_n) -> list[RerankResult]` signature exactly.
5. Add a `RerankTelemetry` dataclass (`provider`, `model`, `candidates`, `latency_ms`, `fallback_used`, `reason: str | None`) — pure, no DTO change.
6. Add `build_reranker(provider, *, model, base_url, path, api_key, timeout_ms) -> RerankerClient` and `build_reranker_from_settings(settings, *, api_key) -> RerankerClient` factories that return: `FixtureRerankerClient()` for `provider in {"fixture", None}` or when `enabled=False`; otherwise a `GracefulReranker(JinaRerankerClient(...))` configured with provider defaults (`jina`: `base_url=https://api.jina.ai/v1`, `path=/rerank`, `model=jina-reranker-v2-base-multilingual`; `cohere`: `base_url=https://api.cohere.com`, `path=/v2/rerank`, `model=rerank-v3.5`; `selfhosted`: `base_url=$JINA_RERANKER_URL`, no key). **SSRF guard:** non-self-hosted providers reject a `base_url` outside the provider's known host; `selfhosted` requires an explicit `FORGE_RERANK_ALLOW_INSECURE_URL=true` for non-loopback/non-private hosts.

**`forge_knowledge/retriever.py` (extend `HybridRetriever.rerank`):**

7. Add the **graceful fallback** in the one place the reranker is called. Current `rerank()` calls `self._reranker.rerank(query, documents, len(documents))` and crashes if it raises. New behavior:
   - if `self._reranker is None` or rerank is disabled, OR the reranker returns empty due to degradation → order candidates by **weighted RRF** (`candidate.score` from fusion × `chunk.weight`), set `rerank_score=None`, return top-n. This is the documented "fallback to weighted-RRF" and also the `rerank=false` path.
   - capture `_weighted_rrf_fallback(scored, top_n)` as a private helper so both the disabled and the degraded paths share one ordering.
   - compute and stash a **rank delta** (mean |Δposition| of the returned top-n vs their pre-rerank fused order, plus a Kendall-tau-style monotonic flag) on a retriever-side telemetry hook for the debug payload.
8. Keep the existing `final_score = rerank_score * weight` and stable sort semantics intact (AC-preserving for F05 §6.5).

**`forge_knowledge/service.py` (extend `KnowledgeService`):**

9. Thread a `rerank_enabled: bool` and `rerank_budget_ms: int` through `__init__` and `from_session_factory(...)` (additive kwargs with today's defaults) so the service can disable/limit reranking without reconstructing the retriever. `search()` keeps its signature `(query, scope, k=10) -> list[RetrievedChunk]`; the rerank candidate cap stays `DEFAULT_RERANK_CANDIDATES = 50` (DoS bound).

**`apps/api` wiring (extend, swap the hardcoded fixture):**

10. `apps/api/forge_api/routers/knowledge.py` `_knowledge_service_singleton()` currently hardcodes `FixtureRerankerClient()`. Replace with `build_reranker_from_settings(get_settings(), api_key=...)` where the key is resolved from the BYOK vault (`SecretVault.get_secret`, `APIKeyKind.MODEL_PROVIDER`, provider tag) when present, else from env (`JINA_API_KEY`/`COHERE_API_KEY`) for the integration lane, else `None` → fixture. The DI seam (`get_knowledge_service` / `app.dependency_overrides`) is unchanged so tests still inject fakes.
11. Add reranker settings to `apps/api/forge_api/settings.py` (§4).

### 3.3 Worker / agent runtime

- `apps/worker/forge_worker/indexer.py` `build_knowledge_service()` and `apps/worker/forge_worker/syncer.py` use the **same** `build_reranker_from_settings(...)` factory so a worker-side or agent-side `search()` uses the configured reranker (the reranker is *query-time* only — indexing does not call it, so index throughput is unaffected).
- The single-execution agent's retrieval (`packages/agent-runtime` `load_context` consumer of `KnowledgeService`) needs **no change**: it calls `search()` and inherits the live reranker + fallback. A reranker outage therefore degrades context quality but cannot raise inside the LangGraph loop (the bounded-loop/`GraphError` backstop is untouched).
- No new Celery task. No LangGraph change.

### 3.4 Frontend

Minimal, additive to the existing knowledge search panel (`apps/web/components/knowledge/` / `apps/web/lib/api/knowledge.ts`):

- Render `rerank_score` alongside `score` when present; when `rerank_score` is `null`, show a small "ranked by RRF (reranker unavailable)" badge so degradation is visible to operators.
- In the debug view, show: reranker provider + model, `fallback_used`, per-call latency (ms), and the rank-delta summary. No new route; reuses the search response.

### 3.5 Infra / deploy / CI

- **BYOK (BETA default).** No new container required for hosted Jina/Cohere; only env keys. Add to `deploy/.env.example`, `deploy/.env.production.example`, and a new committed `.env.integration.example` (names only): `FORGE_RERANK_PROVIDER`, `FORGE_RERANK_ENABLED`, `FORGE_RERANK_MODEL`, `FORGE_RERANK_BASE_URL`, `FORGE_RERANK_TIMEOUT_MS`, `FORGE_RERANK_CANDIDATES`, `JINA_API_KEY`, `COHERE_API_KEY`, `JINA_RERANKER_URL`.
- **Optional self-hosted reranker** (FORGE_SPEC "self-hosted, open-weight"): document an optional `reranker` service in `deploy/docker-compose.yml` running `jinaai/jina-reranker-v2-base-multilingual` pinned by `@sha256` (digest pinning is HARD-08's gate), on an internal network reachable only by `api`/`worker`, `GET /health` healthcheck, CPU/mem limits, non-root, never exposed via Caddy. Selected with `FORGE_RERANK_PROVIDER=selfhosted` + `JINA_RERANKER_URL=http://reranker:8080`.
- **CI.** The hermetic default lane runs the offline reranker/factory/fallback/budget/redaction tests (no network). A separate **integration lane** runs `-m integration` only when `JINA_API_KEY` or `COHERE_API_KEY` (or a reachable `JINA_RERANKER_URL`) is present; absent, those tests skip clean. The `integration` marker already exists in `pyproject.toml` (`[tool.pytest.ini_options] markers`).

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**Frozen, unchanged** — every implementation conforms to `forge_contracts.protocols.RerankerClient` (note: **synchronous**, not async):

```python
@runtime_checkable
class RerankerClient(Protocol):
    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]: ...

# forge_contracts.dtos
class RerankResult(_Model):
    index: int
    score: float
    document: str | None = None
```

**New / extended (`packages/knowledge-core/forge_knowledge/reranker.py`):**

```python
class RerankerUnavailableError(RuntimeError):
    """Raised by a live reranker on transport/timeout/HTTP failure; caught by the retriever."""

@dataclass(frozen=True)
class RerankTelemetry:
    provider: str            # "fixture" | "jina" | "cohere" | "selfhosted"
    model: str | None
    candidates: int
    latency_ms: float
    fallback_used: bool
    reason: str | None = None   # redacted; set when fallback_used

class GracefulReranker:                      # implements RerankerClient
    def __init__(self, inner: RerankerClient, *, timeout_ms: int = 800) -> None: ...
    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]: ...
    @property
    def last_call(self) -> RerankTelemetry | None: ...

def build_reranker(
    provider: str | None,                    # "fixture"|"jina"|"cohere"|"selfhosted"|None
    *,
    enabled: bool = True,
    model: str | None = None,
    base_url: str | None = None,
    path: str | None = None,
    api_key: str | None = None,
    timeout_ms: int = 800,
) -> RerankerClient: ...

def build_reranker_from_settings(settings, *, api_key: str | None = None) -> RerankerClient: ...
```

`JinaRerankerClient.__init__` gains an optional `timeout_ms: int | None = None` (overrides `timeout`) and its `rerank()` now raises `RerankerUnavailableError` instead of leaking `httpx` exceptions (the mock-transport tests assert the new error type for non-2xx).

**Extended orchestration:**

```python
# forge_knowledge/retriever.py
class HybridRetriever:
    def rerank(self, query: str, candidates: list[Ranked], top_n: int) -> list[RetrievedChunk]: ...
    # now: degrades to weighted-RRF on disabled/empty/RerankerUnavailableError; exposes last rank-delta

# forge_knowledge/service.py
class KnowledgeService:
    @classmethod
    def from_session_factory(
        cls, session_factory, embedding_client, reranker, *,
        candidate_k: int = 50, rerank_candidates: int = 50,
        rerank_enabled: bool = True, rerank_budget_ms: int = 800,
    ) -> "KnowledgeService": ...
```

**Env vars / config keys** (FastAPI `FORGE_` prefix via `apps/api/forge_api/settings.py`):

| Key | Default | Meaning |
|---|---|---|
| `FORGE_RERANK_ENABLED` | `true` | master switch; `false` → weighted-RRF only, no client built |
| `FORGE_RERANK_PROVIDER` | `fixture` | `fixture` \| `jina` \| `cohere` \| `selfhosted` |
| `FORGE_RERANK_MODEL` | provider default | e.g. `jina-reranker-v2-base-multilingual`, `rerank-v3.5` |
| `FORGE_RERANK_BASE_URL` | provider default | override; SSRF-validated |
| `FORGE_RERANK_TIMEOUT_MS` | `800` | per-call latency budget; exceed → fallback |
| `FORGE_RERANK_CANDIDATES` | `50` | max docs sent to reranker (DoS bound) |
| `FORGE_RERANK_ALLOW_INSECURE_URL` | `false` | required to point `selfhosted` at a non-private host |
| `JINA_API_KEY` / `COHERE_API_KEY` | unset | BYOK key (integration lane); prod resolves from vault |
| `JINA_RERANKER_URL` | unset | self-hosted reranker base URL |

BYOK keys are **never** added to `Settings` as plain fields that get logged; they are read on demand (`os.environ` on the integration lane; `SecretVault.get_secret(workspace_id, secret_id)` with `APIKeyKind.MODEL_PROVIDER` in prod) and discarded after constructing the client.

## 5. Dependencies (must exist first)

- **F05 — Hybrid Knowledge Retrieval** (`packages/knowledge-core`) — DONE in ALPHA; this slice extends its `reranker.py` / `retriever.py` / `service.py`. (foundation)
- **Frozen `forge_contracts`** — `RerankerClient`, `RerankResult`, `RetrievedChunk`, `Ranked`, `APIKeyKind`. Unchanged. (foundation)
- **HARD-10 — Production crypto + OAuth seam** (vault) — SOFT but recommended: BYOK reranker keys should resolve from a *real* `FernetCipher`-backed `SecretVault`. Until HARD-10 lands, the integration lane reads the key from `.env.integration` env directly (still never logged); prod-vault resolution is wired behind the same factory arg.
- **HARD-01 — Real Postgres + pgvector** — SOFT: the reranker runs purely on candidate *text*, so it does not require Postgres; but the *measured delta* is only meaningful over real semantic/keyword legs (HARD-01 substrate) rather than the SQLite stand-in. Offline reranker tests need no DB.
- **HARD-03-live-embedder** (sibling) and **HARD-04 — Real RAG eval** — DOWNSTREAM consumers: HARD-04 reports the recall@k/MRR/nDCG@10 reranker-delta numbers using the learned embedder (sibling) + this live reranker + the delta hooks shipped here. This slice does not depend on them; they depend on it.
- **`.env.integration` / `.env.integration.example`** convention from the SPEC "Credentials & secrets handling" — committed example (names only); real file gitignored.

## 6. Acceptance criteria (numbered, testable)

1. **(offline)** `GracefulReranker`, `JinaRerankerClient`, and `FixtureRerankerClient` each satisfy `isinstance(x, RerankerClient)` and the existing `test_reranker.py` suite stays green unchanged. *(no creds)*
2. **(offline)** `build_reranker("fixture")` and `build_reranker(..., enabled=False)` return a fixture/identity path that calls **no** network; `build_reranker("jina"/"cohere"/"selfhosted", ...)` returns a `GracefulReranker` wrapping a `JinaRerankerClient` with the correct provider defaults (base_url/path/model). *(no creds)*
3. **(offline)** **Graceful fallback:** with a reranker that raises `RerankerUnavailableError` (mock transport returning 503, and a forced `httpx.TimeoutException`), `HybridRetriever.rerank` returns the **weighted-RRF order** (no exception), every returned chunk has `rerank_score is None`, and `fallback_used=True` is recorded. *(no creds)*
4. **(offline)** **Latency budget:** a mock transport that sleeps past `timeout_ms` triggers the fallback path within the budget (assert wall-clock < budget + slack), not a 30 s hang. *(no creds)*
5. **(offline)** **Disabled path / F05 §6.5 preserved:** `rerank_enabled=False` (or `rerank=false`) yields order == weighted-RRF order and the reranker is never called; with rerank enabled and a fixture that encodes a different order, the result order differs from fused order and `final_score == rerank_score * weight`. *(no creds)*
6. **(offline)** **Redaction:** with `api_key="jina-secret"`, no rerank code path writes the key to a log record, the `RerankTelemetry.reason`, the debug payload, or an exception message — asserted by capturing logs + the rendered error on a forced 401/500. *(no creds)*
7. **(offline)** **SSRF guard:** `build_reranker("jina", base_url="http://169.254.169.254/...")` (or any off-host base_url) raises `ValueError`; `selfhosted` to a public host without `FORGE_RERANK_ALLOW_INSECURE_URL=true` raises. *(no creds)*
8. **(offline)** **Cohere parity:** a mock transport asserts the Cohere provider posts `{"model":"rerank-v3.5","query","documents","top_n"}` to `/v2/rerank` with a `Bearer` header and parses `{"results":[{"index","relevance_score"}]}` identically to Jina. *(no creds)*
9. **(offline)** **Rank-delta metric:** given a fixture that reorders a known candidate list, the retriever exposes a rank-delta (mean |Δposition| > 0 and a monotonic-score flag) on its telemetry hook; given a fixture that preserves order, delta == 0. *(no creds)*
10. **(needs creds)** **Live reranker returns real scores:** `@pytest.mark.integration`, gated on `JINA_API_KEY` *or* `COHERE_API_KEY` (or reachable `JINA_RERANKER_URL`) — a real cross-encoder scores ≥3 candidate documents for a query; scores are floats in the provider's range and are **not** all equal (proves a learned model, not a constant). *(creds)*
11. **(needs creds)** **Live reorder + budget:** over a seeded candidate set where the lexically-best fused result is *not* the most relevant, the live reranker promotes the relevant doc above the fused top-1 **within `FORGE_RERANK_TIMEOUT_MS`**; latency is captured into `RerankTelemetry.latency_ms`. *(creds)*
12. **(needs creds)** **Live redaction + audit:** the live call emits exactly one redacted telemetry/audit event (provider, model, candidates, latency, `fallback_used=false`) and the BYOK key appears in **no** captured log/trace/audit/error. *(creds)*
13. **(needs creds, defers to HARD-04 for publication)** **Measured reranker delta hook:** the eval harness can run the same corpus with rerank ON vs OFF and produce a recall@k / nDCG@10 / MRR delta; this slice provides the toggle + delta computation, HARD-04 publishes the numbers. A green flag here is that the delta is *computable and finite* on the live path (not that it hits a specific value). *(creds for live; offline with fixture for wiring)*
14. **(offline)** **Whole-suite green gate:** `uv run pytest -q`, `uv run ruff check .`, `make typecheck`, `cd apps/web && pnpm test` all green with the integration lane skipped (creds absent); no real secret in source, fixtures, logs, or `uv.lock`. *(no creds)*

## 7. Test plan (TDD) — unit + integration + how to run

Write tests first. Unit tests extend `packages/knowledge-core/tests/test_reranker.py`, `test_retriever.py`, `test_service.py`; API tests extend `apps/api/tests/test_knowledge_api.py`.

**Unit (offline, hermetic — no network, no creds):**
- `test_graceful_reranker_satisfies_protocol` (AC1), `test_build_reranker_provider_defaults` (AC2), `test_build_reranker_fixture_and_disabled_make_no_client` (AC2).
- `test_fallback_to_weighted_rrf_on_503` and `test_fallback_on_timeout` via `httpx.MockTransport` (503) and a sleeping handler past `timeout_ms` (AC3, AC4) — assert weighted-RRF order, `rerank_score is None`, `fallback_used=True`, wall-clock bound.
- `test_rerank_disabled_equals_weighted_rrf` and `test_rerank_enabled_reorders_and_final_score` (AC5).
- `test_no_api_key_in_logs_or_errors` — `caplog` + forced 401/500 (AC6).
- `test_ssrf_guard_rejects_offhost_base_url` (AC7).
- `test_cohere_provider_payload_and_parsing` via mock transport (AC8).
- `test_rank_delta_positive_on_reorder` / `test_rank_delta_zero_on_identity` (AC9).
- Existing `test_jina_*` updated so non-2xx raises `RerankerUnavailableError` (was `httpx.HTTPStatusError`).

**Integration (gated on `-m integration` + creds present; skip-clean otherwise):**
- `test_live_reranker_returns_real_scores` (AC10), `test_live_reranker_reorders_within_budget` (AC11), `test_live_reranker_redacts_and_audits` (AC12) — each begins with `pytest.importorskip`-style guards: `pytest.mark.skipif(not (os.getenv("JINA_API_KEY") or os.getenv("COHERE_API_KEY") or _reranker_url_reachable()), reason="no reranker creds")`.
- `test_live_rerank_delta_computable` (AC13) — runs the corpus rerank ON vs OFF, asserts a finite delta dict.

**API (offline, DI-injected fakes):**
- `test_search_uses_configured_reranker_and_survives_outage` — override the service with a `GracefulReranker` over a 503 mock; assert 200 + weighted-RRF order + degradation badge in debug.

**How to run:**
```bash
# Hermetic default (no creds, no network) — must stay green:
uv run pytest packages/knowledge-core apps/api/tests/test_knowledge_api.py -q
uv run ruff check . && make typecheck && (cd apps/web && pnpm test)

# Live reranker lane (only with creds; skips clean otherwise):
export FORGE_RERANK_PROVIDER=jina          # or cohere / selfhosted
export JINA_API_KEY=...                     # from .env.integration (gitignored)
uv run pytest -m integration -k reranker -q
```

## 8. Security & policy considerations

- **BYOK secret handling (SPEC "Credentials & secrets handling").** The reranker key is read from env (integration lane) or the encrypted vault (`SecretVault`, `APIKeyKind.MODEL_PROVIDER`) at call time, sent only as the `Authorization: Bearer` header, and discarded. It is never a logged `Settings` field, never in `RerankTelemetry.reason` (which is run through the shared redaction filter), never in an exception message (the `raise_for_status()` → `RerankerUnavailableError` conversion strips the request), never in a fixture or `uv.lock`. Re-applies the shared redaction filter defensively (F05 §8 pattern).
- **SSRF.** `FORGE_RERANK_BASE_URL` / `JINA_RERANKER_URL` are user-controlled URLs the server fetches — a classic SSRF vector flagged in HARD-09's punch-list. Mitigation in `build_reranker`: hosted providers are pinned to their known host; `selfhosted` is restricted to loopback/private ranges unless `FORGE_RERANK_ALLOW_INSECURE_URL=true` is explicitly set. (AC7)
- **DoS bounds.** Candidate cap `FORGE_RERANK_CANDIDATES=50` (documents sent per call), a hard `FORGE_RERANK_TIMEOUT_MS` budget, and the fallback together bound the blast radius of a slow or hostile reranker. The reranker is never called at index time, so it cannot amplify ingest load.
- **Availability / fail-open-for-quality, fail-closed-for-secrets.** A reranker outage degrades *quality* (weighted-RRF) but never fails a search or an agent run — and never silently substitutes a fake on the integration lane (absent creds → the live test *skips*, it does not fall back to fixture under the integration marker).
- **Tenant isolation unchanged.** Reranking happens after the tenant-scoped legs; it only reorders an already workspace-filtered candidate set and adds no cross-tenant path.
- **Audit.** Each live rerank emits one observability/audit event (provider, model, candidate count, latency, `fallback_used`) with the query hashed (not raw) and no secret — feeding the FORGE_SPEC "reranker delta" + "retrieval latency p50/p95/p99" metrics.

## 9. Effort & risk (S/M/L + risks)

**Effort: M.** The provider client already exists and is provider-agnostic; the net-new work is the `GracefulReranker` wrapper + budget, the retriever fallback path, the factory + settings, the SSRF guard, the rank-delta metric, and the env/CI wiring — plus rewriting a handful of mock-transport tests to the new error type. No schema migration, no contract change, no new package.

Risks:
- **Provider schema drift.** Cohere v2 / Jina occasionally adjust fields. Mitigation: parsing is centralized in `JinaRerankerClient.rerank`; mock-transport tests pin the exact request/response shape per provider. (Low-Med)
- **Latency variance of hosted rerankers.** A hosted cross-encoder can spike well past 800 ms. Mitigation: the budget + fallback make a spike a quality event, not an outage; `FORGE_RERANK_TIMEOUT_MS` is tunable; self-hosted container is the deterministic-latency option. (Med)
- **The measured delta may be small or negative on the toy golden set.** That is the honest point of blocker #2 — a near-1.000 fixture leaves little headroom. Mitigation: HARD-04 runs the delta on a *real heterogeneous corpus* with the learned embedder; this slice only guarantees the delta is computable and the live path real. (Med — owned with HARD-04)
- **SSRF if the guard is too lax.** Mitigation: default-deny off-host; explicit insecure flag with a loud warning; covered by AC7 and HARD-09's enforcement matrix. (Med)

**Cannot be done in-sandbox (named):**
- The **VERIFIED-LIVE** criteria (AC10-13) require a real Jina/Cohere key or a reachable self-hosted reranker and outbound network — they run on the creds-bearing integration lane / a networked CI runner, **not** the no-network sandbox. They skip clean locally.
- The optional self-hosted reranker container's `docker compose build` + `@sha256` pin is **HARD-08**'s networked gate, not this slice.
- A 3rd-party human pentest of the SSRF/secret surface remains HARD-09's named punch-list item (agents produce the automated guard + tests, not the human pentest).

## 10. Key files / paths (exact, in the real monorepo)

- `packages/knowledge-core/forge_knowledge/reranker.py` — extend: `RerankerUnavailableError`, `RerankTelemetry`, `GracefulReranker`, `build_reranker`, `build_reranker_from_settings`; budget + typed errors on `JinaRerankerClient`.
- `packages/knowledge-core/forge_knowledge/retriever.py` — `HybridRetriever.rerank` graceful fallback + rank-delta hook + `_weighted_rrf_fallback`.
- `packages/knowledge-core/forge_knowledge/service.py` — `rerank_enabled` / `rerank_budget_ms` plumbing through `__init__` / `from_session_factory`.
- `packages/knowledge-core/forge_knowledge/__init__.py` — export the new public symbols.
- `packages/knowledge-core/tests/test_reranker.py`, `test_retriever.py`, `test_service.py` — unit + fallback + budget + SSRF + redaction + Cohere-parity + integration-gated tests.
- `apps/api/forge_api/routers/knowledge.py` — replace hardcoded `FixtureRerankerClient()` in `_knowledge_service_singleton()` with `build_reranker_from_settings(...)` (vault/env key resolution).
- `apps/api/forge_api/settings.py` — add `FORGE_RERANK_*` settings.
- `apps/api/tests/test_knowledge_api.py` — search-survives-outage API test.
- `apps/worker/forge_worker/indexer.py`, `apps/worker/forge_worker/syncer.py` — `build_knowledge_service()` uses the shared factory.
- `apps/api/forge_api/auth/vault.py` — consumed read-only (`get_secret`, `APIKeyKind.MODEL_PROVIDER`); no change required.
- `apps/web/components/knowledge/` + `apps/web/lib/api/knowledge.ts` — surface `rerank_score` / degradation badge / debug delta.
- `deploy/.env.example`, `deploy/.env.production.example`, `.env.integration.example` (repo root, committed; real `.env.integration` gitignored), optional `reranker` service in `deploy/docker-compose.yml`.
- `pyproject.toml` — `[tool.pytest.ini_options] markers` already has `integration`; no change.

## 11. Research references

- FORGE_SPEC: `docs/FORGE_SPEC.md` line 153 (Jina Reranker v2, 15-30% lift, self-hostable), line 408 (pipeline diagram: cross-encoder between RRF and top-k), line 924 (observability: "reranker delta", "retrieval latency p50/p95/p99"), line 1061 ("Retrieval is always hybrid ... and reranking").
- MORNING_REPORT: §1.3 (reranker DONE but Jina via `httpx.MockTransport`), §4 + honesty caveat (perfect 1.000s with fixture reranker), §5 item 4 (live model/reranker/embedding HTTP calls parked), §6 (provider/transport realism gap).
- SPEC-PRODUCTION-HARDENING: HARD-03 (real embedder + reranker), G-RAG-REAL gate, blockers #1/#2 mapping, Credentials & secrets handling rules.
- F05 slice: `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` §3.2 pipeline (rerank step), §6.5 (rerank disable → weighted-RRF), §8 (BYOK + redaction), §9 (reranker latency/degrade risk).
- Jina Reranker v2: https://jina.ai/reranker/ · Cohere Rerank v3.5: https://docs.cohere.com/docs/rerank · RRF (k=60): the F05 fusion contract.

## 12. Out of scope / future

- **Learned local embedder + `Vector(1536)` dimension reconciliation** — sibling slice **HARD-03-live-embedder** (the embedder is required for honest eval; the reranker is text-only and independent of it).
- **Published recall@k / MRR / nDCG@10 reranker-delta numbers and the regression floor** — **HARD-04** (this slice ships only the toggle + delta-computation hooks).
- **Self-hosted reranker container build + `@sha256` digest pin + healthcheck wiring in CI** — **HARD-08**.
- **ColBERT / token-level late-interaction reranking, recency-decay as an RRF leg** — future tuning once the real eval (HARD-04) guides it.
- **Reranker latency p50/p95/p99 at scale + a durable rerank-metrics table** — **HARD-13** (perf/soak).
- **Per-workspace reranker provider selection / cost accounting UI** — future; this slice is process-level config + per-request key resolution.
- **Human pentest of the SSRF/secret surface** — named punch-list item in **HARD-09**.
