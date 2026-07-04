# HARD-10 — Production Observability, Cost Accounting & Telemetry

> Phase: hardening · Blocker(s): #6 (maturity gaps: no real telemetry pipeline, no
> load/perf signal, no cost truth) — supports the verification of #1 (real model
> cost numbers) and #2 (retrieval-debug numbers on the real path) · Status target:
> **BETA-done** = the ALPHA "OTel hooks degrade to in-memory only" asterisk is
> retired *offline*: `forge_obs` exports real OTel traces/metrics/logs over OTLP
> when enabled (verified against an in-memory reader + a containerised
> `otel-collector` behind `@pytest.mark.integration`), a **durable cost ledger**
> (`cost_event` + `model_price`) runs on **real Postgres** (the HARD-01 substrate)
> with per-task/per-phase/per-provider rollups, the Prometheus rules + Grafana
> dashboards + alert rules all parse and validate as code, `docker compose
> --profile observability config` validates a hardened five-service stack, and the
> hermetic default suite stays green with a true **no-op** path. **PRODUCTION-done**
> = the live pipeline is exercised end-to-end on a networked runner (OTLP →
> collector → Prometheus/Tempo/Loki, Grafana loads the four dashboards with live
> data, ≥1 alert rule actually fires and resolves), and **token/cost accounting is
> proven on a real BYOK model run** (piggybacks HARD-02's model creds) so the cost
> numbers are real, not fixture. **Needs real creds?** No external creds for the
> ledger, cardinality, redaction, dashboard/rule, or no-op gates; the *real-cost*
> AC needs HARD-02's BYOK model key; the *live-pipeline* + *Grafana-load* +
> *alert-fires* ACs need a networked/CI runner (not the no-network sandbox).
>
> **Relationship to the product spec.** This workstream operationalizes the F38
> product slice (`docs/implementation-slices/cross-cutting/F38-observability-cost-metrics.md`):
> ALPHA built only the api-local `apps/api/forge_api/observability/` module (audit
> hash-chain, run-trace assembler, redaction, OTel hooks that *degrade to
> in-memory*). It did **not** build the F38 `forge_obs` shared package, the cost
> ledger, the Prometheus/Grafana/Loki/Tempo pipeline, or alert rules. HARD-10 makes
> those real and production-hardened. It EXTENDS the existing observability module
> and the F38-specified (not-yet-built) `forge_obs` shared seam; it does not
> duplicate an existing package.

---

## 1. Intent — what & why

Forge's ALPHA observability is honest-but-hollow: `apps/api/forge_api/observability/otel.py`
records spans into an in-memory `SpanRecorder` and *only* talks to a real
OpenTelemetry SDK "when the SDK is installed" — which it never is in the suite, so
no real export path has ever run. The audit log is an in-memory hash chain. There
is **no cost accounting at all** (`apps/worker/forge_worker/agent_runner.py` emits
zero token/cost telemetry today), **no Prometheus/Grafana/Loki/Tempo stack**, and
**no alert rules**. MORNING_REPORT §1.14 marks observability "DONE (DB-trigger
immutability PARKED)"; §6 lists "low coverage on the most side-effecty code" and
"no load/perf" — you cannot run a serious production deployment, debug a slow run,
or trust a spend number with what exists.

