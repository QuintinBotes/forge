# HARD-11 — Reliability & Maturity (coverage, migration rollback, load/perf, soak, graceful shutdown, rate limiting, idempotency)

> Phase: hardening · Blocker(s): **#6** (maturity gaps: low coverage on worker/agent-runtime, no load/perf, no migration upgrade/rollback testing, no multi-tenant soak) · Status target: **VERIFIED** when (a) `apps/worker` and `packages/agent-runtime` each reach **≥ 90%** coverage with error/retry/escalation/cleanup/worktree paths named-tested; (b) Alembic `upgrade head → downgrade base → upgrade head` plus a per-revision step-down walk run green **on live pgvector** and a populated-DB single-step rollback is proven **data-preserving**; (c) a k6/locust load harness + a retrieval-latency micro-bench publish p50/p95/p99 against documented budgets; (d) a bounded multi-tenant soak runs with **zero cross-tenant leak** and bounded memory/FDs/connections; (e) graceful shutdown, rate limiting, and idempotency are implemented, default-on, and unit-tested. **No external creds required.** Parts (b)/(c)/(d) require a **live Postgres + a resourced/networked runner** (testcontainers or CI service); they **skip cleanly** in the no-network sandbox so the hermetic suite stays green.

> **Relationship to `SPEC-PRODUCTION-HARDENING.md`.** This slice is the consolidated *maturity* workstream for blocker #6. It delivers the spec's gates **G-COVERAGE** (worker/agent-runtime ≥ 90%), **G-MIGRATE** (populated upgrade→rollback→re-upgrade), **G-PERF** (retrieval p50/p95/p99 + API hot-path load), and **G-SOAK** (bounded multi-tenant soak) — i.e. the maturity halves the spec sketched under its HARD-12/HARD-13 — **and adds three platform reliability primitives** the spec did not yet own as a slice: graceful shutdown, HTTP rate limiting, and request/task idempotency. The whole-workspace **typecheck** half of the spec's HARD-12 (`make typecheck` green; the "source file found twice" bug) is **out of scope here** and stays its own slice. This slice depends on **HARD-01** (real pgvector substrate) for the migration-on-PG, perf, and soak gates and on **HARD-03b** (local `sentence-transformers` embedder, no API key) for honest retrieval-latency numbers.

---

## 1. Intent — what & why

The ALPHA stands the platform up and is 96% covered overall, but the maturity that lets a serious team *operate* Forge is missing, and `MORNING_REPORT.md` says so plainly:

- **§3 / §6 — the two least-covered units are the most side-effecty.** `apps/worker` (78.9%) and `packages/agent-runtime` (82.4%) are exactly the Celery tasks, the git-worktree sandbox, and the plan→act→observe loop where untested error/retry/escalation/cleanup branches hide. The report calls this out: *"the most 'side-effecty' code … is the least covered; that's where untested error paths most likely hide."*
- **§5(1) / §6 — no real database was ever exercised.** Migrations only ran upgrade/downgrade on SQLite. There is no proof that `downgrade` works on Postgres, no per-revision rollback walk, and no proof a rollback **preserves data** on a populated DB. The report's #1 ranked next step is to run the real migration round-trip on pgvector.
- **§6 — no load/perf and no soak.** The FORGE_SPEC promises a "retrieval latency p50/p95/p99" metric; today there is no harness that measures it on a real embedder + pgvector, no API hot-path load test, and no multi-tenant soak proving tenant isolation and resource stability under sustained mixed load.
- **Missing reliability primitives.** There is **no graceful shutdown** (the API `create_app` has no `lifespan`; Celery has no `acks_late`/drain config — a SIGTERM mid-task can lose or double-run work), **no HTTP rate limiting** (every route is unthrottled — a DoS and cost vector, flagged in the FORGE_SPEC Security table and the HARD-09 pentest punch-list), and **no generic request idempotency** (webhook dedup exists for incidents/alerts/PM, and knowledge sync is content-hash idempotent, but a client retry of `POST /knowledge/{source}/sync` or an agent-run enqueue double-fires).

HARD-11 closes blocker #6 by turning each of these from "asserted on paper / unit-logic only" into "exercised on real Postgres + a real load/soak run, with published evidence", and by adding the three reliability primitives a self-hosted operator needs to run Forge without losing or duplicating work. It **extends** the existing `forge_*` packages — no new packages — conforms to the real `forge_db` schema (singular/enum tables) and frozen `forge_contracts`, and keeps the default hermetic suite green and network-free by gating every resource-heavy test behind a marker.

## 2. User-facing / operator behavior

HARD-11 is mostly operator- and SRE-facing; the few user-visible effects are correctness/availability improvements, not new product surface.

- **Operator journey A — Safe deploy/rollback.** An operator follows `docs/self-hosting/upgrade.md`: `alembic upgrade head`, observe, and (if a release misbehaves) `alembic downgrade -1` with a documented, **data-preserving** rollback. The runbook states exactly which revisions are reversible, which require a backup-first, and how to verify row counts before/after.
- **Operator journey B — Zero-downtime restart.** On `docker compose up -d --force-recreate api worker` (or a rolling restart), the API stops accepting new traffic the instant it receives SIGTERM (readiness probe `/health/ready` flips to `503`), drains in-flight requests within the grace period, and closes DB/Redis pools cleanly; the worker finishes (or re-queues) the task it is running instead of dropping it. No 502s, no half-applied agent runs.
- **Operator journey C — Abuse / cost protection.** A misbehaving client (or a runaway script) hammering `POST /knowledge/search` gets `429 Too Many Requests` with `Retry-After` and `X-RateLimit-Remaining: 0` headers once it exceeds its workspace/key budget, instead of degrading the whole instance. Limits are configurable per deployment and per hot route.
- **User journey D — Retry without double-effect.** A user (or the web app on a flaky connection) retries `POST /knowledge/{source_id}/sync` or an agent-run enqueue with the same `Idempotency-Key`; the second call returns the **same** `sync_run_id`/run id and response body it got the first time, and the work runs **once**. No duplicate sync runs, no duplicate agent runs.
- **Maintainer journey E — Published maturity evidence.** A maintainer reads `docs/self-hosting/performance.md` (retrieval p50/p95/p99 at corpus size N, API hot-path throughput/latency) and the soak report (N tenants, duration, zero cross-tenant leak, flat memory/FD/connection curves) before quoting Forge as production-ready. CI re-runs the load *smoke* on every PR so a regression is caught early.

