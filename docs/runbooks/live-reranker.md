# Runbook — Live BYOK cross-encoder reranker (HARD-03)

Forge's hybrid retrieval pipeline (`/knowledge/search`, agent task-scoped
retrieval) fuses the pgvector semantic leg and the BM25 keyword leg with RRF,
then re-scores the survivors with a **cross-encoder reranker** — the last and
highest-leverage quality lever (`docs/FORGE_SPEC.md`: *Jina Reranker v2,
self-hosted, open-weight, 15-30% quality improvement*).

The reranker is a swappable implementation of the frozen
`forge_contracts.protocols.RerankerClient` (note: **synchronous**). Three ship:

- `FixtureRerankerClient` — the offline, deterministic, network-free default
  (token-overlap). Used by the whole hermetic test suite.
- `JinaRerankerClient` — the provider-agnostic BYOK HTTP client. It speaks the
  Jina Reranker v2 schema, which **Cohere v2 rerank** mirrors byte-for-byte, so
  the same client drives Jina, Cohere, or a self-hosted reranker by changing
  `base_url` / `path` / `model` alone.
- `GracefulReranker` — the production decorator: a hard **latency budget**,
  **graceful fallback** to weighted-RRF, and a redacted telemetry record.

This runbook covers turning the live path on. See the slice doc
`docs/implementation-slices/hardening/HARD-03-live-reranker.md` for the ACs.

---

## 1. When it activates

`FORGE_RERANK_PROVIDER` is the master switch. With it **`fixture`** (default) or
`FORGE_RERANK_ENABLED=false`, behavior is exactly today's — offline,
deterministic, network-free (fixture path, or weighted-RRF when disabled). Set a
real provider (plus a key or a self-hosted URL) and the API/worker build a live,
budgeted, SSRF-guarded reranker on next boot:

```
build_reranker_from_settings(get_settings(), api_key=…)   # forge_knowledge
  provider fixture / disabled  -> FixtureRerankerClient()          (no network)
  provider jina|cohere         -> GracefulReranker(JinaRerankerClient(...))
  provider selfhosted+URL      -> GracefulReranker(JinaRerankerClient(...))
```

The BYOK key is resolved **on demand** — from the per-workspace encrypted vault
(`SecretVault.get_secret`, `APIKeyKind.MODEL_PROVIDER`) in production, or from the
integration-lane env (`JINA_API_KEY` / `COHERE_API_KEY`) — handed straight to the
client as an `Authorization: Bearer` header, and never stored on the `Settings`
object, logged, or written to a trace/audit row.

**Fail-open-for-quality, fail-closed-for-secrets.** A slow, erroring, or
unhealthy reranker degrades a search to weighted-RRF (each result's
`rerank_score` becomes `null`, an operator-visible signal) — it never fails a
search or blocks an agent run. It also never silently substitutes the fixture on
the integration lane: absent creds, the live tests *skip*.

---

## 2. Configuration

Env vars (FastAPI `FORGE_` prefix, except the BYOK keys / self-hosted URL which
are read on demand so they never become logged settings fields):

| Var | Default | Meaning |
|---|---|---|
| `FORGE_RERANK_ENABLED` | `true` | master switch; `false` -> weighted-RRF only, no client built |
| `FORGE_RERANK_PROVIDER` | `fixture` | `fixture` \| `jina` \| `cohere` \| `selfhosted` |
| `FORGE_RERANK_MODEL` | provider default | e.g. `jina-reranker-v2-base-multilingual`, `rerank-v3.5` |
| `FORGE_RERANK_BASE_URL` | provider default | override; SSRF-validated |
| `FORGE_RERANK_TIMEOUT_MS` | `800` | per-call latency budget; exceed -> fallback |
| `FORGE_RERANK_CANDIDATES` | `50` | max docs sent to the reranker (DoS bound) |
| `FORGE_RERANK_ALLOW_INSECURE_URL` | `false` | required to point `selfhosted` at a non-private host |
| `JINA_API_KEY` / `COHERE_API_KEY` | unset | BYOK key (integration lane; prod resolves from vault) |
| `JINA_RERANKER_URL` | unset | self-hosted reranker base URL (e.g. `http://reranker:8080`) |

Provider defaults built into `build_reranker`:

| provider | base_url | path | model |
|---|---|---|---|
| `jina` | `https://api.jina.ai/v1` | `/rerank` | `jina-reranker-v2-base-multilingual` |
| `cohere` | `https://api.cohere.com` | `/v2/rerank` | `rerank-v3.5` |
| `selfhosted` | `$JINA_RERANKER_URL` | `/rerank` | `jina-reranker-v2-base-multilingual` |

Copy the template and fill in a real key. `.env.integration` is gitignored.

```bash
cp .env.integration.example .env.integration
# edit .env.integration:
#   FORGE_RERANK_PROVIDER=jina
#   JINA_API_KEY=jina_…              (or COHERE_API_KEY + FORGE_RERANK_PROVIDER=cohere)
```

---

## 3. Run the live lane

```bash
set -a; source .env.integration; set +a        # export the BYOK creds
export FORGE_RERANK_PROVIDER=jina               # or cohere / selfhosted

# Live reranker lane (creds-gated; skips cleanly without creds/endpoint):
uv run pytest -m live_rerank -k reranker -q
```

The live tests (`packages/knowledge-core/tests/test_reranker_live.py`) assert:

- **AC10** — a real cross-encoder scores ≥3 candidates; scores are floats and are
  **not all equal** (proves a learned model, not a constant / the fixture).
- **AC11** — over a seeded set where the lexically-obvious doc is *not* the most
  relevant, the reranker promotes the relevant doc above the decoy **within
  `FORGE_RERANK_TIMEOUT_MS`**; latency lands in `RerankTelemetry.latency_ms`.
- **AC12** — exactly one non-fallback telemetry record is emitted and the BYOK
  key appears in **no** captured log / telemetry / result payload.
- **AC13** — the same corpus reranked ON vs OFF yields a finite, computable delta
  (rank-shift here; HARD-04 publishes the recall@k / nDCG@10 / MRR numbers).

The default lane stays hermetic and network-free:

```bash
uv run pytest -q -m "not live_rerank and not integration"
```

---

## 4. Optional: self-hosted, open-weight reranker

For a deterministic-latency, no-BYOK-cost path (FORGE_SPEC's *self-hosted,
open-weight*), run `jinaai/jina-reranker-v2-base-multilingual` on an internal
network reachable only by `api`/`worker` (the Helm chart already ships a gated
`reranker` service — `reranker.enabled=true`), and point Forge at it:

```bash
export FORGE_RERANK_PROVIDER=selfhosted
export JINA_RERANKER_URL=http://reranker:8080      # internal, never Caddy-exposed
```

The container build + `@sha256` digest pin + CI healthcheck wiring is **HARD-08**'s
networked gate, not this slice.

---

## 5. Security notes

- **SSRF.** `FORGE_RERANK_BASE_URL` / `JINA_RERANKER_URL` are admin-controlled
  URLs the server fetches. `build_reranker` pins hosted providers to their known
  host (`api.jina.ai` / `api.cohere.com`) and restricts `selfhosted` to
  loopback/RFC1918/private-DNS unless `FORGE_RERANK_ALLOW_INSECURE_URL=true`. The
  cloud metadata endpoint (`169.254.169.254`) is always rejected. The api/worker
  additionally inject `forge_api.security.ssrf.assert_safe_url` as the
  DNS-resolving authoritative control (defense-in-depth). (AC7)
- **DoS bounds.** `FORGE_RERANK_CANDIDATES=50` caps documents per call, the hard
  `FORGE_RERANK_TIMEOUT_MS` budget bounds latency, and the reranker is never
  called at index time, so a slow/hostile reranker cannot amplify ingest load.
- **Redaction.** A non-2xx / transport error becomes `RerankerUnavailableError`
  whose message is run through `forge_knowledge.redaction.redact_secrets`;
  `RerankTelemetry.reason` is likewise redacted and never carries the raw query
  or a secret. (AC6, AC12)

---

## 6. PARKED — live verification (needs a real BYOK key or a reachable reranker)

The **VERIFIED-LIVE** criteria (AC10-AC13) cannot be closed in the no-network
sandbox: they need a real Jina/Cohere key **or** a reachable self-hosted reranker
and outbound network. They skip clean locally. Run the exact command below on a
networked runner with a credential configured to close them:

```bash
set -a; source .env.integration; set +a
export FORGE_RERANK_PROVIDER=jina        # or cohere / selfhosted (+ JINA_RERANKER_URL)
uv run pytest -m live_rerank -k reranker -q
```