HARD-10 closes the maturity gap (blocker #6) by turning the observability seam into
something an operator can actually run and trust:

1. **One real telemetry init across `api` + `worker` + `mcp-gateway` + agent.** A
   `setup_telemetry()` that installs real OTel `TracerProvider`/`MeterProvider`/
   `LoggerProvider` with OTLP exporters and auto-instruments FastAPI / SQLAlchemy /
   httpx / Celery / Redis, with W3C trace-context propagation across the
   `api → Celery worker → mcp-gateway` boundary so a single workflow run is one
   end-to-end trace (the spec's "task lineage"). It keeps the in-memory recorder +
   a true **no-op** fallback so the hermetic suite stays network-free.
2. **Token/cost accounting per task / phase / provider, on the real path.** A
   durable, idempotent **cost ledger** on Postgres (`cost_event` priced from
   `model_price`), written through a single `UsageMeter` emission point wired into
   the **real** BYOK model/embedder/reranker clients from HARD-02/HARD-03, so the
   spec's "Cost: token cost per task, per workflow phase, per model provider" Key
   Metric is real money, not a fixture 0.0.
3. **Retrieval debug as live telemetry.** F05 already computes per-stage
   `RetrievalDebug` (semantic/keyword/fusion/rerank latency + reranker delta);
   HARD-10 emits it as `forge_retrieval_*` metrics + span attributes and a Grafana
   panel, giving the p50/p95/p99 retrieval-latency signal HARD-13 measures and the
   "retrieval debug" surface the spec mandates.
4. **The real ops stack + dashboards + alert rules.** The `observability` compose
   profile (`otel-collector` + `prometheus` + `grafana` + `loki` + `tempo` +
   `alertmanager`), hardened to the spec's production rules, with datasources, four
   dashboards, recording rules, and — new vs F38, which deferred alerting to V2 —
   **Prometheus alert rules** for the production-maturity signals (retrieval p99
   over budget, agent failure spike, cost-emit failures, unpriced models, error
   rate, queue backlog, exporter/collector down, audit-integrity break).
5. **Secret-safe by construction.** Structured JSON logs and span attributes pass
   through the existing redaction filter *last*, with metric-label cardinality
   bounded, so secrets never reach Loki/Tempo/Prometheus and dashboards can never
   become a DoS vector.

Why this is hardening and not product: F38 specified the *surface*; HARD-10 proves
the surface against a real collector, a real ledger on real Postgres, a real model
run, and a validated dashboard/alert pipeline — with a green whole-suite gate at
the end and an honest mark on what can only run on a networked runner.

## 2. User-facing / operator behavior

- **Operator A — lean default, zero overhead.** `docker compose up` (no profile)
  boots every service; `OBS_ENABLED=false` → `setup_telemetry` installs no-op
  providers, no OTLP connection is attempted, structured JSON logs still print to
  stdout, and the cost ledger still records (it is Postgres, not the collector).
  No crashes, near-zero overhead. (This is the hermetic-suite path too.)
- **Operator B — full stack.** `docker compose --profile observability up` brings
  up the collector + Prometheus + Grafana + Loki + Tempo + Alertmanager. Grafana
  (behind Caddy auth at `/grafana`) loads four pre-provisioned dashboards with live
  data: **Workflow Quality**, **Agent Quality**, **Retrieval Quality**, **Cost**.
- **Operator C — debug a slow run via trace↔log↔metric correlation.** A
  Retrieval-p99 alert fires; the operator pivots from the Grafana panel to a Tempo
  exemplar trace for a slow `knowledge.search`, sees `semantic 40ms / keyword 35ms
  / rerank 410ms`, clicks the `trace_id` into Loki and reads the structured logs
  for that exact request — every secret shown as `[REDACTED]`. The trace links back
  to the in-app run-trace view by `run_id`.
- **Engineer D — task spend (in-product).** Opens a task's **Cost** view: total
  spend, a by-phase breakdown (`spec_drafting`, `executing`, `verifying`), and a
  by-provider/model breakdown, all read from the workspace-scoped ledger.
- **Admin E — pricing + reprice.** Adds a `model_price` row for a newly-adopted
  BYOK model (priced immediately); runs reprice to recompute historical
  `cost_event.cost_usd` for rows whose `(provider, model, kind)` price changed
  (idempotent, audited).
- **On-call F — alerts.** An alert (`ForgeAgentFailureRateHigh`,
  `ForgeCostEmitFailures`, `ForgeOtlpExporterDown`, …) routes through Alertmanager;
  the runbook annotation links to the relevant dashboard + the
  `docs/self-hosting/observability.md` remediation section.

## 3. Vertical slice

> Layout note: EXTENDS the existing `apps/api/forge_api/observability/` module and
> realizes the F38-specified shared seam `packages/observability/forge_obs/` (net-
> new, not a duplicate — F38 owns the spec). New ORM lives in
> `packages/db/forge_db/models/` and the single Alembic history at
> `packages/db/migrations/versions/` (current head `0002_pm_adapters`). Deploy
> assets extend `deploy/` and add the `observability` compose profile. Table names
> are **singular** per the real `forge_db` convention (`workflow_run`, `agent_run`,
> `task`, `workspace`).

### 3.1 Data model

One additive migration `packages/db/migrations/versions/0003_cost_ledger.py`
(`down_revision = "0002_pm_adapters"`; `alembic downgrade` drops both tables
cleanly). Traces/metrics/logs are **not** stored in Postgres (they live in
Tempo/Prometheus/Loki); only the durable **cost ledger** and **price book** are
relational, because cost is queried, aggregated, re-priced transactionally, and
must survive a metrics-stack outage.

**New table `cost_event`** (durable system of record for token cost — the spec's
"token/cost logs"):

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `workspace_id` | `uuid` NOT NULL FK → `workspace.id` ON DELETE CASCADE | tenant key; indexed |
| `project_id` | `uuid` NULL FK → `project.id` | nullable for non-project calls |
| `task_id` | `uuid` NULL FK → `task.id` ON DELETE SET NULL | per-task cost (Key Metric) |
| `workflow_run_id` | `uuid` NULL FK → `workflow_run.id` | per-run rollup |
| `agent_run_id` | `uuid` NULL FK → `agent_run.id` | F06/agent runtime |
| `step_index` | `integer` NULL | ordinal into `agent_run.steps` (JSON) — links cost to the trace step (the repo stores steps as a JSON list, not a child table; see §4 frozen-contract note) |
| `phase` | `text` NULL | FSM state at call time (`spec_drafting`,`executing`,…) — per-phase cost |
| `kind` | `text` NOT NULL | `completion \| embedding \| rerank` |
| `provider` | `text` NOT NULL | `anthropic \| openai \| jina \| cohere \| …` (BYOK provider id) |
| `model` | `text` NOT NULL | model id |
| `prompt_tokens` | `integer` NOT NULL DEFAULT 0 | (== `TokenUsage.input_tokens`) |
| `completion_tokens` | `integer` NOT NULL DEFAULT 0 | (== `TokenUsage.output_tokens`) |
| `total_tokens` | `integer` GENERATED ALWAYS AS (`prompt_tokens + completion_tokens`) STORED | |
| `cost_usd` | `numeric(14,8)` NOT NULL | computed at emission from `model_price` |
| `price_id` | `uuid` NULL FK → `model_price.id` | price row used (reprice provenance) |
| `request_id` | `text` NOT NULL | idempotency key (provider response id / generated); dedups retries |
| `occurred_at` | `timestamptz` NOT NULL | when the model call completed |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | `TimestampMixin` |

Constraints / indexes: `UNIQUE (workspace_id, request_id)` (idempotent emission —
prevents double-billing on retries); `INDEX (workspace_id, occurred_at)`;
`INDEX (task_id)`; `INDEX (workflow_run_id)`;
`INDEX (workspace_id, provider, model, occurred_at)`;
`INDEX (workspace_id, phase, occurred_at)`. **Append-only**: rows INSERT once; the
only permitted UPDATE is `cost_usd`/`price_id` via the audited reprice path
(enforced in the repository, mirroring the audit-log immutability discipline; the
hard DB-trigger immutability for the *audit* table itself is owned by HARD-01).

**New table `model_price`** (BYOK price book; per-workspace override + global
default):

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NULL FK → `workspace.id` ON DELETE CASCADE | NULL = global default; non-null = override |
| `provider` / `model` / `kind` | `text` NOT NULL | `kind` ∈ `completion\|embedding\|rerank` |
| `prompt_usd_per_1k` | `numeric(14,8)` NOT NULL DEFAULT 0 | |
| `completion_usd_per_1k` | `numeric(14,8)` NOT NULL DEFAULT 0 | |
| `currency` | `text` NOT NULL DEFAULT `'USD'` | V1 USD only |
| `effective_from` | `timestamptz` NOT NULL DEFAULT now() | historical pricing |
| `created_by` | `uuid` NULL FK → `user.id` | |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

Indexes: `INDEX (provider, model, kind, effective_from DESC)` and
`INDEX (workspace_id, provider, model, kind, effective_from DESC)`. Resolution =
newest `effective_from <= occurred_at`, preferring a `workspace_id` match over the
global (NULL) row. A seed inserts sane global defaults for the providers Forge
ships (Anthropic/OpenAI/Jina/Cohere); an unknown `(provider, model)` prices at `0`,
sets `priced=False`, and increments `forge_unpriced_model_total` so gaps are
**visible, never silently dropped**.

**Read-only dependencies (not created here):** `workspace`/`user`/`project`/`task`
(foundation + F01), `workflow_run`/`agent_run` (`packages/db/forge_db/models/runs.py`).

### 3.2 Backend

**Extend `apps/api/forge_api/observability/`** (already exists — keep its public
surface, add real export + cost):

- `otel.py` (extend) — `setup_telemetry(service_name, settings) -> Telemetry`:
  when `settings.enabled` and an OTLP endpoint resolves, install real
  `TracerProvider`/`MeterProvider`/`LoggerProvider` + `OTLPSpanExporter`/
  `OTLPMetricExporter`/`OTLPLogExporter`, register OTel auto-instrumentors
  (FastAPI, SQLAlchemy, httpx, Celery, Redis), and set resource attributes
  (`service.name`, `forge.version`, `deployment.environment`). When disabled / no
  endpoint → install **no-op** providers; keep the existing in-memory
  `SpanRecorder` always (so the trace viewer + tests work offline). `setup_telemetry`
  is **idempotent** (safe double-call). Existing `span()`/`traced()`/`SpanRecorder`
  keep working unchanged; they now also feed the real tracer when present.
- `metrics.py` (new in the module) — `ForgeMetrics` facade (one typed method per
  Key Metric, §4) over pre-created OTel instruments exported as Prometheus series
  with **bounded label cardinality**; `NoopMetrics`; `get_metrics()` process
  singleton; a cardinality guard that buckets unknown allow-listed values to
  `"other"` and forbids id-shaped labels.
- `logging.py` (new) — `configure_logging(service_name, settings)`: JSON logs to
  stdout (+OTLP when enabled) carrying `trace_id`/`span_id`/`workspace_id`; the
  existing `redact_*` (from `redaction.py`) runs as the **last** processor.
  `bind_context(**kv)` (contextvar-backed) and `get_logger(name)`.
- `cost/` (new sub-package) — `models.py` (Pydantic DTOs, §4), `pricing.py`
  (`PriceBook` Protocol + `DbPriceBook` + `compute_cost`), `meter.py` (`UsageMeter`
  + `NoopUsageMeter`), `repository.py` (`CostLedgerRepository` upsert/reprice +
  `CostReadRepository` rollups/timeseries).
- `service.py` (extend `ObservabilityService`) — hold a `ForgeMetrics` + a
  `CostReadRepository`; add `cost_summary(...)` / `cost_timeseries(...)` /
  `record_run` is unchanged.
- `routers/observability.py` (extend) — add cost endpoints under the existing
  `/observability` family (the repo mounts routers without an `/api/v1` prefix):
  `GET /observability/cost/summary`, `GET /observability/cost/timeseries`,
  `GET /observability/cost/tasks/{task_id}`, `GET /observability/cost/prices`,
  `POST /observability/cost/prices` (admin), `POST /observability/cost/reprice`
  (admin, enqueues the worker task), and a hardened internal-only `/metrics`
  Prometheus exposition endpoint (bound to the internal network, never routed
  through Caddy). All authenticated; `viewer`+ read; `admin` for price/reprice;
  every read filters by the caller's `workspace_id` (cross-workspace `scope_id` →
  404, no existence leak).

**Shared seam `packages/observability/forge_obs/`** (realizes F38; importable by
`worker` + `mcp-gateway` + agent which cannot import `forge_api`): the framework-
agnostic core — `settings.py` (`ObsSettings`), `telemetry.py`, `metrics.py`,
`logging.py`, `tracing.py`, `cost/` — with `apps/api/forge_api/observability/`
re-exporting from it for back-compat so existing imports keep working. (If the
program prefers strictly "extend, no new package", the alternative is to lift the
shared primitives into `forge_contracts`/`forge_db`; the F38 product slice already
chose `forge_obs`, so HARD-10 follows it and notes the decision.)

### 3.3 Worker / agent

- `apps/worker/forge_worker/observability/init.py` (new) — `setup_telemetry("forge-worker", …)`
  + `configure_logging` on Celery `worker_process_init`; `task_prerun`/`task_postrun`
  carry the propagated W3C trace context so an `api`-started span continues into the
  worker task. Constructs **one** `UsageMeter` per process (DB session +
  `DbPriceBook` + `ForgeMetrics`).
- `apps/worker/forge_worker/agent_runner.py` (extend — currently emits **no** cost
  telemetry): wrap every model / embedding / rerank call so
  `client.call(...) → UsageMeter.record(ModelUsage(...)) → CostRecord`. The
  `CostRecord.cost_usd` is recorded both in the `cost_event` row (authoritative) and
  stamped into the corresponding `agent_run.steps[i].metadata["cost_usd"]` JSON
  field (the repo stores steps as a JSON list on `agent_run`, not a child table, and
  the frozen `TokenUsage` DTO has no `cost_usd` — see §4), so the trace view and the
  ledger share one source. `phase` is read from the current FSM state; `provider`/
  `model` from the resolved BYOK identity (HARD-02). Emission is **guarded**: a
  metric/ledger failure logs + increments `forge_cost_emit_failures_total` and never
  aborts the run.
- New Celery tasks (queue `observability`): `cost.reprice(workspace_id, since_iso,
  provider?, model?) -> dict` (idempotent historical re-price, emits an audit
  event); `obs.refresh_freshness_gauges() -> dict` (beat, 60s) sets
  `forge_mcp_freshness_lag_seconds` per MCP connection.
- `apps/mcp-gateway/forge_mcp_gateway/observability/init.py` (new) —
  `setup_telemetry("forge-mcp-gateway", …)`; accepts inbound trace context; emits
  `forge_mcp_calls_total` + `forge_mcp_call_latency_seconds` per tool call so MCP
  calls appear in the same end-to-end trace and complement the MCP audit row.
- **Retrieval debug emission** (extend `forge_knowledge`'s search service): after
  the pipeline builds `RetrievalDebug`, call `ForgeMetrics.record_retrieval(...)`
  with per-stage latencies + reranker delta and attach the same as span attributes.

### 3.4 Frontend

In-product cost surface (ops dashboards live in Grafana, §3.5), extending
`apps/web`:

- A **Cost** view on a task (total, by-phase, by-provider/model, token totals) via
  TanStack Query against `GET /observability/cost/tasks/{taskId}`.
- A project-level **Insights → Cost** view: daily spend time-series stacked by
  provider, top-N expensive tasks, cost-per-merged-PR; date-range + provider
  filters.
- `components/cost/{CostBreakdown,CostTimeseriesChart,CostStat}.tsx` + a typed
  `lib/api/cost.ts` client; an admin-only price-book table (the only mutating UI).
- A settings link that deep-links to Grafana `/grafana` for operators.

If the in-product UI is descoped for BETA, the API + Grafana dashboards alone
satisfy the operator journeys; the web pieces are a thin, test-covered add.

### 3.5 Infra / deploy / CI

Fill in the `observability` compose **profile** in `deploy/docker-compose.yml`
(today there is no such profile; networks are `edge`/`backend`/`data`/`mcp`). All
services obey the spec's Production Docker Compose Requirements — pinned
`@sha256`, non-root, healthcheck, CPU/mem limits, capped logs, named volumes, the
`autoheal=true` label, on a new `internal: true` `observability` network:

- `otel-collector` — OTLP gRPC/HTTP receiver → exports metrics to Prometheus
  (scrape/remote-write), traces to Tempo, logs to Loki. Config
  `deploy/observability/otel-collector/config.yaml`.
- `prometheus` — scrapes the collector + each app `/metrics`; config
  `deploy/observability/prometheus/prometheus.yml`; **recording rules**
  `rules/forge.rules.yml` (rate/percentile rollups); **alert rules**
  `rules/forge.alerts.yml` (new); named volume.
- `grafana` — provisioned datasources (Prometheus/Loki/Tempo) + four dashboards
  (`dashboards/{workflow-quality,agent-quality,retrieval-quality,cost}.json`);
  admin password from `GRAFANA_ADMIN_PASSWORD`; reached only via Caddy `/grafana`.
- `loki` + `tempo` — log/trace stores (Grafana-native; Tempo is the self-hosted
  trace sink; LangSmith stays an optional extra via `LANGSMITH_API_KEY`).
- `alertmanager` — routes Prometheus alerts (config
  `deploy/observability/alertmanager/alertmanager.yml`); a webhook receiver can
  reuse the existing `FORGE_GRAFANA_WEBHOOK_SECRET` seam.
- `deploy/caddy/Caddyfile` — add `handle_path /grafana/*` reverse-proxy behind
  Caddy auth; Grafana is never published directly.

CI (`.github/workflows/ci.yml`): the `compose` job adds
`docker compose --profile observability config`; a new `observability` job runs
`promtool check rules` + `promtool check config` and the dashboard/rule JSON/YAML
contract tests; the live OTLP-roundtrip + Grafana-load + alert-fires checks run on
a networked runner job (gated, not in the no-network sandbox).

## 4. Public interfaces / contracts

**Settings + telemetry (`forge_obs/settings.py`, `telemetry.py`):**

```python
@dataclass(frozen=True)
class ObsSettings:
    enabled: bool = False
    service_name: str = "forge"
    version: str = "0.1.0"
    environment: str = "dev"
    otlp_endpoint: str | None = None          # None -> no-op exporters
    traces_sampler_ratio: float = 0.1
    metric_workspace_label: bool = False       # cardinality guard
    prometheus_scrape_enabled: bool = True

class Telemetry:
    metrics: "ForgeMetrics"
    def shutdown(self) -> None: ...            # flush + close exporters

def setup_telemetry(service_name: str, settings: ObsSettings) -> Telemetry:
    """Idempotent. Real OTel providers + OTLP exporters + auto-instrumentation when
    settings.enabled and otlp_endpoint resolves; else no-op providers + NoopMetrics."""
```

**Key-Metric facade (`forge_obs/metrics.py`) — implemented here, called by
F02/F05/F06/F07/F08/F09/F12 seams:**

```python
class ForgeMetrics(Protocol):
    # Cost (called only via UsageMeter)
    def record_model_cost(self, *, provider: str, model: str, kind: str,
                          phase: str | None, prompt_tokens: int,
                          completion_tokens: int, cost_usd: float) -> None: ...
    # Workflow quality
    def record_workflow_terminal(self, *, workflow: str, terminal_state: str,
                                 duration_seconds: float) -> None: ...
    def record_task_completion(self, *, status: str, duration_seconds: float | None) -> None: ...
    def record_approval_decision(self, *, gate: str, decision: str) -> None: ...
    def record_pr_outcome(self, *, outcome: str, time_to_merge_seconds: float | None) -> None: ...
    def observe_spec_completeness(self, *, score: float) -> None: ...
    # Agent quality
    def record_agent_run(self, *, status: str, skill_profile: str,
                         retries: int, confidence: float | None) -> None: ...
    def observe_requirement_satisfaction(self, *, ratio: float) -> None: ...
    # Retrieval quality (HARD-04/HARD-13 signal)
    def record_retrieval(self, *, hit: bool, total_latency_seconds: float,
                         stage_latencies: Mapping[str, float],
                         reranker_delta: float | None) -> None: ...
    def record_mcp_call(self, *, connection: str, status: str, latency_seconds: float) -> None: ...
    def set_mcp_freshness_lag(self, *, connection: str, lag_seconds: float) -> None: ...

def get_metrics() -> ForgeMetrics: ...        # process singleton (real or NoopMetrics)
```

**Prometheus instrument catalog (frozen cardinality contract)** — names/types/labels
mirror F38 §4 (the load-bearing subset):

| Prometheus name | Type | Labels (bounded) | Spec Key Metric |
|---|---|---|---|
| `forge_model_tokens_total` | Counter | `service,provider,model,phase,token_kind{prompt,completion}` | Cost — tokens |
| `forge_model_cost_usd_total` | Counter | `service,provider,model,phase` | Cost per phase/provider |
| `forge_cost_emit_failures_total` | Counter | `service,reason` | ledger health |
| `forge_unpriced_model_total` | Counter | `provider,model` | price-gap visibility |
| `forge_workflow_runs_total` | Counter | `workflow,terminal_state` | task completion rate |
| `forge_task_completions_total` | Counter | `status` | task completion rate |
| `forge_approval_decisions_total` | Counter | `gate,decision` | approval accept/reject |
| `forge_pr_outcomes_total` | Counter | `outcome` | PR acceptance |
| `forge_time_to_merge_seconds` | Histogram | `workflow` | mean time to merge |
| `forge_task_duration_seconds` | Histogram | `workflow` | time to task completion |
| `forge_spec_completeness` | Histogram (0..1) | — | spec completeness |
| `forge_agent_runs_total` | Counter | `status,skill_profile` | failure rate |
| `forge_agent_retries_total` | Counter | `skill_profile` | retry rate |
| `forge_agent_confidence` | Histogram (0..1) | `skill_profile` | confidence dist. |
| `forge_requirement_satisfaction_ratio` | Histogram (0..1) | — | requirement satisfaction |
| `forge_retrieval_requests_total` | Counter | `hit` | hybrid hit rate |
| `forge_retrieval_latency_seconds` | Histogram | `stage{semantic,keyword,fusion,rerank,total}` | retrieval p50/p95/p99 |
| `forge_reranker_delta` | Histogram | — | reranker delta |
| `forge_mcp_calls_total` | Counter | `connection,status` | MCP audit/health |
| `forge_mcp_call_latency_seconds` | Histogram | `connection` | MCP call latency |
| `forge_mcp_freshness_lag_seconds` | Gauge | `connection` | MCP freshness lag |

Cardinality rules (enforced by guard + a contract test): **no `task_id`,
`workflow_run_id`, `user_id`, or raw `workspace_id`** as a metric label;
`workspace_id` is added only if `OBS_METRIC_WORKSPACE_LABEL=true`. High-cardinality
dimensions live on **span attributes** and the **cost ledger** instead.

**Cost models (`forge_obs/cost/models.py`):**

```python
class ModelUsage(BaseModel):
    workspace_id: UUID
    request_id: str                  # idempotency key
    provider: str; model: str; kind: str = "completion"
    prompt_tokens: int = 0; completion_tokens: int = 0
    occurred_at: datetime
    project_id: UUID | None = None; task_id: UUID | None = None
    workflow_run_id: UUID | None = None; agent_run_id: UUID | None = None
    step_index: int | None = None; phase: str | None = None

class ModelPrice(BaseModel):
    id: UUID | None = None
    provider: str; model: str; kind: str
    prompt_usd_per_1k: Decimal = Decimal(0); completion_usd_per_1k: Decimal = Decimal(0)
    currency: str = "USD"; effective_from: datetime

class CostRecord(BaseModel):          # returned by UsageMeter.record
    cost_event_id: UUID; cost_usd: Decimal; priced: bool; price_id: UUID | None = None

class CostBucket(BaseModel):
    key: str; cost_usd: Decimal; prompt_tokens: int; completion_tokens: int

class CostSummary(BaseModel):
    scope: str; scope_id: UUID; total_cost_usd: Decimal
    total_prompt_tokens: int; total_completion_tokens: int
    group_by: str; buckets: list[CostBucket]
    from_: datetime | None = Field(default=None, alias="from"); to: datetime | None = None

class CostTimeseries(BaseModel):
    scope: str; scope_id: UUID; bucket: str; group_by: str
    series: dict[str, list[tuple[datetime, Decimal]]]
```

**Cost emission + pricing (`meter.py`, `pricing.py`, `repository.py`):**

```python
class PriceBook(Protocol):
    async def resolve(self, *, workspace_id, provider, model, kind, at: datetime) -> ModelPrice | None: ...

def compute_cost(usage: ModelUsage, price: ModelPrice | None) -> Decimal:
    """(prompt/1000)*prompt_usd_per_1k + (completion/1000)*completion_usd_per_1k.
    price=None -> Decimal(0) (priced=False; increments forge_unpriced_model_total)."""

class UsageMeter(Protocol):
    async def record(self, usage: ModelUsage) -> CostRecord:
        """Resolve price, compute cost, idempotently upsert cost_event on
        (workspace_id, request_id), increment cost counters, return CostRecord.
        Guarded: metric failure never raises; ledger failure increments
        forge_cost_emit_failures_total (re-raises only in strict mode)."""

class CostReadRepository(Protocol):
    async def summary(self, *, workspace_id, scope, scope_id, group_by, frm, to) -> CostSummary: ...
    async def timeseries(self, *, workspace_id, scope, scope_id, bucket, group_by, frm, to) -> CostTimeseries: ...
```

**Frozen-contract reconciliation (load-bearing — differs from F38's assumption).**
`forge_contracts.TokenUsage` is **frozen** and has only `input_tokens` /
`output_tokens` — **no `cost_usd`** — and the repo stores agent steps as a **JSON
list** on `agent_run.steps`, not a child `agent_steps` table. So HARD-10 does
**not** mutate the contract: `prompt_tokens`/`completion_tokens` map to
`input_tokens`/`output_tokens`; the authoritative cost lives in `cost_event`; the
per-step cost is carried in `agent_run.steps[i].metadata["cost_usd"]` (free JSON)
and joined back to the ledger by `(agent_run_id, step_index)`. This keeps
`forge_contracts` frozen while still letting the trace view and the ledger agree.

**REST API (under `/observability`, authenticated; `viewer`+ read; `admin`
price/reprice):**

```
GET  /observability/cost/tasks/{task_id}                         -> CostSummary (group_by=phase)
GET  /observability/cost/summary?scope=&scope_id=&group_by=&from=&to=   -> CostSummary
GET  /observability/cost/timeseries?scope=&scope_id=&bucket=&group_by=&from=&to= -> CostTimeseries
GET  /observability/cost/prices?provider=&model=                 -> {items: ModelPrice[]}
POST /observability/cost/prices            (admin)  body: ModelPrice -> ModelPrice
POST /observability/cost/reprice           (admin)  body: {scope_id, since, provider?, model?} -> {updated:int}
GET  /metrics                              (internal network only; Prometheus exposition)
```

**Env / config keys** (extend `.env.example` / `.env.production.example`; existing
keys reused: `OTEL_EXPORTER_OTLP_ENDPOINT`, `LANGSMITH_API_KEY`, `MODEL_PROVIDER`,
`MODEL_PROVIDER_KEY`, `FORGE_GRAFANA_WEBHOOK_SECRET`): `OBS_ENABLED` (default
`false` base / `true` under the profile), `OTEL_TRACES_SAMPLER` +
`OTEL_TRACES_SAMPLER_ARG` (default parent-ratio `0.1`), `OBS_METRIC_WORKSPACE_LABEL`
(default `false`), `PROMETHEUS_SCRAPE_ENABLED`, `LOKI_ENDPOINT`, `TEMPO_ENDPOINT`,
`GRAFANA_ADMIN_PASSWORD`, `COST_DEFAULT_CURRENCY=USD`,
`COST_UNPRICED_MODEL_BEHAVIOR=warn` (`warn`|`error`).

**Grafana dashboard JSON + Prometheus rule YAML** are validated by a contract test:
each dashboard parses, declares a unique `uid`+`title`, and references only
provisioned datasources; each rule file parses and references only metrics in the
catalog above.

## 5. Dependencies (must exist first)

- **HARD-01 — real Postgres + pgvector substrate** (hard). The cost ledger rollups
  (`generated` column, indexes, time-bucketing, idempotent upsert, reprice) are
  proven on **real Postgres** via the existing `pg_engine`/`postgres_url`
  conftest fixtures and the `postgres` marker; HARD-01 also owns the audit-table
  immutability trigger this slice's ledger discipline mirrors.
- **HARD-02 — real model provider (BYOK)** (hard for the *real-cost* gate). Supplies
  the live `provider`/`model` identity + real `TokenUsage` from a real run that the
  `UsageMeter` prices, so the cost numbers are real, not fixture. Absent → cost
  paths are exercised with a fake model client (offline) and the real-cost AC skips.
- **HARD-03 — real embedder + reranker** (soft). Routes embedding/rerank cost
  through `UsageMeter` and supplies the real retrieval-debug latencies/reranker
  delta the Retrieval-Quality dashboard + `forge_retrieval_*` metrics display.
- **HARD-04 / HARD-13 — real eval + perf** (soft, downstream consumers). HARD-13
  reads `forge_retrieval_latency_seconds` p50/p95/p99 and the cost counters from
  this pipeline; HARD-04's honest numbers populate the eval-quality panels.
- **Production crypto / secret-key / BYOK vault seam (gate `G-CRYPTO`)** (soft). The
  provider/model identity + BYOK key the meter sees come from the encrypted vault;
  the redaction filter ensures keys never reach logs/traces/labels/ledger. (The
  spec tracks the crypto/secret-key hardening as a separate workstream; HARD-10
  consumes its vault + redaction outputs, it does not own them.)
- **HARD-08 — container/web build + digest pin** (hard for the live-stack gate). The
  `observability` profile images must be pinned `@sha256`; the live pipeline + alert
  smoke needs a networked runner from the same build lane.
- **Existing foundation** (hard): `forge_contracts` (frozen `TokenUsage`,
  `AgentRunResult`, `Step`), `forge_db` (`workspace`/`project`/`task`/`workflow_run`/
  `agent_run`, `TimestampMixin`, naming convention, the linear Alembic history),
  the `apps/api/forge_api/observability/` module (audit/trace/redaction/otel), RBAC
  (`viewer`/`member`/`admin`/`agent-runner`) and the audit log
  (`AuditLog`/`AuditEntry`) this slice writes price/reprice events to.
- **Product spec owner**: `cross-cutting/F38-observability-cost-metrics.md` (this
  workstream realizes + hardens it); `v1/F10-run-trace-viewer.md` (per-run cost
  reads the same per-step value); `v1/F14-docker-compose-selfhost.md` (base compose
  + hardening invariants the profile conforms to).

This slice's core (ledger + cost API + facade + no-op telemetry + dashboard/rule
validation) is **buildable and fully testable offline** against fakes + the
testcontainer Postgres; only the live OTLP roundtrip, Grafana load, alert firing,
and real-cost numbers need a networked runner / HARD-02 creds.

## 6. Acceptance criteria (numbered, testable)

**Offline (no external creds, hermetic or `postgres`-marker):**

1. **No-op default.** With `OBS_ENABLED=false` (or no endpoint), `setup_telemetry`
   installs no-op providers, `get_metrics()` returns `NoopMetrics`, **no** OTLP
   connection is attempted, and structured JSON logs still print to stdout; the
   whole-suite gate (`uv run pytest -q` + `ruff check .` + `ruff format --check .` +
   `make typecheck` + `cd apps/web && pnpm test`) stays green and network-free.
2. **Idempotent init.** A second `setup_telemetry` call does not duplicate
   instruments/exporters (asserted via the instrument registry). *(offline)*
3. **Migration.** `0003_cost_ledger` creates `cost_event` + `model_price` with the
   documented columns, the generated `total_tokens`, the `UNIQUE (workspace_id,
   request_id)` constraint, and all indexes; `alembic downgrade` drops both cleanly
   — asserted on **real Postgres** via `pg_indexes`/table existence (`postgres`
   marker; runs on the HARD-01 substrate).
4. **Cost math is exact.** `prompt_tokens=2000, completion_tokens=500`, price
   `prompt_usd_per_1k=0.003, completion_usd_per_1k=0.015` → `Decimal("0.0135")`;
   `price=None` → `Decimal(0)` with `priced=False`. *(offline)*
5. **Price resolution.** `PriceBook.resolve` returns the newest `effective_from <=
   occurred_at`, preferring a workspace override over the global row. *(offline)*
6. **Meter idempotency + counters.** `UsageMeter.record` upserts exactly one
   `cost_event`, increments `forge_model_tokens_total` and
   `forge_model_cost_usd_total` by the computed amounts, and returns a `CostRecord`;
   a second call with the same `(workspace_id, request_id)` yields **one** row and
   does **not** double-increment. *(offline w/ in-memory metric reader; ledger on
   `postgres` marker)*
7. **Unpriced visibility.** An unpriced `(provider, model)` records `cost_usd=0`,
   `priced=False`, and increments `forge_unpriced_model_total{provider,model}` (the
   call is not dropped). *(offline)*
8. **Guarded emission.** When the metric exporter raises, `record` still persists
   the ledger row and does not propagate; when the ledger write raises (non-strict),
   it increments `forge_cost_emit_failures_total` and does not crash the run.
   *(offline)*
9. **Ledger ↔ trace agreement.** A recorded model call's `cost_event.cost_usd`
   equals the `CostRecord.cost_usd` stamped into
   `agent_run.steps[i].metadata["cost_usd"]` (single source) — verified with a
   seeded `agent_run`. *(offline / `postgres`)*
10. **Rollups.** `GET /observability/cost/tasks/{id}` totals equal the sum of the
    task's `cost_event.cost_usd` with a correct by-phase breakdown;
    `summary?group_by=provider|model` and `timeseries?bucket=day` bucket-sums equal
    the scoped total. *(offline / `postgres`)*
11. **Cardinality contract.** The instrument catalog has **no** `task_id`/
    `workflow_run_id`/`user_id` label and no raw `workspace_id` unless
    `OBS_METRIC_WORKSPACE_LABEL=true`; an out-of-allow-list value is bucketed to
    `"other"`. Asserted by a registry contract test. *(offline)*
12. **Redaction defense-in-depth.** A log record and a span attribute containing a
    secret-shaped value are emitted with the value replaced by `[REDACTED]`
    (nested structures + a BYOK-key passed through a model client appear nowhere in
    logs, span attributes, metric labels, or the ledger). *(offline)*
13. **RBAC + isolation.** `viewer` reads all cost GETs; `POST /cost/prices` +
    `/cost/reprice` require `admin`; a cross-workspace `scope_id` → **404**;
    `/metrics` is not reachable through the public proxy. *(offline)*
14. **Reprice audited + idempotent.** `cost.reprice` updates only rows on/after
    `since` whose `(provider, model, kind)` price changed, recomputes `cost_usd`
    idempotently (re-run is a no-op), and writes a `cost.repriced` audit entry.
    *(offline / `postgres`)*
15. **Dashboard/rule validity.** Every `deploy/observability/grafana/dashboards/*.json`
    parses with a unique `uid`+`title` and references only provisioned datasources;
    every Prometheus `rules/*.yml` parses (`promtool check rules`) and references
    only metrics in the §4 catalog; `alertmanager.yml` is valid
    (`amtool check-config`). *(offline)*
16. **Compose profile hardened.** `docker compose --profile observability config`
    validates and includes `otel-collector`, `prometheus`, `grafana`, `loki`,
    `tempo`, `alertmanager`, each with a pinned `@sha256` image, healthcheck,
    CPU/mem limits, non-root user, `autoheal=true`, on the `internal: true`
    `observability` network, no host-published port except Grafana via Caddy.
    *(offline — `config` only; `build`/`up` is the networked gate)*

**Needs real creds / networked runner (PRODUCTION gate, cannot run in the
no-network sandbox):**

17. **Live OTLP roundtrip.** With `OBS_ENABLED=true` against a containerised
    `otel-collector`, a request that starts in `api`, enqueues a Celery task, and
    triggers an `mcp-gateway` call produces spans sharing one `trace_id` across the
    three services, visible in Tempo; metrics land in Prometheus; logs land in Loki.
    *(`@pytest.mark.integration`; networked)*
18. **Real cost numbers.** With HARD-02's BYOK model key, a real agent run produces
    `cost_event` rows with non-zero `cost_usd`, correct `provider`/`model`/`phase`,
    and a per-task summary that matches the provider's reported usage within
    rounding — replacing any fixture/zero cost. *(needs HARD-02 creds; `integration`)*
19. **Grafana loads live.** On the networked runner, Grafana provisions the four
    dashboards and they render live panels (Workflow/Agent/Retrieval/Cost) from
    Prometheus/Loki/Tempo. *(networked)*
20. **An alert fires + resolves.** At least one alert rule (e.g.
    `ForgeOtlpExporterDown` or `ForgeCostEmitFailures`) transitions
    pending→firing→resolved through Alertmanager under an induced fault. *(networked)*

## 7. Test plan (TDD) — unit + integration + how to run

Write tests first. Layout: `apps/api/forge_api/observability/` tests live in
`apps/api/tests/` (extend the existing `test_obs_*.py`); shared core in
`packages/observability/tests/`; worker propagation/cost in `apps/worker/tests/`;
deploy contract tests in `deploy/tests/` (or `tests/`); web in `apps/web`.

**Key fixtures:** `pg_engine`/`postgres_url` (existing conftest, `postgres` marker)
+ `alembic upgrade head` including `0003`; `metric_reader` (OTel
`InMemoryMetricReader`); `span_exporter` (OTel `InMemorySpanExporter`);
`fake_price_book` (global + workspace override at two `effective_from` dates);
`seed_run` (workspace/project/task/workflow_run/agent_run so cost links to a real
step index); `secretful_payload` (API-key-shaped strings); `meter` (real
`UsageMeter` over `pg` + `fake_price_book` + in-memory metrics);
`fake_model_client` (offline) and a real-client variant gated on HARD-02 creds.

**Unit (offline, hermetic):**
- `test_compute_cost_exact` / `test_compute_cost_none_is_zero_unpriced` (AC4/AC7).
- `test_price_resolution_prefers_override_and_effective_date` (AC5).
- `test_record_idempotent_on_request_id` / `test_record_increments_counters` (AC6).
- `test_unpriced_model_records_zero_and_warns` (AC7).
- `test_metric_export_failure_swallowed` / `test_ledger_failure_increments_counter` (AC8).
- `test_facade_methods_emit_expected_instruments` + `test_no_high_cardinality_labels`
  + `test_unknown_label_bucketed_to_other` (AC11).
- `test_setup_telemetry_idempotent` (AC2) + `test_noop_when_disabled_no_export` (AC1).
- `test_logs_carry_trace_context` + `test_log_and_span_secret_redaction` (AC12).

**Integration — `postgres` marker (real PG, no external creds):**
- `test_migration_0003_up_down` (AC3, via `pg_indexes`).
- `test_summary_group_by_phase_provider_model_sums_match_total` +
  `test_timeseries_buckets_sum_to_total` (AC10).
- `test_ledger_and_step_cost_agree` (AC9) and `test_reprice_only_affected_idempotent_audited` (AC14).

**API tests (httpx AsyncClient, offline):**
- `test_task_cost_summary` (AC10); `test_viewer_reads_admin_mutates` +
  `test_cross_workspace_is_404` + `test_metrics_endpoint_internal_only` (AC13).

**Contract tests (offline):**
- `test_grafana_dashboards_valid` + `test_prometheus_rules_reference_known_metrics`
  + `test_alertmanager_config_valid` (AC15).
- `test_observability_profile_hardened` — `docker compose --profile observability
  config` includes the six services with pinned digest/healthcheck/limits/non-root/
  internal network/no rogue host ports (AC16).

**Integration — `integration` marker (networked / HARD-02 creds; skips clean
without):**
- `test_trace_context_propagates_api_worker_mcp` against a real collector (AC17).
- `test_real_model_run_produces_cost_events` (AC18, needs HARD-02 key).
- (Manual/runner-scripted, evidenced in the report) Grafana-load (AC19) + alert-fire
  (AC20).

**How to run:**
```bash
# Hermetic default (must stay green; no network):
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && make typecheck
cd apps/web && pnpm test

# Cost ledger on real Postgres (HARD-01 substrate):
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/forge_test
uv run pytest -m postgres -q

# Dashboard/rule/alert validation (needs promtool/amtool on PATH):
promtool check rules deploy/observability/prometheus/rules/*.yml
promtool check config deploy/observability/prometheus/prometheus.yml
amtool check-config deploy/observability/alertmanager/alertmanager.yml
docker compose --profile observability config -q

# Live pipeline + real-cost (networked runner / HARD-02 creds in .env.integration):
uv run pytest -m integration -q
```

## 8. Security & policy considerations

- **Secret redaction is mandatory + defense-in-depth.** The existing
  `apps/api/forge_api/observability/redaction.py` (`redact_text`/`redact_mapping`,
  token `[REDACTED]`) runs as the **last** logging processor and on span attributes
  before export, so Loki, Tempo, and Prometheus labels never receive a secret. BYOK
  provider keys are used by the model client but never appear in `ModelUsage`, the
  ledger, span attributes, logs, or labels (AC12). New live paths re-apply
  redaction defensively (per the F05 §8 / FORGE_SPEC Security rule).
- **Cardinality = availability.** Unbounded labels (task/workflow/user/workspace
  ids) would explode Prometheus and become a DoS vector; the §4 catalog + the guard
  + allow-list bucketing keep high-cardinality dimensions in Postgres (ledger) and
  Tempo (span attrs) only (AC11).
- **Tenant isolation.** `cost_event`/`model_price` carry `workspace_id`; every cost
  query filters by it and a cross-workspace `scope_id` returns **404** (no existence
  leak, matching the existing observability router's run-trace isolation). Grafana is
  an **ops-facing** infrastructure surface for the self-hoster (not per-tenant),
  behind Caddy auth on the `internal` network.
- **RBAC + audit.** Cost reads require `viewer`+; price-book mutation and reprice
  require `admin` and emit immutable `cost.price_set`/`cost.repriced` audit entries
  through the existing `AuditLog` (the spec's immutable-audit non-negotiable; the
  audit table's DB-trigger immutability is hardened in HARD-01). The ledger is
  append-only except the audited reprice path.
- **No anonymous access / internal `/metrics`.** All `/observability/cost/*` routes
  are authenticated; the Prometheus `/metrics` endpoint is bound to the internal
  network and never routed through the public proxy (AC13/AC16).
- **Billing integrity.** Idempotent `(workspace_id, request_id)` upsert prevents
  double-billing on retries; `forge_cost_emit_failures_total` makes a failed ledger
  write alertable rather than silent; `forge_unpriced_model_total` surfaces pricing
  gaps so a real spend never hides behind a silent $0.
- **Overhead / resilience.** Emission is guarded (telemetry failure never aborts a
  run, AC8); tracing is sampled (`OTEL_TRACES_SAMPLER_ARG`, default 10%); degraded
  mode is a true no-op (AC1). Observability degrades; execution never does.

## 9. Effort & risk

**Effort: L.** Cross-cutting: realize a real OTLP export path + facade + structured
logging across three apps, a relational cost ledger + pricing + reprice on real
Postgres, a cost API (+ thin UI), and the full Grafana/Prometheus/Loki/Tempo/
Alertmanager profile with provisioned dashboards + recording rules + alert rules.
Rough split: telemetry/logging/facade realization (M), cost ledger + pricing + meter
+ reprice (M), wiring `UsageMeter` into the real model/embed/rerank clients + Celery
propagation (M), cost API + UI (S/M), compose profile + dashboards/rules/alerts +
contract tests (M).

**Key risks:**
1. **Metric cardinality explosion.** *Mitigation:* frozen §4 catalog + guard +
   allow-list bucketing + enforcing contract test (AC11); ids in ledger/traces only.
2. **Cost double-count / drift between ledger, trace, counters.** *Mitigation:* one
   emission point, idempotent upsert, an end-to-end agreement test (AC6/AC9).
3. **Secret leak into a permanent sink.** *Mitigation:* reuse the single canonical
   redactor, run last + on span attrs, never put payloads in labels (AC12).
4. **Trace-context loss across Celery.** *Mitigation:* OTel `CeleryInstrumentor` +
   explicit header carrier + eager-mode propagation test (AC17).
5. **Frozen-contract mismatch (no `cost_usd` on `TokenUsage`; steps are JSON).**
   *Mitigation:* cost lives in the ledger; per-step cost in `steps[i].metadata` —
   no contract mutation (§4 reconciliation). Honest, tested.
6. **Trace-backend choice beyond the spec's listed services.** *Mitigation:* default
   to Tempo (Grafana-native), LangSmith optional via env, documented as an ADR.
7. **Pricing staleness (BYOK).** *Mitigation:* effective-dated `model_price` +
   overrides + `forge_unpriced_model_total` + audited reprice (AC7/AC14).

**Cannot be done in-sandbox (named, not hidden):** the live OTLP→collector→
Prometheus/Tempo/Loki roundtrip (AC17), Grafana loading dashboards with live data
(AC19), an alert actually firing/resolving through Alertmanager (AC20), and **real
token/cost numbers from a real BYOK model run** (AC18, needs HARD-02 creds) require
a networked/CI runner and (for AC18) real model creds — they are PRODUCTION-gate
items run on a networked runner, not in the no-network sandbox. A true multi-week,
multi-tenant production telemetry **fleet soak** (dashboard correctness under real
fleet load over weeks) is out of scope here and named in HARD-13's bounded-soak +
the release-notes asterisk.

## 10. Key files / paths

**Extend `apps/api/forge_api/observability/`** (exists):
- `otel.py` — real OTLP exporters + auto-instrumentation + idempotent
  `setup_telemetry`; keep `SpanRecorder`/`span`/`traced`.
- `metrics.py` (new) — `ForgeMetrics` + `NoopMetrics` + cardinality guard + `get_metrics()`.
- `logging.py` (new) — `configure_logging`/`get_logger`/`bind_context` (JSON + trace-context + redaction-last).
- `redaction.py` (exists) — reused as the last processor.
- `service.py` (extend) — add cost read methods.
- `cost/{models,pricing,meter,repository}.py` (new).
- `routers/observability.py` (extend) — cost endpoints + internal `/metrics`.

**Shared seam `packages/observability/forge_obs/`** (realizes F38; re-exported by
`forge_api.observability`): `settings.py`, `telemetry.py`, `metrics.py`,
`logging.py`, `tracing.py`, `cost/*`, `tests/`.

**`packages/db/`:**
- `forge_db/models/cost.py` — SQLAlchemy 2.x ORM for `cost_event`, `model_price`
  (singular tables, `WorkspaceScopedModel`/`TimestampMixin`).
- `migrations/versions/0003_cost_ledger.py` — both tables + indexes + default-price
  seed (`down_revision = "0002_pm_adapters"`).

**`apps/worker/forge_worker/`:**
- `observability/init.py` — worker telemetry + Celery signal instrumentation + `UsageMeter` injection.
- `agent_runner.py` (extend) — wrap model/embed/rerank calls → `UsageMeter.record`; stamp step cost.
- `tasks/observability.py` — `cost.reprice`, `obs.refresh_freshness_gauges`.
- `tests/test_cost_end_to_end.py`, `tests/test_trace_propagation.py`.

**`apps/mcp-gateway/forge_mcp_gateway/observability/init.py`** — gateway telemetry +
`forge_mcp_*` emission + inbound context.

**`apps/web/`** — task **Cost** view, project Insights→Cost, `components/cost/*`,
`lib/api/cost.ts`, a Grafana deep-link, tests.

**`deploy/`:**
- `docker-compose.yml` — fill the `observability` profile (`otel-collector`,
  `prometheus`, `grafana`, `loki`, `tempo`, `alertmanager`) + `internal: true`
  `observability` network.
- `observability/otel-collector/config.yaml`, `observability/prometheus/{prometheus.yml,rules/forge.rules.yml,rules/forge.alerts.yml}`,
  `observability/loki/loki-config.yaml`, `observability/tempo/tempo.yaml`,
  `observability/alertmanager/alertmanager.yml`,
  `observability/grafana/provisioning/{datasources,dashboards}/*`,
  `observability/grafana/dashboards/{workflow-quality,agent-quality,retrieval-quality,cost}.json`.
- `caddy/Caddyfile` — `/grafana` reverse-proxy behind auth.
- `.env.example` / `.env.production.example` — `OBS_*`, `OTEL_*`, `LOKI_*`,
  `TEMPO_*`, `GRAFANA_*`, `COST_*` (reuse `OTEL_EXPORTER_OTLP_ENDPOINT`,
  `LANGSMITH_API_KEY`, `MODEL_PROVIDER*`, `FORGE_GRAFANA_WEBHOOK_SECRET`).
- `tests/test_observability_profile.py`, `tests/test_grafana_dashboards.py`,
  `tests/test_prometheus_rules.py`.

**`.github/workflows/ci.yml`** — add `--profile observability config` to the
`compose` job; new `observability` job (`promtool`/`amtool` + contract tests);
networked job for AC17–AC20.

**`docs/`** — `docs/self-hosting/observability.md` (enabling the profile,
dashboards, alert runbooks, log/trace correlation), `docs/architecture/adr-NNNN-trace-backend-tempo.md`.

## 11. Research references

- FORGE_SPEC `docs/FORGE_SPEC.md` — Observability Layer ("Run traces, token/cost
  logs, task lineage, retrieval debug, eval harness"); Technology Stack
  ("OpenTelemetry + Prometheus + Grafana + Loki + LangSmith"); Observability and
  Evaluation → **Key Metrics** (incl. "Cost: token cost per task, per workflow
  phase, per model provider"); Security (secret redaction, immutable audit, rate
  limiting); Production Docker Compose Requirements (digest pin, non-root,
  healthchecks, limits, network segmentation, capped logs); MCP Security Rules
  ("full audit log: tool name, payload hash, result status, latency").
- `docs/MORNING_REPORT.md` — §1.14 (observability DONE / DB-trigger immutability
  PARKED), §5 (PARKED items), §6 (no real infra, low worker/agent coverage, no
  load/perf), §7 (ranked next steps).
- `docs/implementation-slices/cross-cutting/F38-observability-cost-metrics.md` — the
  product slice this hardens (cost ledger, facade, dashboards, cardinality contract).
- `docs/implementation-slices/v1/F10-run-trace-viewer.md` — per-run trace that reads
  the per-step cost; `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md`
  — `RetrievalDebug`/retrieval-debug source; `docs/implementation-slices/v1/F14-docker-compose-selfhost.md`
  — base compose + observability profile/network + hardening invariants.
- `scratchpad/hardening-docs/SPEC-PRODUCTION-HARDENING.md` — HARD-01 (real PG),
  HARD-02 (real model + token/cost), HARD-03 (real embedder/reranker), HARD-08
  (build + digest pin), HARD-13 (perf p50/p95/p99, bounded soak), gate `G-CRYPTO`.
- OpenTelemetry Python (OTLP exporters, auto-instrumentors, sampling): https://opentelemetry.io/docs/languages/python/
- Prometheus (recording + alerting rules, `promtool check`): https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/ · https://prometheus.io/docs/alerting/latest/alertmanager/
- Grafana provisioning (datasources + dashboards as code): https://grafana.com/docs/grafana/latest/administration/provisioning/
- Grafana Loki (logs) + Tempo (traces): https://grafana.com/docs/loki/latest/ · https://grafana.com/docs/tempo/latest/
- Docker Compose production hardening 2026: https://distr.sh/blog/running-docker-in-production/

## 12. Out of scope / future

- **Producing the metric values** — F02/F05/F06/F07/F08/F09/F12 decide *when*
  events happen; HARD-10 ships the facade + pipeline + ledger they call and proves
  it real. The offline golden-suite gate stays F12/HARD-04; this is the online sink.
- **The per-run, step-level trace viewer rendering** — F10 owns the UI; HARD-10 only
  returns/stamps the per-step cost and emits aggregate series.
- **Multi-currency / FX cost + budget enforcement (hard caps, overspend alerts as
  policy)** — V1 stores USD + reports; budget guardrails are future FinOps work.
- **Per-tenant Grafana dashboards** — V1 Grafana is ops-facing for the self-hoster;
  per-team dashboards are future multi-team work (F30).
- **RUM / frontend performance telemetry + distributed profiling** — future.
- **Kubernetes/Helm observability templating** — V2 (F24); HARD-10 provides the
  provisioned configs the chart will template.
- **A real multi-week, multi-tenant production telemetry fleet soak** (dashboard +
  alert correctness under real fleet load over weeks) — not reproducible in-sandbox;
  HARD-13 delivers a bounded simulated soak and the release-notes asterisk names the
  remaining external item.
- **A 3rd-party human review of the alert/runbook coverage** — automated rule
  validation (AC15/AC20) is in scope; an operational on-call review remains an
  external punch-list item alongside HARD-09's pentest hand-off.