## 3. Vertical slice

### 3.1 Data model

**No new tables are required for the default path** — and that is deliberate, to honor "conform to the real `forge_db` schema, no drift". The reliability primitives use Redis (already a first-class dependency: `FORGE_REDIS_URL`) as their default backing store:

- **Idempotency store (default = Redis).** `Idempotency-Key` → `{request_fingerprint, status_code, response_body, created_at}` with a TTL (`FORGE_IDEMPOTENCY_TTL_SECONDS`, default 24h). Keyed `forge:idem:{workspace_id}:{key}`; written with `SET … NX EX` so concurrent retries collapse to one in-flight slot.
- **Rate-limit store (default = Redis).** Token-bucket / fixed-window counters at `forge:rl:{scope}:{window}`; in-process fallback (`FORGE_RATE_LIMIT_BACKEND=memory`) for single-instance/dev so the unit suite needs no Redis.
- **Worker task-dedup store (Redis).** `forge:task:dedup:{task_name}:{idem_key}` SETNX guard so a re-delivered Celery message is a no-op.

**Optional durable idempotency (future, behind a migration).** For deployments that want idempotency to survive a Redis flush, the slice documents an *optional* new migration `packages/db/migrations/versions/0003_idempotency.py` creating a **singular** `idempotency_record` table (`id uuid pk`, `workspace_id uuid fk→workspace`, `idem_key text`, `method text`, `path text`, `request_hash text`, `status_code int`, `response_body jsonb`, `created_at`/`expires_at timestamptz`, `unique(workspace_id, idem_key)`), enum columns rendered `Enum(native_enum=False)` per the workspace convention. This is **out of scope for the BETA path** (Redis is sufficient and schema-neutral) and listed in §12; if built, it chains after `0002_pm_adapters` and is exercised by the §3.5 migration walk like any other revision.

**Migration testing touches the existing chain, not the schema.** The Alembic gates run `0001_baseline → 0002_pm_adapters` (and any later revisions) forward and backward; no model changes are introduced by HARD-11.

### 3.2 Backend (`apps/api` / `forge_api`)

New, small, composable middleware under a new `apps/api/forge_api/middleware/` subpackage (extends `forge_api`, no new top-level package):

```
apps/api/forge_api/middleware/
├── __init__.py          # install_middleware(app, settings) — one call from create_app
├── ratelimit.py         # RateLimitMiddleware + RateLimiter (redis|memory backends)
├── idempotency.py       # IdempotencyMiddleware + IdempotencyStore
└── shutdown.py          # ShutdownState (readiness flag) + lifespan() context manager
```

- **`create_app` wiring.** `forge_api/main.py` gains `lifespan=lifespan` on the `FastAPI(...)` call and a single `install_middleware(app, cfg)` line after the routers are mounted. Order matters: idempotency outermost (so a replay short-circuits before rate-limit consumption), then rate-limit, then the existing CORS. Everything is **default-on** but no-ops cleanly when its backend is unavailable in dev/test.
- **Rate limiting.** `RateLimitMiddleware` resolves a *scope key* in priority order: authenticated API-key id → `workspace_id` (from the auth dependency) → client IP. A fixed-window/token-bucket counter (Redis `INCR`+`EXPIRE`, or in-process `dict` for `memory` backend) enforces `FORGE_RATE_LIMIT_DEFAULT` (e.g. `"120/minute"`) with **per-route overrides** for the expensive hot paths (`/knowledge/search`, `/knowledge/retrieve`, agent-run enqueue, `/index`, `/sync`). On breach: `429` + `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`. Health/liveness routes (`/health`, `/health/ready`, `/`) are **never** limited.
- **Idempotency.** `IdempotencyMiddleware` activates only for unsafe methods (`POST`/`PUT`/`PATCH`/`DELETE`) carrying an `Idempotency-Key` header. It computes a `request_hash` over `(method, path, workspace_id, body)`; on first sight it runs the handler, stores `(status, body)` under the key with TTL, and returns it; on replay it returns the stored response with `Idempotency-Replayed: true`. A **fingerprint mismatch** (same key, different body) returns `422` (key reuse with a different payload is a client bug). Concurrent first-and-replay collapse via Redis `SET NX`; the loser polls briefly then returns the stored response.
- **Readiness vs liveness.** `forge_api/routers/health.py` is extended: `/health` (liveness) **always** returns `200` while the process is up; new **`/health/ready`** (readiness) returns `200` only when `ShutdownState.is_serving` is true **and** a fast DB ping (`SELECT 1`) and Redis ping succeed; it returns `503` the moment SIGTERM is received so a load balancer drains the instance before the process exits.
- **Graceful shutdown (`lifespan`).** On startup: create the DB engine/session-factory and Redis pool, set `ShutdownState.is_serving=True`. On shutdown: flip `is_serving=False` (readiness → 503), wait up to `FORGE_SHUTDOWN_DRAIN_SECONDS` for in-flight requests to finish, then dispose the DB engine, close the Redis pool, and flush the OTel/audit exporters (`forge_api.observability`).

### 3.3 Worker / agent runtime (`apps/worker` / `forge_worker`, `packages/agent-runtime` / `forge_agent`)

- **Celery reliability config (extends `forge_worker/celery_app.py`).** Add, driven from env so dev stays untouched:
  - `task_acks_late = True` + `task_reject_on_worker_lost = True` — a task interrupted by SIGTERM/OOM is re-queued, not lost (paired with idempotent tasks so re-run is safe).
  - `worker_prefetch_multiplier` (`FORGE_WORKER_PREFETCH_MULTIPLIER`, default `1`) — fair dispatch, bounded in-flight work.
  - `worker_max_tasks_per_child` (`FORGE_WORKER_MAX_TASKS_PER_CHILD`, default `200`) — bounds memory growth (soak gate relies on this).
  - `task_soft_time_limit` / `task_time_limit` (`FORGE_TASK_SOFT_TIME_LIMIT` / `FORGE_TASK_TIME_LIMIT`) — a runaway task raises `SoftTimeLimitExceeded` (caught → graceful escalate) then is hard-killed.
  - `task_default_retry_delay` + a shared **`ForgeTask` base** (`forge_worker/reliability.py`) setting `autoretry_for=(TransientError,)`, `retry_backoff=True`, `retry_jitter=True`, `max_retries=FORGE_TASK_MAX_RETRIES` (default `3`) — so transient failures (DB/Redis blip) retry with exponential backoff while deterministic failures surface immediately.
- **Idempotent tasks.** `index_source_task`, `sync_source_task`, `run_agent_task`, and the incident tasks gain an optional `idempotency_key` arg; the `ForgeTask` base does a Redis `SETNX` dedup so a re-delivered message returns the prior result instead of re-running. Knowledge index/sync are already content-hash idempotent (`MORNING_REPORT §1.3/§1.4`); this adds *enqueue-level* dedup on top. The agent runner gains a dedup keyed on the objective's `task_id` so a retried enqueue does not start a second run.
- **Worker graceful drain.** Document and wire warm shutdown (`celery worker` traps SIGTERM → stop prefetch → finish running tasks → exit) and the compose `stop_grace_period`/`STOPSIGNAL` to match.
- **Coverage targets (the bulk of the test work).** Hermetic tests (no network, no live model, no git where avoidable) added to lift the two units to ≥ 90%:
  - `apps/worker/tests/` — `run_agent_task` happy + error/exception result mapping; `build_agent_runner`/`build_knowledge_service` factory wiring; the `sync_source_task` `ValueError("requires either 'files' or 'root'")` branch; `index_source_task` failure path; `ForgeTask` retry/backoff and `SoftTimeLimitExceeded` handling (with an eager-mode Celery app + a raising fake); idempotent re-enqueue dedup (fake Redis); the incident task error/retry paths.
  - `packages/agent-runtime/tests/` — extend `test_runtime_hardening.py` and add coverage for `tools.py` (dispatch error, unknown tool, `action_for`), `policy_gate.py` (allow/deny/requires-approval branches), `context.py` (`build_system_prompt` with/without `agents_md`), and a **real-git** `WorktreeSandbox` create→write→cleanup and cleanup-after-`create`-failure test (git is available locally; the existing suite already monkeypatches the sandbox, so add the real-git path to cover `sandbox.py`'s `_git` error branches). Cover `graph.py`'s `GraphError` construction branches and the `recursion_limit → GraphError` runaway path (the LangGraph backstop).
- **Per-package coverage gate.** A `--cov=forge_worker --cov-fail-under=90` and `--cov=forge_agent --cov-fail-under=90` invocation (plus the existing overall gate) wired into the worker/agent-runtime test runs and CI.

### 3.4 Frontend (`apps/web`)

Minimal. The web client (`apps/web/src/lib/api/*` / TanStack Query mutations) is updated to:

- Send an `Idempotency-Key` (a generated UUID v4 held for the lifetime of a mutation) on retry-prone mutations (sync, index, agent-run enqueue, approval actions) so a network retry is safe.
- Surface `429` responses to the user with a "rate limited, retrying after N s" toast that honors `Retry-After`, and back off automatically (TanStack Query `retry`/`retryDelay`).
- No new pages/components; only the API client + mutation hooks change, covered by the existing Vitest suite (extend, keep the 28+ green).

### 3.5 Infra / deploy / CI

- **Migration round-trip + walk (extends `packages/db/tests/test_migration.py`).** Add `@pytest.mark.postgres` tests using the root `pg_engine` fixture:
  1. `test_full_roundtrip_on_postgres` — `command.upgrade(cfg, "head")` → `downgrade(cfg, "base")` → `upgrade(cfg, "head")` against live pgvector, asserting the `EXPECTED_TABLES` (and PM tables) appear/disappear correctly, including `CREATE EXTENSION vector` and pgvector/`tsvector` columns that SQLite cannot represent.
  2. `test_stepwise_revision_walk` — iterate `ScriptDirectory.walk_revisions()`; for each revision upgrade to it then downgrade to its `down_revision`, proving **every** revision is individually reversible on Postgres.
  3. `test_rollback_is_data_preserving` — `upgrade head`, seed a known workspace + a few rows via `forge_db` models, `downgrade -1` then `upgrade head` again, and assert the seeded rows survive (for revisions that touch only additive/independent tables; revisions that are intentionally destructive are asserted to require a backup and are flagged in the runbook).
  A small helper `packages/db/forge_db/migration_utils.py` exposes `iter_revisions()` and `revision_pairs()` so the walk is reusable by CI and ops scripts.
- **Load/perf harness (new `deploy/load/`).**
  - `deploy/load/k6/api_hotpaths.js` — k6 script driving `/health`, `/board`, `/knowledge/search`, `/knowledge/retrieve`, agent-run enqueue at a configurable VU/duration, asserting p95/error-rate thresholds (`thresholds: { http_req_duration: ['p(95)<...'], http_req_failed: ['rate<0.01'] }`).
  - `deploy/load/locustfile.py` — equivalent Locust user classes (read-heavy + write-heavy mixes) for operators who prefer Python.
  - `packages/evaluation/forge_eval/perf/retrieval_latency.py` — a pytest-driven micro-bench (`@pytest.mark.perf`) that indexes a corpus of size `FORGE_PERF_CORPUS_SIZE` (default N=10k chunks) with the **local `sentence-transformers` embedder (HARD-03b, no API key)** into live pgvector and measures embed/semantic/keyword/fusion/rerank/total **p50/p95/p99**, writing a JSON report to `deploy/load/reports/retrieval_latency.json`. Budgets live in `deploy/load/budgets.toml`; the bench fails if p95 exceeds budget.
  - `make load-smoke` — a tiny k6 run (few VUs, short duration) usable in CI as a **non-blocking smoke**; the full perf run is a networked/manual gate.
- **Multi-tenant soak (new `packages/evaluation/forge_eval/soak/`).** `soak_runner.py` (`@pytest.mark.soak`) spins up **N tenants** (distinct `workspace_id`s) against live pgvector + Redis and drives a mixed workload (search/retrieve reads, board writes, index/sync, agent-run enqueue) for `FORGE_SOAK_DURATION_SECONDS`. It asserts: (a) **zero cross-tenant leak** — every result row's `workspace_id` matches the querying tenant (row-id assertions on identical seeded content across two tenants); (b) **bounded resources** — sample RSS, open FDs, and DB connection count at intervals and assert no monotonic growth beyond a slack threshold; (c) **no error cliff** under the target rate. Output → `deploy/load/reports/soak_report.json` + a human summary appended to `docs/self-hosting/performance.md`.
- **Compose (extends `deploy/docker-compose.yml`).** Per FORGE_SPEC "Production Docker Compose Requirements": add `stop_grace_period` (≥ `FORGE_SHUTDOWN_DRAIN_SECONDS` + slack) to `api`/`worker`, ensure `healthcheck` for `api` points at `/health/ready`, and confirm CPU/memory `deploy.resources.limits` + non-root user (the soak's resource bounds are meaningful only with limits set). Image-digest pinning itself is owned by a separate build slice; HARD-11 only adds the lifecycle/limits knobs it needs.
- **CI (extends `.github/workflows/ci.yml`).** (1) Run the existing pgvector service job with the new `@pytest.mark.postgres` migration tests **un-skipped**; (2) add a `coverage` step enforcing the per-package floors; (3) add a `load-smoke` job (k6) that is **non-blocking** (visible, not gating) so the harness is exercised without flaking PRs. The resource-heavy `@pytest.mark.perf`/`@pytest.mark.soak` lanes are documented as manual/nightly, not per-PR.

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**New pytest markers** (registered in root `pyproject.toml [tool.pytest.ini_options].markers`, alongside the existing `postgres` and `integration`):

```toml
"perf: resource-heavy latency/throughput benchmark (skips without a runner + live PG)",
"soak: long-running multi-tenant isolation/resource-stability run (skips by default)",
```

**`forge_api/middleware/shutdown.py`:**

```python
class ShutdownState:
    is_serving: bool                       # flips False on SIGTERM/lifespan-shutdown
    def begin_drain(self) -> None: ...

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: open DB engine + Redis pool, mark serving.
    Shutdown: stop serving (readiness→503), drain up to FORGE_SHUTDOWN_DRAIN_SECONDS,
    dispose engine, close Redis, flush OTel/audit exporters."""
```

**`forge_api/middleware/ratelimit.py`:**

```python
class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_s: int) -> RateLimitResult: ...

class RateLimitResult(BaseModel):
    allowed: bool; remaining: int; reset_s: int; retry_after_s: int | None

class RateLimitMiddleware:                 # ASGI middleware
    def __init__(self, app, *, limiter: RateLimiter, default: str,
                 overrides: dict[str, str], exempt_paths: set[str]) -> None: ...
```

**`forge_api/middleware/idempotency.py`:**

```python
class IdempotencyStore(Protocol):
    async def get(self, key: str) -> StoredResponse | None: ...
    async def put_if_absent(self, key: str, value: StoredResponse, ttl_s: int) -> bool: ...

class StoredResponse(BaseModel):
    request_hash: str; status_code: int; body: bytes; created_at: datetime

class IdempotencyMiddleware:               # ASGI middleware (unsafe methods only)
    def __init__(self, app, *, store: IdempotencyStore, ttl_s: int,
                 methods: frozenset[str]) -> None: ...
```

**`forge_api/middleware/__init__.py`:**

```python
def install_middleware(app: FastAPI, settings: Settings) -> None:
    """Install idempotency (outer) → rate-limit → (existing CORS) in that order,
    each default-on but no-op when its backend/feature flag is disabled."""
```

**`forge_worker/reliability.py`:**

```python
class TransientError(Exception): ...        # retryable (DB/Redis/network blip)

class ForgeTask(celery.Task):
    autoretry_for = (TransientError,)
    retry_backoff = True; retry_jitter = True
    max_retries = ...                        # from FORGE_TASK_MAX_RETRIES
    acks_late = True
    def is_duplicate(self, idem_key: str) -> bool: ...   # Redis SETNX dedup

def configure_reliability(app: celery.Celery, settings: WorkerSettings) -> None:
    """Apply acks_late, prefetch, max-tasks-per-child, soft/hard time limits."""
```

Task signatures extend with a keyword-only, optional `idempotency_key: str | None = None` (backward compatible):
`run_agent_task(objective, *, idempotency_key=None)`, `index_source_task(source_id, files, *, idempotency_key=None)`, `sync_source_task(source_id, *, ..., idempotency_key=None)`.

**`packages/db/forge_db/migration_utils.py`:**

```python
def iter_revisions(cfg: alembic.config.Config) -> list[str]: ...      # base→head order
def revision_pairs(cfg) -> list[tuple[str, str | None]]: ...          # (rev, down_revision)
```

**`packages/evaluation/forge_eval/perf/retrieval_latency.py`:**

```python
def measure_retrieval_latency(*, corpus_size: int, queries: int,
                              embedder, store, reranker) -> LatencyReport: ...

class LatencyReport(BaseModel):
    corpus_size: int
    stages: dict[str, Percentiles]          # embed/semantic/keyword/fusion/rerank/total
    class Percentiles(BaseModel): p50: float; p95: float; p99: float
```

**`packages/evaluation/forge_eval/soak/soak_runner.py`:**

```python
def run_soak(*, tenants: int, duration_s: int, mix: WorkloadMix,
             store, redis) -> SoakReport: ...

class SoakReport(BaseModel):
    tenants: int; duration_s: int; requests: int; errors: int
    cross_tenant_leaks: int                  # MUST be 0 to pass
    rss_mb_samples: list[float]; fd_samples: list[int]; db_conn_samples: list[int]
    resource_stable: bool
```

**New env vars / config keys** (all `FORGE_`-prefixed; sensible defaults so dev/test are unaffected):

| Key | Default | Purpose |
|---|---|---|
| `FORGE_RATE_LIMIT_ENABLED` | `true` | master switch |
| `FORGE_RATE_LIMIT_DEFAULT` | `"120/minute"` | default per-scope budget |
| `FORGE_RATE_LIMIT_BACKEND` | `redis` | `redis` \| `memory` (memory = dev/test) |
| `FORGE_RATE_LIMIT_OVERRIDES` | `{}` | JSON `{route: "N/window"}` for hot paths |
| `FORGE_IDEMPOTENCY_ENABLED` | `true` | master switch |
| `FORGE_IDEMPOTENCY_TTL_SECONDS` | `86400` | replay window |
| `FORGE_SHUTDOWN_DRAIN_SECONDS` | `30` | request drain grace |
| `FORGE_WORKER_PREFETCH_MULTIPLIER` | `1` | bounded in-flight work |
| `FORGE_WORKER_MAX_TASKS_PER_CHILD` | `200` | bounds worker memory growth |
| `FORGE_TASK_SOFT_TIME_LIMIT` / `FORGE_TASK_TIME_LIMIT` | `300` / `360` | runaway-task limits (s) |
| `FORGE_TASK_MAX_RETRIES` | `3` | transient retry budget |
| `FORGE_TASK_RETRY_BACKOFF` | `true` | exponential backoff + jitter |
| `FORGE_PERF_CORPUS_SIZE` | `10000` | perf-bench corpus N |
| `FORGE_SOAK_TENANTS` / `FORGE_SOAK_DURATION_SECONDS` | `5` / `900` | soak shape (bounded) |
| `FORGE_TEST_DATABASE_URL` | unset | live pgvector for `-m postgres`/`perf`/`soak` (else skip) |

## 5. Dependencies (other slices/foundation that must exist first)

- **HARD-01 — Real Postgres + pgvector substrate (REQUIRED).** The migration round-trip/walk/data-preservation tests, the retrieval-latency bench, and the soak all run against live pgvector using the root `conftest.py` `pg_engine`/`postgres_url` fixtures (testcontainers `pgvector/pgvector:pg16` or `FORGE_TEST_DATABASE_URL`). Without HARD-01 those gates `skip` (suite stays green) and cannot be marked done.
- **HARD-03b — Local `sentence-transformers` embedder (REQUIRED for honest perf/soak numbers).** The perf bench measures latency on a *learned* embedder, not the deterministic fake; no API key required. If only the fake is available the bench runs but its numbers are labeled "wiring-only" (same honesty rule as HARD-04).
- **Foundation — `forge_db` schema + migrations (`0001_baseline`, `0002_pm_adapters`)** and the frozen `forge_contracts` DTOs/Protocols (`AgentObjective`, `AgentRunResult`, `IndexResult`, `KnowledgeStore`, …) the worker/agent tests build against — already present.
- **Foundation — Redis (`FORGE_REDIS_URL`)** for the rate-limit/idempotency/task-dedup default backends; already a workspace dependency (worker broker + API settings). The `memory` backend keeps unit tests Redis-free.
- **HARD-09 — Security audit (SOFT, complementary).** Rate limiting is one line item in the HARD-09 enforcement matrix / pentest punch-list; HARD-11 supplies the implementation HARD-09 asserts.
- **Independent of:** the typecheck slice (separate), the real external integrations (HARD-02/05/06/07 — none needed here), and the build/digest-pin slice (HARD-08 — HARD-11 only adds compose lifecycle/limit knobs).

## 6. Acceptance criteria (numbered, testable; creds/runner needs marked)

> **None require external creds.** Tag legend: **[offline]** runs in the hermetic sandbox; **[live-PG]** needs a Postgres (testcontainers/CI service); **[runner]** needs a resourced/networked runner and **cannot run in the no-network sandbox**.

1. **[offline]** `apps/worker` coverage **≥ 90%** and `packages/agent-runtime` coverage **≥ 90%**, each enforced by `--cov-fail-under=90`; overall workspace coverage does not regress below the agreed band (≥ 93%).
2. **[offline]** Named hermetic tests exist and pass for: Celery task failure→retry/backoff, `SoftTimeLimitExceeded` handling, escalation/handoff on low confidence + on policy-deny + on tool-failure-after-retries, git-worktree sandbox cleanup on success **and** on exception (real-git path), and policy-denied tool dispatch — covering the branches `MORNING_REPORT §6` flags as untested.
3. **[live-PG]** `alembic upgrade head → downgrade base → upgrade head` runs clean on live pgvector (`CREATE EXTENSION vector`, pgvector/`tsvector` columns included); the per-revision `walk_revisions` step-down proves **every** revision is individually reversible.
4. **[live-PG]** A populated-DB single-step rollback (`upgrade head` → seed rows → `downgrade -1` → `upgrade head`) is **data-preserving** for additive revisions; intentionally destructive revisions (if any) are asserted to require a backup and are documented as such in `docs/self-hosting/upgrade.md`.
5. **[offline]** Graceful shutdown: `/health` (liveness) stays `200` while up; `/health/ready` returns `503` once `ShutdownState.begin_drain()` is called; the `lifespan` shutdown disposes the DB engine and closes the Redis pool (asserted via spies). Compose `stop_grace_period` ≥ `FORGE_SHUTDOWN_DRAIN_SECONDS` validated by `docker compose config`.
6. **[offline]** Rate limiting: with `memory` backend, the (N+1)-th request within a window returns `429` + `Retry-After` + `X-RateLimit-*` headers; health/liveness routes are never limited; per-route overrides apply to `/knowledge/search`/`/retrieve`; disabling via `FORGE_RATE_LIMIT_ENABLED=false` is a clean no-op.
7. **[offline]** Idempotency: two `POST`s with the same `Idempotency-Key` + same body run the handler **once** and return identical responses (second carries `Idempotency-Replayed: true`); same key + different body returns `422`; the worker `ForgeTask.is_duplicate` SETNX dedup prevents a re-delivered task from re-running (fake Redis).
8. **[runner]** Retrieval-latency bench publishes embed/semantic/keyword/fusion/rerank/total **p50/p95/p99** at corpus size N on the local embedder + live pgvector to `deploy/load/reports/retrieval_latency.json`; p95 within `deploy/load/budgets.toml`; numbers documented in `docs/self-hosting/performance.md` (satisfies the FORGE_SPEC "retrieval latency p50/p95/p99" metric).
9. **[runner]** k6/Locust API hot-path load run meets the documented throughput + p95 latency budgets with `http_req_failed` rate < 1% at target concurrency (no error cliff); the `make load-smoke` job runs green in CI as a non-blocking smoke.
10. **[runner]** Bounded multi-tenant soak (`FORGE_SOAK_TENANTS` tenants, `FORGE_SOAK_DURATION_SECONDS`) reports `cross_tenant_leaks == 0` (row-id assertions on identical content across tenants), `resource_stable == true` (RSS/FD/DB-connection samples show no unbounded growth), and no error cliff; `soak_report.json` + summary published.
11. **[offline]** Every `[live-PG]`/`[runner]` test **skips with a clear reason** when its DB/runner is absent, so `uv run pytest -q` stays green and network-free in the sandbox; no fake is silently substituted on the gated lanes.
12. **[offline]** Whole-suite green gate holds at end of slice: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, and `cd apps/web && pnpm test` all green (typecheck owned by its own slice).

## 7. Test plan (TDD) — unit + integration (gated on env) + how to run

Write tests first; mirror the existing split (pure helpers unit-tested hermetically; resource-heavy paths behind markers). New/extended test files:

**Unit (hermetic, no network/DB) — the coverage bulk:**
- `apps/worker/tests/test_reliability.py` — `ForgeTask` retry/backoff, `SoftTimeLimitExceeded`, `is_duplicate` dedup (fake Redis), `configure_reliability` applies the Celery knobs.
- `apps/worker/tests/test_agent_runner.py` (extend) — `run_agent_task` error/exception → result mapping; `idempotency_key` dedup.
- `apps/worker/tests/test_syncer.py` / `test_indexer.py` (extend) — the `ValueError` "requires either files or root" branch; index failure path.
- `packages/agent-runtime/tests/test_runtime_hardening.py` (extend) + `test_tools_and_policy.py`, `test_sandbox.py` — tool dispatch errors/unknown tool, all `policy_gate` effect branches, `context.build_system_prompt` with/without `agents_md`, **real-git** `WorktreeSandbox` create→cleanup and `_git` error branches, `graph.GraphError`/recursion-limit backstop.
- `apps/api/tests/test_ratelimit.py`, `test_idempotency.py`, `test_graceful_shutdown.py` — middleware behavior via FastAPI `TestClient` with the `memory`/fake backends (AC5–7).

**Integration (gated):**
- `packages/db/tests/test_migration.py` (extend, `@pytest.mark.postgres`) — `test_full_roundtrip_on_postgres`, `test_stepwise_revision_walk`, `test_rollback_is_data_preserving` (AC3, AC4) using `pg_engine`.
- `packages/evaluation/forge_eval/tests/test_retrieval_latency.py` (`@pytest.mark.perf`) — asserts the report has all stage percentiles and p95 ≤ budget at a small corpus (AC8).
- `packages/evaluation/forge_eval/tests/test_soak.py` (`@pytest.mark.soak`) — a short (seconds-scale) soak asserting `cross_tenant_leaks == 0` and `resource_stable` on a 2-tenant mini-run (AC10), so the soak logic itself is CI-verifiable even though the full duration is manual/nightly.

**How to run:**
```bash
# Hermetic default suite (sandbox-safe): coverage + reliability primitives
uv run pytest -q
uv run pytest apps/worker packages/agent-runtime \
  --cov=forge_worker --cov=forge_agent --cov-report=term-missing --cov-fail-under=90

# Live-Postgres lane (testcontainers or a service container)
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5432/forge_test
uv run pytest packages/db -m postgres

# Perf bench + soak (resourced/networked runner; not per-PR)
uv run pytest -m perf packages/evaluation
uv run pytest -m soak packages/evaluation
make load-smoke          # quick k6 smoke (CI, non-blocking)
k6 run deploy/load/k6/api_hotpaths.js          # full load (manual)
```

## 8. Security & policy considerations

- **Rate limiting is a security control, not just QoS** — it is the FORGE_SPEC Security-table "rate limiting" line and a HARD-09 punch-list item. It bounds brute-force on auth, abuse of expensive RAG/agent endpoints, and BYOK cost-exhaustion. It fails **open** only on backend outage by explicit config (`memory` fallback), never silently disabling auth; default behavior is to limit.
- **Idempotency keys are tenant-scoped** (`forge:idem:{workspace_id}:{key}`) so one tenant cannot read or collide with another's stored response — the same tenant-isolation invariant the soak verifies. Stored response bodies pass through the existing `forge_api.observability.redaction` filter before persistence so no secret is cached.
- **No new secret surface.** Reliability primitives read only `FORGE_`-prefixed config and the existing Redis URL; nothing new is logged. Rate-limit/idempotency keys derived from API-key ids use a hash, never the raw key.
- **Graceful shutdown preserves the immutable-audit and at-least-once guarantees.** Draining + `acks_late` ensures an interrupted run is re-queued (paired with task idempotency so re-run is exactly-once-effect), and the audit/OTel exporters are flushed on shutdown so no audit row is lost on restart.
- **Soak enforces the tenant-isolation security property** (FORGE_SPEC multi-tenant): zero cross-tenant leak under sustained mixed load is a hard pass/fail, complementing the per-request isolation tests with a sustained-load assertion.
- **DoS bounds carry through to the worker** — `prefetch_multiplier=1`, `max_tasks_per_child`, and soft/hard time limits bound a single tenant's ability to starve the worker pool.

## 9. Effort & risk (S/M/L + risks; what cannot be done in-sandbox)

**Effort: L.** Breakdown: coverage lift on worker+agent-runtime **M** (many small branch tests); migration round-trip/walk/data-preservation **S/M**; three reliability primitives (rate-limit + idempotency + graceful shutdown, API + worker) **M**; load/perf harness **M**; multi-tenant soak **M**. Largest by surface area is the reliability middleware + the harnesses; largest by tedium is the coverage branch-hunt.

Risks:
- **Coverage to 90% on side-effecty code is fiddly** (subprocess git, Celery, time limits). *Mitigation:* keep the pure/IO split that already exists (`run_objective` vs `run_agent_task`); use eager-mode Celery + fakes; cover real-git with `tmp_path` repos. (Medium)
- **`acks_late` + retries can double-run a non-idempotent task.** *Mitigation:* ship task-level idempotency (SETNX dedup) *with* `acks_late`, never one without the other; knowledge tasks are already content-hash idempotent. (Medium)
- **Rate-limit/idempotency middleware can become a single point of latency or failure.** *Mitigation:* Redis ops are O(1) with short timeouts; explicit `memory` fallback; both feature-flagged; both exempt health routes. (Low-Med)
- **Perf budgets are environment-sensitive** (CI runners vary). *Mitigation:* publish absolute numbers + the runner spec; the per-PR gate is the *smoke* only; the budgeted bench is nightly/manual with a documented machine class. (Medium)
- **Migration data-preservation is only meaningful for non-destructive revisions.** *Mitigation:* classify each revision in the runbook; destructive ones assert "backup required" rather than data-preserving. (Low-Med)

**Cannot be done in the no-network sandbox (named, not hidden):**
- `[live-PG]` migration round-trip/walk and `[runner]` perf/load/soak require a live Postgres and a resourced/networked runner (testcontainers/CI). They **skip** in-sandbox; the slice is "code-complete in-sandbox, gates verified on a runner."
- **A true multi-week, multi-tenant production *fleet* soak is out of scope** — HARD-11 delivers a **bounded, simulated** soak (minutes–hours, N tenants on one host). This limitation is stated verbatim in `docs/self-hosting/performance.md` and matches the spec's PRODUCTION "honest asterisk". A real fleet soak is a named external gap, not claimable from this slice.

## 10. Key files / paths (exact, in the real monorepo)

- `apps/api/forge_api/middleware/__init__.py` — `install_middleware`.
- `apps/api/forge_api/middleware/ratelimit.py` — `RateLimitMiddleware`, `RateLimiter`, redis/memory backends.
- `apps/api/forge_api/middleware/idempotency.py` — `IdempotencyMiddleware`, `IdempotencyStore`.
- `apps/api/forge_api/middleware/shutdown.py` — `ShutdownState`, `lifespan`.
- `apps/api/forge_api/main.py` — wire `lifespan=` + `install_middleware(app, cfg)`.
- `apps/api/forge_api/routers/health.py` — add `/health/ready` (readiness) alongside `/health`.
- `apps/api/forge_api/settings.py` — add the `FORGE_RATE_LIMIT_*` / `FORGE_IDEMPOTENCY_*` / `FORGE_SHUTDOWN_DRAIN_SECONDS` keys.
- `apps/api/tests/test_ratelimit.py`, `test_idempotency.py`, `test_graceful_shutdown.py`.
- `apps/worker/forge_worker/celery_app.py` — call `configure_reliability(celery_app, …)`.
- `apps/worker/forge_worker/reliability.py` — `ForgeTask`, `TransientError`, `configure_reliability`.
- `apps/worker/forge_worker/{agent_runner.py,indexer.py,syncer.py,tasks/incident.py}` — `base=ForgeTask`, optional `idempotency_key`.
- `apps/worker/tests/test_reliability.py` (+ extend `test_agent_runner.py`/`test_syncer.py`/`test_indexer.py`).
- `packages/agent-runtime/tests/test_runtime_hardening.py` (extend) + `test_tools_and_policy.py`, `test_sandbox.py`, `test_graph.py` (extend).
- `packages/db/forge_db/migration_utils.py` — `iter_revisions`, `revision_pairs`.
- `packages/db/tests/test_migration.py` — add `@pytest.mark.postgres` round-trip/walk/data-preservation tests.
- `packages/evaluation/forge_eval/perf/retrieval_latency.py` + `forge_eval/tests/test_retrieval_latency.py`.
- `packages/evaluation/forge_eval/soak/soak_runner.py` + `forge_eval/tests/test_soak.py`.
- `deploy/load/k6/api_hotpaths.js`, `deploy/load/locustfile.py`, `deploy/load/budgets.toml`, `deploy/load/reports/` (gitignored outputs).
- `deploy/docker-compose.yml` — `stop_grace_period`, `/health/ready` healthcheck, resource limits (extend existing).
- `apps/web/src/lib/api/*` — send `Idempotency-Key` on retry-prone mutations; handle `429`/`Retry-After`.
- `pyproject.toml` (root) — register `perf`/`soak` markers; `Makefile` — add `load-smoke` target.
- `.github/workflows/ci.yml` — un-skip `-m postgres` migration tests, per-package coverage floor, non-blocking `load-smoke` job.
- `docs/self-hosting/upgrade.md` (rollback runbook), `docs/self-hosting/performance.md` (perf + soak evidence), `docs/self-hosting/reliability.md` (shutdown/rate-limit/idempotency operator notes).

## 11. Research references

- FORGE_SPEC: "retrieval latency p50/p95/p99" observability metric; "Production Docker Compose Requirements" (stop_grace_period, healthchecks, resource limits, non-root); Security table (rate limiting, tenant isolation); Core Data Model (singular/enum tables).
- `docs/MORNING_REPORT.md` §3 (coverage table: worker 78.9%, agent-runtime 82.4%), §5(1) (live Postgres parked), §6 (no DB/load/soak; least-covered units), §7 (ranked next steps 1, 7).
- `SPEC-PRODUCTION-HARDENING.md` — gates G-COVERAGE, G-PERF, G-MIGRATE, G-SOAK; blocker-#6 mapping; "honest asterisk" on real fleet soak.
- Alembic downgrade / `ScriptDirectory.walk_revisions` — https://alembic.sqlalchemy.org/en/latest/api/script.html
- Celery reliability: `task_acks_late` / `task_reject_on_worker_lost` / `worker_max_tasks_per_child` / soft+hard time limits — https://docs.celeryq.dev/en/stable/userguide/configuration.html ; worker graceful (warm) shutdown — https://docs.celeryq.dev/en/stable/userguide/workers.html#stopping-the-worker
- HTTP `Idempotency-Key` header (IETF draft) — https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
- Token-bucket / fixed-window rate limiting; Starlette/FastAPI ASGI middleware — https://www.starlette.io/middleware/
- FastAPI `lifespan` (startup/shutdown) — https://fastapi.tiangolo.com/advanced/events/
- k6 thresholds/scenarios — https://grafana.com/docs/k6/latest/using-k6/thresholds/ ; Locust — https://docs.locust.io/
- testcontainers-python pgvector — https://testcontainers-python.readthedocs.io/ ; pgvector — https://github.com/pgvector/pgvector

## 12. Out of scope / future

- **Whole-workspace typecheck** (`make typecheck` green; the mypy "source file found twice" bug) — its own slice (spec's HARD-12 typecheck half); HARD-11 does not touch `mypy.ini`/`Makefile` typecheck wiring.
- **Durable idempotency table** (`idempotency_record` via `0003_idempotency`) — Redis is the BETA default; a DB-backed store for cross-flush durability is a documented optional follow-up (§3.1).
- **Distributed/global rate limiting with sliding-window-log precision** and quota tiers per plan — V1 uses fixed-window/token-bucket per scope; advanced quota tiers belong to the multi-team RBAC / marketplace slices.
- **Autoscaling / HPA-driven load shedding and circuit breakers** — beyond compose; belongs to the Kubernetes/Helm slice (F24) and a future resilience slice.
- **Real multi-week, multi-tenant production *fleet* soak and chaos/fault-injection (kill-broker, partition-DB)** — HARD-11 ships a bounded simulated soak; fleet soak + chaos engineering are named external/future gaps.
- **Per-tenant resource quotas and fair-share scheduling in the worker** (beyond prefetch/time-limits) — future capacity-management work.
- **Continuous perf-regression gating on absolute budgets in CI** — HARD-11 wires a non-blocking smoke + nightly bench; promoting the budgeted bench to a blocking gate (with a stable runner class) is a follow-up once the runner is standardized.
