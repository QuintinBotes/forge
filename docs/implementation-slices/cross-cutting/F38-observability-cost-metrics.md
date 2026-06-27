# F38 — Observability & Cost Metrics

> Phase: cross-cutting · Spec module(s): Observability Layer (token/cost logs, retrieval debug, Key Metrics), Technology Stack (OpenTelemetry + Prometheus + Grafana + Loki + LangSmith), Self-Hosting & Deployment (the `observability` Compose profile stubbed by F14), Security (secret redaction in logs/traces, per-workspace isolation, audit) · Status target: **Done** = every Forge service (`api`, `worker`, `mcp-gateway`) boots through one shared `forge_obs` telemetry init that emits OTel traces, metrics, and structured JSON logs over OTLP to an `otel-collector`; the spec's **Key Metrics** (workflow-quality, agent-quality, retrieval-quality, cost) are emitted through a single typed `ForgeMetrics` facade with bounded label cardinality; **every model call flows through one `UsageMeter`** that atomically (a) writes a durable, queryable `cost_event` ledger row priced from a `model_price` table, (b) returns the cost so F06's step sink stamps it onto the per-step trace field it owns (`agent_steps.token_usage.cost_usd`, later read by F10), and (c) increments the Prometheus cost counters — so cost is never double-counted; an opt-in `docker compose --profile observability up` brings up `otel-collector` + `prometheus` + `grafana` + `loki` + `tempo` with **provisioned-as-code** datasources and four dashboards; structured logs carry `trace_id`/`workspace_id` and pass through the shared secret-redaction filter so secrets never reach Loki, traces, or metric labels; the in-app **Cost** API + page expose per-task / per-phase / per-provider spend, workspace-isolated; and `forge_obs` ships a **no-op fallback** so producers (F05–F12) call the facade unconditionally and degrade to zero overhead when `OBS_ENABLED=false`. Lint + types + `pytest` green on `packages/observability/forge_obs`, the cost router/service, and the worker/mcp-gateway init; new-code coverage ≥ 80%; a YAML-parsing contract test validates every Grafana dashboard and Prometheus rule file.

---

## 1. Intent — what & why

The spec names the **Observability Layer** as a first-class module ("Run traces, token/cost logs, task lineage, retrieval debug, eval harness") and pins the stack in the Technology Stack table: *"Observability: OpenTelemetry + Prometheus + Grafana + Loki + LangSmith — Standard self-hostable stack."* The "Observability and Evaluation → **Key Metrics**" section enumerates exactly which numbers must exist (workflow quality, agent quality, retrieval quality, and **Cost: token cost per task, per workflow phase, per model provider**). F14 (docker-compose self-host) deliberately ships the `prometheus`/`grafana`/`loki` Compose **profile as a stub** and defers the real dashboards/config to "`cross-cutting/observability`" — that is this slice.

F38 is the **live, aggregate, system-wide** observability substrate. It is deliberately distinct from its two siblings:

- **F10 (Run Trace Viewer)** owns the *per-run, step-level* trace (one run's ordered `agent_steps`, SSE viewer, per-run cost rollup read from `agent_steps.token_usage.cost_usd`). It is the microscope.
- **F12 (Eval Harness)** owns the *offline, golden-suite* quality gate (deterministic metrics computed against fixed cases, regression gate in CI). It is the lab bench.
- **F38 (this slice)** owns the *online, fleet-wide* telemetry: OTel instrumentation across all services, the Prometheus/Grafana/Loki/Tempo pipeline, the typed Key-Metric emission facade, the **durable cost ledger** that aggregates spend across all runs, and the in-product cost views + ops dashboards. It is the control room.

Concretely F38 delivers six things the spec mandates but no other slice owns:

1. **One telemetry init (`forge_obs.setup_telemetry`)** that every app calls at startup: OTel `TracerProvider` + `MeterProvider` + `LoggerProvider` with OTLP exporters, auto-instrumentation of FastAPI / SQLAlchemy / httpx / Celery / Redis, W3C trace-context propagation across `api → Celery → mcp-gateway`, and resource attributes (`service.name`, `forge.version`, deployment env).
2. **A typed Key-Metric facade (`ForgeMetrics`)** — one method per spec metric family, mapping to OTel instruments exported as Prometheus series with **bounded** label cardinality (no `task_id`/`workspace_id` on Prometheus labels by default; high-cardinality dimensions live in the ledger and traces).
3. **A single cost-emission path (`UsageMeter` + `model_price`)** — the only place token usage becomes money. It writes the durable `cost_event` row, increments the cost counters, and returns the computed `cost_usd` that F06's step sink stamps onto `agent_steps.token_usage.cost_usd` (the per-step trace field F10 reads). One emission, three sinks → no double counting.
4. **Structured, redacted, trace-correlated logging** — JSON logs with `trace_id`/`span_id`/`workspace_id`, run through the shared secret-redaction filter, shipped to Loki; satisfies Security → "Secrets stripped from logs, traces, and retrieval results."
5. **The real `observability` Compose profile** — `otel-collector`, `prometheus`, `grafana`, `loki`, `tempo`, all hardened to the spec's production rules (pinned digests, non-root, healthchecks, resource limits, `internal` network), with Grafana datasources + four dashboards provisioned as code.
6. **The in-product cost surface** — a workspace-isolated Cost API and page (per task / per phase / per provider, over time) that turns the ledger into the spec's "token/cost logs" the team actually reads.

Why cross-cutting and not per-app: instrumentation, metric naming, cardinality policy, log schema, and cost emission must be **uniform** across every service, or dashboards and the cost ledger are inconsistent and untrustworthy. Centralizing them in `forge_obs` lets every other slice simply *call the facade* against a frozen contract while a single test suite enforces cardinality, redaction, and dashboard validity.

**F38 does NOT** produce the metric *values* — F06/F07/F08 decide when an agent retried or a task completed; F05 measures retrieval latency; F02/F12 compute spec completeness. F38 ships the facade those slices call and the pipeline that stores/visualizes the result. It also does **not** own the per-step trace (`agent_steps` is written by F06 and rendered by F10) or the offline golden gate (F12); the shared cost value flows one way — `UsageMeter` (F38) computes it, F06 stamps it onto the step it owns, F10 reads it — and F38 can *optionally* receive F12's eval metrics through the same facade.

---

## 2. User-facing behavior / journeys

**Journey A — Engineer reads task spend (in-product).** An engineer opens `TASK-123` → **Cost** tab. The page shows total spend ($0.37), a breakdown by workflow phase (`spec_drafting $0.04`, `executing $0.28`, `verifying $0.05`) and by provider/model (`anthropic/claude-… $0.31`, `openai/text-embedding-… $0.06`), and prompt/completion token totals — all read from the `cost_event` ledger, scoped to the engineer's workspace.

**Journey B — Team lead reads spend trends (in-product).** A team lead opens **Project → Insights → Cost**: a time-series of daily spend for the project (stacked by provider), the top-10 most expensive tasks this sprint, and the cost-per-merged-PR. Filters by date range and provider. No Grafana login required for this product-level view.

**Journey C — Operator opens Grafana dashboards (ops-facing).** A self-hoster who ran `docker compose --profile observability up` opens Grafana (behind Caddy auth at `/grafana`). Four pre-provisioned dashboards load with live data: **Workflow Quality** (task-completion rate, approval accept/reject, PR acceptance, time-to-merge p50/p95, spec completeness), **Agent Quality** (retry rate, failure rate, confidence histogram, requirement-satisfaction), **Retrieval Quality** (hybrid hit rate, reranker delta, MCP freshness lag, retrieval latency p50/p95/p99), and **Cost** (token cost rate by provider/model/phase, cumulative spend). Datasources (Prometheus, Loki, Tempo) are already wired.

**Journey D — Operator debugs a slow/failed run via trace↔log↔metric correlation.** An alert fires (retrieval p99 latency over budget). The operator pivots from the Grafana panel to Tempo, opens an exemplar trace for a slow `knowledge.search`, sees the span breakdown (`semantic 40ms`, `keyword 35ms`, `rerank 410ms`), clicks the span's `trace_id` to jump into Loki and reads the structured logs for that exact request — secrets shown as `«redacted»`. The trace also links back to the in-product F10 step view via `workflow_run_id`.

**Journey E — Operator runs the lean default stack (no observability overhead).** A small-team operator runs the base `docker compose up` (no `--profile observability`). All services still boot; `forge_obs` detects no collector and runs in **degraded mode** — metric/trace export is a no-op, structured logs still print to stdout (captured by Docker's capped JSON logs), the cost ledger still records (it is Postgres, not the collector), and the in-product Cost page still works. Zero crashes, near-zero overhead.

**Journey F — Cost API consumer / FinOps export.** An admin calls `GET /api/v1/cost/timeseries?scope=workspace&group_by=provider&bucket=day&from=…&to=…` to feed an internal budget tool. A `viewer` can read cost for their own workspace; a cross-workspace request returns 404 (no existence leak).

**Journey G — Pricing update / re-price.** An admin adds a new `model_price` row (a new model the workspace adopted via BYOK). New model calls are priced immediately; the `forge-cli cost reprice --from <date>` command re-computes `cost_usd` for historical `cost_event` rows whose `(provider, model)` price changed (idempotent, audited).

---

## 3. Vertical slice

> Layout note: follows the repo's evolved layout used by F10/F12 — apps are `apps/api/forge_api`, `apps/worker/forge_worker`, `apps/mcp-gateway/forge_mcp_gateway`; the shared ORM models live in `packages/db/forge_db/models/` and the single Alembic history lives at `packages/db/migrations/versions/` (sibling to `forge_db`, as F06/F07/F10/F12 use). F38 introduces one new shared package, `packages/observability/forge_obs`, because telemetry must be importable by **all** apps and packages (the spec's package list is illustrative; F10 and F12 likewise introduced `packages/db`). Deploy assets extend the `deploy/` tree and the `observability` Compose profile/network that F14 already declared as stubs.

### 3.1 Data model (tables/columns/migrations touched)

One additive migration `packages/db/migrations/versions/0NNN_cost_ledger.py` (same shared linear Alembic history as F06/F07/F10/F12, sibling to the `forge_db` package — not nested under it), chained on the current head of that history (the latest of F06 `0006_agent_runtime` which creates `agent_runs`/`agent_steps`, F07 `workflow_transition`, F10's trace-read indexes, and F12's eval tables — `down_revision` = that head). It adds two tables. Metrics/traces/logs are **not** stored in Postgres (they live in Prometheus/Tempo/Loki); only the durable **cost ledger** and **price book** are relational, because cost is queried, aggregated, and re-priced transactionally and must survive a metrics-stack outage.

**New table `cost_event`** (durable system of record for token cost; the spec's "token/cost logs")

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | tenant isolation; indexed |
| `project_id` | `uuid` NULL FK → `projects.id` | nullable for non-project model calls |
| `task_id` | `uuid` NULL FK → `tasks.id` | enables per-task cost (spec Key Metric) |
| `workflow_run_id` | `uuid` NULL FK → `workflow_run.id` | per-run rollup join (F07) |
| `agent_run_id` | `uuid` NULL FK → `agent_runs.id` | F06 |
| `step_id` | `uuid` NULL FK → `agent_steps.id` | links the cost to the exact F06 trace step (rendered by F10) |
| `phase` | `text` NULL | FSM state when the call happened (`spec_drafting`,`executing`,…) — per-phase cost |
| `kind` | `text` NOT NULL | `completion` \| `embedding` \| `rerank` (priced call classes) |
| `provider` | `text` NOT NULL | e.g. `anthropic`, `openai`, `jina` (BYOK provider id) |
| `model` | `text` NOT NULL | model id |
| `prompt_tokens` | `integer` NOT NULL DEFAULT 0 | |
| `completion_tokens` | `integer` NOT NULL DEFAULT 0 | |
| `total_tokens` | `integer` GENERATED ALWAYS AS (`prompt_tokens + completion_tokens`) STORED | |
| `cost_usd` | `numeric(14,8)` NOT NULL | computed at emission from `model_price` |
| `price_id` | `uuid` NULL FK → `model_price.id` | the price row used (audit + reprice provenance) |
| `request_id` | `text` NOT NULL | idempotency key (provider response id / generated); dedups retries |
| `occurred_at` | `timestamptz` NOT NULL | when the model call completed |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

Constraints / indexes:
- `UNIQUE (workspace_id, request_id)` — idempotent emission (a retried record is a no-op upsert; prevents double-billing).
- `INDEX (workspace_id, occurred_at)` — workspace time-series.
- `INDEX (task_id)` and `INDEX (workflow_run_id)` — per-task / per-run rollups.
- `INDEX (workspace_id, provider, model, occurred_at)` — per-provider breakdown.
- `INDEX (workspace_id, phase, occurred_at)` — per-phase breakdown.
- **Append-only**: rows are INSERTed once; the only permitted UPDATE is `cost_usd`/`price_id` via the audited `reprice` path (Journey G). Enforced in `CostLedgerRepository`.

**New table `model_price`** (BYOK price book; supports per-workspace overrides + global defaults)

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NULL FK → `workspaces.id` ON DELETE CASCADE | NULL = global default; non-null = workspace override |
| `provider` | `text` NOT NULL | |
| `model` | `text` NOT NULL | |
| `kind` | `text` NOT NULL | `completion` \| `embedding` \| `rerank` |
| `prompt_usd_per_1k` | `numeric(14,8)` NOT NULL DEFAULT 0 | |
| `completion_usd_per_1k` | `numeric(14,8)` NOT NULL DEFAULT 0 | |
| `currency` | `text` NOT NULL DEFAULT `'USD'` | V1 stores USD; field reserved for V2 FX |
| `effective_from` | `timestamptz` NOT NULL DEFAULT now() | enables historical pricing |
| `created_by` | `uuid` NULL FK → `users.id` | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

Index: `INDEX (provider, model, kind, effective_from DESC)` and `INDEX (workspace_id, provider, model, kind, effective_from DESC)`. Price resolution = newest `effective_from <= occurred_at`, preferring a `workspace_id` match over global. A seed migration inserts sane global defaults for the providers Forge ships with (and is overridable; unknown models price at `0` and raise an `unpriced_model` warning metric so gaps are visible, never silently dropped).

`alembic downgrade` drops both tables cleanly.

**Read-only dependencies (not created here):** `agent_steps` (F06) — `step_id` FK target + the place F06 stamps the returned `cost_usd` into `agent_steps.token_usage.cost_usd` (the per-step trace field F10 reads); `workflow_run` (F07) / `agent_runs` (F06); `workspaces`/`users` (`cross-cutting/F00-platform-foundation`); `projects`/`tasks` (`v1/F01-project-board`).

### 3.2 Backend (FastAPI routes + services/packages)

**New shared package `packages/observability/forge_obs/`** (framework-agnostic core; importable by every app + package):

```
packages/observability/forge_obs/
├── __init__.py
├── settings.py        # ObsSettings (env-driven: endpoints, sampling, toggles)
├── telemetry.py       # setup_telemetry(); Telemetry handle; shutdown(); resource attrs
├── instrumentation.py # FastAPI/SQLAlchemy/httpx/Celery/Redis auto-instrument helpers + context propagation
├── metrics.py         # ForgeMetrics facade + NoopMetrics + instrument registry (names/labels in §4)
├── logging.py         # configure_logging(); get_logger(); JSON renderer + trace-context + redaction processor
├── redaction.py       # thin adapter over F37's forge_auth.redaction.SecretRedactor (no new patterns)
├── cost/
│   ├── models.py      # ModelUsage, CostRecord, ModelPrice, CostSummary, CostBucket (Pydantic, §4)
│   ├── pricing.py     # PriceBook Protocol + DbPriceBook; compute_cost(usage, price) -> Decimal
│   ├── meter.py       # UsageMeter (the single emission point) + NoopUsageMeter
│   └── repository.py  # CostLedgerRepository (upsert, rollups, reprice), CostReadRepository
└── tracing.py         # traced() decorator/CM + standard span attributes + cardinality guard
```

- `setup_telemetry(service_name, settings)` returns a `Telemetry` handle and is **idempotent** (safe double-call in tests). When `settings.enabled is False` or no OTLP endpoint resolves, it installs **no-op** providers and returns a handle whose `metrics` is `NoopMetrics()` (Journey E).
- `ForgeMetrics` is the only way feature slices touch metrics. It holds pre-created OTel instruments and exposes one typed method per Key Metric (§4). A module-level `get_metrics()` returns the process singleton (real or no-op).
- `UsageMeter.record(usage)` is the **only** way a model call becomes cost: it resolves price via `PriceBook`, computes `cost_usd`, upserts the `cost_event` row (idempotent on `request_id`), increments the cost counters via `ForgeMetrics`, and **returns the `CostRecord`** (incl. `cost_usd` + `cost_event.id`) so F10's recorder can stamp it onto the step. A `NoopUsageMeter` (cost still persisted; metrics no-op) is used when the metrics stack is off.

**App `apps/api/forge_api/`:**

- `observability/init.py` — calls `setup_telemetry("forge-api", ...)` + `configure_logging(...)` in the FastAPI lifespan startup; registers the `/metrics` Prometheus scrape endpoint (internal network only) and an OTLP-push fallback.
- `api/v1/cost.py` — router mounted at `/api/v1/cost`; auth + workspace membership required; `viewer`+ may read (read-only surface). Endpoints in §4.
- `services/cost_service.py` — `CostService` wires `CostReadRepository` + workspace-isolation guard (404 on cross-workspace ids) + RBAC (via F37's `require_role`/`get_principal`); admin-only for the `reprice` and price-book mutation endpoints, which emit `cost.price_set`/`cost.repriced` events through F39's `AuditSink` (`forge_contracts.audit.AuditEvent`).
- `schemas/cost.py` — request/response schemas (re-export `forge_obs.cost.models` for OpenAPI).
- `cli/cost.py` — `forge-cli cost reprice --from <date> [--provider --model]` (Journey G), `forge-cli cost price set …`, `forge-cli cost summary --task <id>`.

**App `apps/worker/forge_worker/observability/init.py`** — `setup_telemetry("forge-worker", ...)`, Celery signal-based instrumentation (`worker_process_init` → init; `task_prerun`/`task_postrun` carry the propagated trace context so a workflow span spans api→worker). Provides the worker-side `UsageMeter` instance injected into F06's model client and F05's retriever/reranker.

**App `apps/mcp-gateway/forge_mcp_gateway/observability/init.py`** — `setup_telemetry("forge-mcp-gateway", ...)`; emits `forge_mcp_*` metrics on every tool call (latency, status) and accepts inbound trace context so MCP calls appear in the same trace as the agent run.

All three apps expose a hardened internal `/metrics` endpoint and push OTLP to `otel-collector`; both paths are supported so Prometheus can scrape **or** the collector can fan out (configurable).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

F38 adds **two** small Celery tasks and the worker-side wiring; it does not own any LangGraph graph.

- **Wiring (the main job):** the worker constructs the `UsageMeter` (DB session + `DbPriceBook` + `ForgeMetrics`) once per process and injects it into the model client / embedding client / reranker client seams used by F06 (agent loop), F05 (retrieval), and F08 (verification). Every model/embedding/rerank call goes `client.call(...) → UsageMeter.record(ModelUsage(...)) → CostRecord`. The `CostRecord.cost_usd` is written by F06's step sink (`RuntimeDeps.step_sink`) into the `agent_steps.token_usage.cost_usd` field F06 already owns (`token_usage` is the shared `forge_contracts.TokenUsage` DTO), so that per-step trace field (which F10 reads) and the `cost_event` row share one source. `phase` is read from the current FSM state (F07). Recording is **guarded**: a ledger/metric write failure logs and is swallowed (cost/observability must never crash a run) — but because the ledger write is the authoritative billing record, a failed ledger insert emits a `forge_cost_emit_failures_total` counter so gaps are alertable.
- **Celery task `cost.reprice(workspace_id, from_iso, provider?, model?) -> dict`** (queue `observability`) — re-prices historical `cost_event` rows whose `(provider, model, kind)` price changed on/after `from_iso`, idempotently, emitting a `cost.repriced` `AuditEvent` through F39's `AuditSink`; backs Journey G / the CLI.
- **Celery task `obs.refresh_freshness_gauges() -> dict`** (Celery beat, every 60s) — sets the `forge_mcp_freshness_lag_seconds` gauge per MCP connection from the last sync timestamp (F09/F20), since freshness lag is a sampled gauge, not an event.

Trace-context propagation across the Celery boundary uses the OTel `CeleryInstrumentor` + an explicit header carrier on `apply_async` so a span started in `api` continues into the worker task and on into `mcp-gateway` — producing one end-to-end trace per workflow run, the spec's "task lineage."

### 3.4 Frontend / UI (Next.js routes/components, if any)

The in-product cost surface (ops dashboards live in Grafana, §3.5). App package `apps/web`:

- `app/(app)/projects/[projectId]/tasks/[taskId]/cost/page.tsx` — the **Cost** tab on a task: total, by-phase breakdown, by-provider/model breakdown, token totals. TanStack Query against `GET /api/v1/tasks/{taskId}/cost`. (Coexists with F10's per-run trace cost; this is the task-aggregate view.)
- `app/(app)/projects/[projectId]/insights/cost/page.tsx` — **Project → Insights → Cost**: daily spend time-series (stacked by provider, Recharts/visx), top-N expensive tasks table (TanStack Table), cost-per-merged-PR stat. Date-range + provider filters.
- `components/cost/CostBreakdown.tsx` — reusable grouped bar/table for a `CostSummary` (used by both pages and embeddable in F10's run header).
- `components/cost/CostTimeseriesChart.tsx` — stacked-area time-series from `CostTimeseries`.
- `components/cost/CostStat.tsx` — single headline stat (total / per-PR).
- `lib/api/cost.ts` — typed client matching §4; hooks `useTaskCost(taskId)`, `useCostSummary(params)`, `useCostTimeseries(params)`.
- (Optional) a settings link `app/(app)/settings/observability/page.tsx` that deep-links to Grafana (`/grafana`) and shows the price-book table (admin-editable) — the only mutating UI, admin-gated.

Keyboard-first per board UX standard: the Insights cost page supports `f` to focus filters, date-range arrow-key stepping, and `[`/`]` to switch grouping. No new design tokens — reuses `ui-kit`.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

F38 fills in the `observability` Compose **profile** that F14 declared as a stub (F14 owns the base file + hardening invariants; F38 owns the observability services' real config + dashboards). All services obey the spec's Production Docker Compose Requirements: pinned `@sha256`, non-root, healthcheck, CPU/mem limits, capped logs, named volumes, the `autoheal=true` label (F14 owns the `willfarrell/docker-autoheal` sidecar), on the `internal: true` `observability` network.

- **`deploy/docker-compose.yml`** (extend, profile `observability`):
  - `otel-collector` (OTLP gRPC/HTTP receiver → exports metrics to Prometheus remote-write/scrape, traces to Tempo, logs to Loki). Config: `deploy/observability/otel-collector/config.yaml`.
  - `prometheus` — scrapes `otel-collector` + each app `/metrics`; config `deploy/observability/prometheus/prometheus.yml`; recording rules `deploy/observability/prometheus/rules/forge.rules.yml` (rate/percentile rollups for dashboards); named volume `prometheus-data`.
  - `grafana` — provisioned datasources (`deploy/observability/grafana/provisioning/datasources/*.yaml`: Prometheus, Loki, Tempo) + dashboards (`provisioning/dashboards/forge.yaml` → JSON in `deploy/observability/grafana/dashboards/*.json`); named volume `grafana-data`; admin password from env.
  - `loki` — log store; config `deploy/observability/loki/loki-config.yaml`; named volume `loki-data`.
  - `tempo` — trace store (the Grafana-stack trace backend; chosen as the OTel trace sink because the spec already mandates Grafana+Loki — Tempo is their native trace companion); config `deploy/observability/tempo/tempo.yaml`; named volume `tempo-data`. *(LangSmith remains an optional additional trace sink per the spec's stack list; configured via `LANGSMITH_*` env when present — Tempo is the self-hosted default.)*
- **Log shipping:** apps emit logs via OTLP to `otel-collector → loki` (primary); a `promtail`/Docker-driver fallback is documented for the lean stack. Either way logs are pre-redacted at the app (defense in depth), so Loki never holds a secret.
- **`deploy/caddy/Caddyfile`** — add a `handle_path /grafana/*` reverse-proxy to `grafana:3000` behind Caddy basicauth/forward-auth; Grafana is **not** exposed directly (data tier rule). Document the Nginx equivalent in `deploy/nginx/forge.conf`.
- **Networks:** the `observability` network stays `internal: true` (F14 invariant); `otel-collector` is the only bridge — apps push to it from the `backend` network where the collector also attaches. No observability service publishes a host port except via Caddy.
- **Env (`deploy/.env.example`, `.env.production.example`):** `OBS_ENABLED` (default `false` in base, `true` under the profile), `OTEL_EXPORTER_OTLP_ENDPOINT` (e.g. `http://otel-collector:4317`), `OTEL_TRACES_SAMPLER` + `OTEL_TRACES_SAMPLER_ARG` (default parent-based ratio `0.1`), `OTEL_SERVICE_NAME` (per app), `OBS_METRIC_WORKSPACE_LABEL` (default `false` — cardinality guard), `LOKI_ENDPOINT`, `TEMPO_ENDPOINT`, `GRAFANA_ADMIN_PASSWORD`, `COST_DEFAULT_CURRENCY=USD`, `COST_UNPRICED_MODEL_BEHAVIOR=warn` (`warn`|`error`), `LANGSMITH_API_KEY`/`LANGSMITH_PROJECT` (optional).
- **Helm:** N/A — V1 ships Docker Compose only; the Helm chart (F24, V2) will template these same provisioned configs.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Settings + telemetry (`forge_obs/settings.py`, `telemetry.py`):**

```python
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class ObsSettings:
    enabled: bool = False
    service_name: str = "forge"
    version: str = "0.0.0"
    environment: str = "dev"
    otlp_endpoint: str | None = None          # None -> no-op exporters
    traces_sampler_ratio: float = 0.1
    metric_workspace_label: bool = False       # cardinality guard
    prometheus_scrape_enabled: bool = True

class Telemetry:
    metrics: "ForgeMetrics"
    def shutdown(self) -> None: ...            # flush + close exporters

def setup_telemetry(service_name: str, settings: ObsSettings) -> Telemetry:
    """Idempotent. Installs OTel Tracer/Meter/Logger providers + OTLP exporters
    and auto-instruments FastAPI/SQLAlchemy/httpx/Celery/Redis. When
    settings.enabled is False or otlp_endpoint is None, installs no-op providers
    and a NoopMetrics()."""
```

**Key-Metric facade (`forge_obs/metrics.py`) — implemented by F38, called by F02/F05/F06/F07/F08/F09/F12:**

```python
from typing import Protocol, Mapping

class ForgeMetrics(Protocol):
    # --- Cost (called only via UsageMeter) ---
    def record_model_cost(self, *, provider: str, model: str, kind: str,
                          phase: str | None, prompt_tokens: int,
                          completion_tokens: int, cost_usd: float) -> None: ...
    # --- Workflow quality ---
    def record_workflow_terminal(self, *, workflow: str, terminal_state: str,
                                 duration_seconds: float) -> None: ...
    def record_task_completion(self, *, status: str,
                               duration_seconds: float | None) -> None: ...
    def record_approval_decision(self, *, gate: str, decision: str) -> None: ...
    def record_pr_outcome(self, *, outcome: str,
                          time_to_merge_seconds: float | None) -> None: ...
    def observe_spec_completeness(self, *, score: float) -> None: ...
    # --- Agent quality ---
    def record_agent_run(self, *, status: str, skill_profile: str,
                         retries: int, confidence: float | None) -> None: ...
    def observe_requirement_satisfaction(self, *, ratio: float) -> None: ...
    # --- Retrieval quality ---
    def record_retrieval(self, *, hit: bool, total_latency_seconds: float,
                         stage_latencies: Mapping[str, float],
                         reranker_delta: float | None) -> None: ...
    def record_mcp_call(self, *, connection: str, status: str,
                        latency_seconds: float) -> None: ...
    def set_mcp_freshness_lag(self, *, connection: str, lag_seconds: float) -> None: ...

def get_metrics() -> ForgeMetrics: ...        # process singleton (real or NoopMetrics)
```

**Prometheus instrument catalog (names, types, labels) — the frozen cardinality contract:**

| Metric (Prometheus name) | Type | Labels (bounded) | Spec Key Metric |
|---|---|---|---|
| `forge_model_tokens_total` | Counter | `service,provider,model,phase,token_kind{prompt,completion}` | Cost — token usage |
| `forge_model_cost_usd_total` | Counter | `service,provider,model,phase` | Cost per phase/provider |
| `forge_cost_emit_failures_total` | Counter | `service,reason` | (ledger health) |
| `forge_unpriced_model_total` | Counter | `provider,model` | (price-gap visibility) |
| `forge_workflow_runs_total` | Counter | `workflow,terminal_state` | task completion rate |
| `forge_task_completions_total` | Counter | `status{completed,failed,cancelled}` | task completion rate |
| `forge_approval_decisions_total` | Counter | `gate,decision{approved,rejected,changes_requested}` | approval accept/reject rate |
| `forge_pr_outcomes_total` | Counter | `outcome{merged,closed_unmerged}` | PR acceptance rate |
| `forge_time_to_merge_seconds` | Histogram | `workflow` | mean time to merge |
| `forge_task_duration_seconds` | Histogram | `workflow` | mean time to task completion |
| `forge_spec_completeness` | Histogram (0..1) | — | spec completeness score |
| `forge_agent_runs_total` | Counter | `status{succeeded,failed},skill_profile` | failure rate |
| `forge_agent_retries_total` | Counter | `skill_profile` | retry rate |
| `forge_agent_confidence` | Histogram (0..1) | `skill_profile` | confidence distribution |
| `forge_requirement_satisfaction_ratio` | Histogram (0..1) | — | requirement satisfaction rate |
| `forge_retrieval_requests_total` | Counter | `hit{true,false}` | hybrid search hit rate |
| `forge_retrieval_latency_seconds` | Histogram | `stage{semantic,keyword,fusion,rerank,total}` | retrieval latency p50/p95/p99 |
| `forge_reranker_delta` | Histogram | — | reranker delta |
| `forge_mcp_calls_total` | Counter | `connection,status` | (MCP audit/health) |
| `forge_mcp_call_latency_seconds` | Histogram | `connection` | MCP call latency |
| `forge_mcp_freshness_lag_seconds` | Gauge | `connection` | MCP freshness lag |

Cardinality rules (enforced by `tracing.py`/`metrics.py` guards + a contract test): **no `task_id`, `workflow_run_id`, `user_id`, or raw `workspace_id`** as a metric label; `workspace_id` is added **only** if `OBS_METRIC_WORKSPACE_LABEL=true`. High-cardinality dimensions are carried on **trace span attributes** and the **cost ledger** instead. `provider`/`model`/`connection`/`skill_profile`/`phase` are bounded by config and validated against allow-lists; an unknown value is bucketed to `"other"`.

**Cost models (`forge_obs/cost/models.py`):**

```python
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field

class ModelUsage(BaseModel):
    workspace_id: UUID
    request_id: str                         # idempotency key
    provider: str
    model: str
    kind: str = "completion"                # completion | embedding | rerank
    prompt_tokens: int = 0
    completion_tokens: int = 0
    occurred_at: datetime
    project_id: UUID | None = None
    task_id: UUID | None = None
    workflow_run_id: UUID | None = None
    agent_run_id: UUID | None = None
    step_id: UUID | None = None
    phase: str | None = None

class ModelPrice(BaseModel):
    id: UUID | None = None
    provider: str
    model: str
    kind: str
    prompt_usd_per_1k: Decimal = Decimal(0)
    completion_usd_per_1k: Decimal = Decimal(0)
    currency: str = "USD"
    effective_from: datetime

class CostRecord(BaseModel):              # returned by UsageMeter.record
    cost_event_id: UUID
    cost_usd: Decimal
    priced: bool                          # False if no price matched (cost_usd=0)
    price_id: UUID | None = None

class CostBucket(BaseModel):
    key: str                              # phase | provider | model | day-ISO depending on group_by
    cost_usd: Decimal
    prompt_tokens: int
    completion_tokens: int

class CostSummary(BaseModel):
    scope: str                            # workspace | project | task
    scope_id: UUID
    total_cost_usd: Decimal
    total_prompt_tokens: int
    total_completion_tokens: int
    group_by: str                         # phase | provider | model | none
    buckets: list[CostBucket]
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None

class CostTimeseries(BaseModel):
    scope: str
    scope_id: UUID
    bucket: str                           # hour | day | week
    group_by: str                         # provider | model | phase | none
    series: dict[str, list[tuple[datetime, Decimal]]]   # series_key -> [(ts, cost)]
```

**Cost emission + pricing contracts (`forge_obs/cost/meter.py`, `pricing.py`, `repository.py`):**

```python
from typing import Protocol
from decimal import Decimal
from datetime import datetime

class PriceBook(Protocol):
    async def resolve(self, *, workspace_id, provider, model, kind,
                      at: datetime) -> ModelPrice | None: ...

def compute_cost(usage: ModelUsage, price: ModelPrice | None) -> Decimal:
    """(prompt_tokens/1000)*prompt_usd_per_1k + (completion_tokens/1000)*completion_usd_per_1k.
    price=None -> Decimal(0) (caller marks priced=False and increments forge_unpriced_model_total)."""

class UsageMeter(Protocol):
    async def record(self, usage: ModelUsage) -> CostRecord:
        """Resolve price, compute cost, idempotently upsert cost_event on
        (workspace_id, request_id), increment cost counters, return CostRecord.
        Guarded: a metric-export failure never raises; a ledger-write failure
        increments forge_cost_emit_failures_total and re-raises only in strict mode."""

class CostLedgerRepository(Protocol):
    async def upsert_event(self, usage: ModelUsage, *, cost: Decimal,
                           price_id: UUID | None) -> CostRecord: ...
    async def reprice(self, *, workspace_id: UUID, since: datetime,
                      provider: str | None, model: str | None,
                      price_book: PriceBook) -> int: ...   # returns rows updated

class CostReadRepository(Protocol):
    async def summary(self, *, workspace_id: UUID, scope: str, scope_id: UUID,
                      group_by: str, frm: datetime | None,
                      to: datetime | None) -> CostSummary: ...
    async def timeseries(self, *, workspace_id: UUID, scope: str, scope_id: UUID,
                         bucket: str, group_by: str, frm: datetime | None,
                         to: datetime | None) -> CostTimeseries: ...
```

**Structured logging (`forge_obs/logging.py`):**

```python
def configure_logging(*, service_name: str, settings: ObsSettings) -> None:
    """JSON logs to stdout (+OTLP when enabled). Each record carries
    service, level, ts, msg, trace_id, span_id, and workspace_id when bound.
    A redaction processor runs LAST so secrets never reach any sink."""

def get_logger(name: str): ...
def bind_context(**kv) -> None: ...        # contextvar-backed (e.g. bind_context(workspace_id=...))
```

**REST API (all under `/api/v1`, authenticated; `viewer`+ read; admin for price/reprice):**

```
GET  /tasks/{task_id}/cost
        -> CostSummary                         # group_by=phase by default; per-task spend
GET  /cost/summary?scope=workspace|project|task&scope_id=<id>&group_by=phase|provider|model|none&from=&to=
        -> CostSummary
GET  /cost/timeseries?scope=...&scope_id=...&bucket=hour|day|week&group_by=provider|model|phase|none&from=&to=
        -> CostTimeseries
GET  /cost/prices?provider=&model=
        -> { items: ModelPrice[] }
POST /cost/prices                              (admin)  body: ModelPrice  -> ModelPrice
POST /cost/reprice                             (admin)  body: { scope_id, since, provider?, model? } -> { updated: int }   # enqueues cost.reprice
GET  /metrics                                  (internal network only; Prometheus exposition)
```

Workspace isolation: every cost endpoint filters by the caller's `workspace_id`; a `scope_id` belonging to another workspace returns **404** (no existence leak). `/metrics` is bound to the internal interface / not routed through Caddy.

**Grafana dashboard JSON & Prometheus rules** are validated by a contract test: each `deploy/observability/grafana/dashboards/*.json` must parse, declare a `uid` + `title`, and reference only datasources defined in provisioning; each Prometheus rule file must parse and reference only metrics in the instrument catalog above.

**Consumed contracts (owned elsewhere):**
- `forge_auth.redaction.SecretRedactor` — `cross-cutting/F37-auth-secrets-byok` (also reused by F06/F09/F10); F38 wraps it, adds no patterns.
- `forge_contracts.audit.AuditEvent` + `AuditSink` Protocol — `cross-cutting/F39-audit-log`; F38 emits `cost.price_set`/`cost.repriced` through it on every price-book mutation and reprice.
- `agent_steps.token_usage.cost_usd` per-step field (`token_usage` is the shared `forge_contracts.TokenUsage` DTO) + `StepSink` (`RuntimeDeps.step_sink`) — F06 (F38 returns the `CostRecord` whose `cost_usd` F06 stamps onto the step; F10 reads it).
- FSM `current_state`/`workflow_run` (phase) — F07; `agent_runs` — F06.
- Model/embedding/rerank client seams — F06/F05; the BYOK provider/model identity + encrypted vault (populates `ModelUsage.provider`/`model`) — `cross-cutting/F37-auth-secrets-byok`.

---

## 5. Dependencies — features/slices that must exist first

- `cross-cutting/F00-platform-foundation` — **hard.** uv workspace + app factories (FastAPI lifespan hooks to call `setup_telemetry`), async SQLAlchemy session + Alembic baseline `0001_foundations` in `packages/db` (`forge_db`: `Base`, `TimestampMixin`, naming convention), Celery app + queues + Beat, `forge_contracts` shared DTOs (incl. `TokenUsage`), the `workspaces`/`users` tables, and `forge-cli`. *(This Phase-0 substrate has no dedicated numbered file yet; sibling slices reference it variously as `cross-cutting/F00-platform-foundation` / `cross-cutting/F00-foundation` / `v1/F00-foundation-substrate` — reconcile to the final foundation slug when it lands; its container packaging is owned by `v1/F14-docker-compose-selfhost`.)*
- `cross-cutting/F37-auth-secrets-byok` — **hard.** Provides `forge_auth.redaction.SecretRedactor` (the canonical redaction filter F38 wraps), the `require_role(...)` / `get_principal` RBAC dependency + role ranks (`viewer`/`member`/`admin`/`agent-runner`), and the encrypted BYOK vault + provider/model identity that populate `ModelUsage.provider`/`model`.
- `cross-cutting/F39-audit-log` — **hard (mutate path).** Provides `forge_contracts.audit.AuditEvent` + the `AuditSink` Protocol; F38 emits `cost.price_set`/`cost.repriced` audit events through it on every price-book mutation and reprice (the spec's immutable-audit non-negotiable). Read-only cost endpoints do not require it.
- `v1/F01-project-board` — **hard (FK targets + UI host).** Creates the `projects`/`tasks` tables `cost_event` FKs into, and provides the board shell that hosts the in-product Cost tab and the Project → Insights → Cost page.
- `v1/F14-docker-compose-selfhost` — **hard (coordination).** Owns `deploy/docker-compose.yml`, the hardening invariants, the `observability` Compose **profile stub** and the `internal: true` `observability` network; F38 fills in the real `otel-collector`/`prometheus`/`grafana`/`loki`/`tempo` services + provisioned config + Caddy `/grafana` route, conforming to F14's contract test.
- `v1/F06-single-execution-agent` — **soft (integration peer).** Provides the `ModelClient`/embedding/rerank seams the `UsageMeter` wraps; F06 writes the returned `CostRecord.cost_usd` into the `agent_steps.token_usage.cost_usd` field it owns (single source with the ledger) via `RuntimeDeps.step_sink`; emits `record_agent_run`/`observe_requirement_satisfaction`. Absent → those series stay empty (no-op facade tolerates it).
- `v1/F10-run-trace-viewer` — **soft (downstream reader).** F10's per-run cost rollup reads `agent_steps.token_usage.cost_usd` — the same value F38's `UsageMeter` computed and F06 stamped — so the run view and the ledger agree. F38 does not write the step; `cost_event.step_id` FKs `agent_steps.id` (owned by F06). Neither blocks the other; if the step is absent, `step_id` is null.
- `v1/F05-hybrid-knowledge-retrieval` — **soft.** Calls `record_retrieval` (hit rate, stage latencies, reranker delta) and routes embedding/rerank cost through `UsageMeter`.
- `v1/F07-feature-workflow-fsm` / `v1/F08-plan-execute-verify-pr-approval` — **soft.** Emit `record_workflow_terminal`/`record_task_completion`/`record_approval_decision`/`record_pr_outcome`/`record_time_to_merge` and supply `phase`.
- `v1/F09-mcp-gateway-v1` (and `v2/F20`) — **soft.** Emits `record_mcp_call`; `obs.refresh_freshness_gauges` reads its last-sync timestamps.
- `v1/F02-spec-engine` / `v1/F12-eval-harness` — **soft.** May feed `observe_spec_completeness` and (optionally) push eval metrics through the same facade; F12 stays the offline gate, F38 the online sink.
- Cross-cutting conventions: per-workspace tenant isolation (foundation); the immutable audit log that price/reprice mutations write to is `cross-cutting/F39-audit-log` (listed above).

F38's core (`forge_obs` + cost ledger + cost API + no-op facade) is **buildable and fully testable independently** against fakes/seeded rows; live dashboards need F14's profile and the producing slices, but the facade contract lets all of them code against F38 before the stack is wired.

---

## 6. Acceptance criteria (numbered, testable)

1. `setup_telemetry("forge-api", ObsSettings(enabled=True, otlp_endpoint=...))` installs real OTel providers, auto-instruments FastAPI/SQLAlchemy/httpx/Celery/Redis, and is idempotent (a second call does not duplicate instruments); with `enabled=False` (or no endpoint) it installs no-op providers and `get_metrics()` returns a `NoopMetrics`, and **no** OTLP export is attempted.
2. Migration `0NNN_cost_ledger` creates `cost_event` and `model_price` with the documented columns, the generated `total_tokens` column, the `UNIQUE (workspace_id, request_id)` constraint, and all indexes; `alembic downgrade` drops both cleanly (asserted via `pg_indexes`/table existence).
3. `compute_cost` is exact: `prompt_tokens=2000, completion_tokens=500`, price `prompt_usd_per_1k=0.003, completion_usd_per_1k=0.015` → `cost_usd == Decimal("0.0135")`; `price=None` → `Decimal(0)` with `priced=False`.
4. `PriceBook.resolve` returns the newest `effective_from <= occurred_at`, preferring a workspace override over the global (NULL) row; with two prices effective at different dates it returns the one in force at `occurred_at`.
5. `UsageMeter.record` upserts exactly one `cost_event` row, increments `forge_model_tokens_total` (prompt+completion) and `forge_model_cost_usd_total` by the computed cost, and returns a `CostRecord` with the row id + cost; **idempotency**: calling `record` twice with the same `(workspace_id, request_id)` yields one row and does not double-increment the ledger total.
6. An unpriced `(provider, model)` records a `cost_event` with `cost_usd=0`, `priced=False`, and increments `forge_unpriced_model_total{provider,model}` (gap is visible, call is not dropped).
7. Guarded emission: when the metric exporter raises, `UsageMeter.record` still persists the ledger row and does not propagate the exception; when the **ledger** write raises (non-strict mode), it increments `forge_cost_emit_failures_total` and does not crash the calling run.
8. The cost ledger and the per-step trace agree: a recorded model call's `cost_event.cost_usd` equals the `cost_usd` returned in the `CostRecord` and stamped (by F06's step sink) onto the linked `agent_steps.token_usage.cost_usd` (single source) — verified end-to-end with a seeded `agent_steps` row.
9. `GET /tasks/{task_id}/cost` returns a `CostSummary` whose `total_cost_usd` equals the sum of the task's `cost_event.cost_usd`, with a `by-phase` `buckets` breakdown consistent with per-row `phase`.
10. `GET /cost/summary?...&group_by=provider` and `&group_by=model` return breakdowns whose bucket sums equal the scoped total; `GET /cost/timeseries?bucket=day&group_by=provider` returns one series per provider with per-day cost summing to the total.
11. Cardinality contract: the instrument catalog (§4) contains **no** `task_id`/`workflow_run_id`/`user_id` label and no raw `workspace_id` label unless `OBS_METRIC_WORKSPACE_LABEL=true`; a label value outside the configured allow-list is recorded as `"other"`. Asserted by a metrics contract test over the registry.
12. Secret redaction: a log record and a span attribute containing a value matching the secret-pattern set are emitted to their sinks with the value replaced by the redaction token; verified for nested structures and for a BYOK API key passed through a model client (it appears nowhere in logs, span attributes, metric labels, or the cost ledger).
13. Trace-context propagation: a request that starts in `api`, enqueues a Celery task, and triggers an `mcp-gateway` call produces spans sharing one `trace_id` across the three services (verified with an in-memory span exporter and the Celery `task_always_eager` test harness).
14. RBAC + isolation: `viewer` can read all cost GET endpoints; `POST /cost/prices` and `POST /cost/reprice` require `admin`; an authenticated user requesting a `scope_id` in another workspace gets **404**; `/metrics` is not reachable through the public proxy.
15. `forge-cli cost reprice --from <date>` (and `cost.reprice` task) updates only `cost_event` rows on/after `<date>` whose `(provider, model, kind)` price changed, recomputes `cost_usd` idempotently (re-running changes nothing further), and emits a `cost.repriced` `AuditEvent` through F39's `AuditSink`.
16. Every `deploy/observability/grafana/dashboards/*.json` parses, has a unique `uid`+`title`, and references only provisioned datasources; every `deploy/observability/prometheus/rules/*.yml` parses and references only metric names present in the §4 catalog (dashboard/rule contract test).
17. Compose profile: `docker compose --profile observability config` validates and includes `otel-collector`, `prometheus`, `grafana`, `loki`, `tempo`, each with a pinned `@sha256` image, a healthcheck, CPU/mem limits, non-root user, an `autoheal=true` label, and attachment to the `internal: true` `observability` network with no host-published port except Grafana via Caddy (re-uses F14's contract assertions).
18. Degraded mode (no profile): with `OBS_ENABLED=false`, all apps boot, the in-product Cost page + API still work (ledger is Postgres), structured JSON logs still print to stdout, and no service attempts an OTLP connection (no error logs about a missing collector).

### 6.x Definition of Done
All ACs covered by passing tests; an integration test wires a fake model client → `UsageMeter` → real Postgres (testcontainer) + an in-memory OTel reader, records a multi-call run, and asserts the cost ledger, the Prometheus counters, the stamped trace step, and the cost API all agree; the dashboard/rule and compose contract tests pass; new-code coverage ≥ 80% (the `backend-tdd` bar Forge applies to itself); frontend cost components pass under React Testing Library.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first. Layout: `packages/observability/tests/` (unit), `apps/api/tests/cost/` (API + CLI), `apps/worker/tests/` (propagation + reprice), `apps/web/__tests__/` (components), `deploy/tests/` (contract).

**Key fixtures:**
- `pg` — Postgres testcontainer + `alembic upgrade head` (includes F10/F12 + the F38 cost migration).
- `metric_reader` — OTel `InMemoryMetricReader` wired into a test `MeterProvider` so counter/histogram values are asserted without a collector.
- `span_exporter` — OTel `InMemorySpanExporter` for propagation/attribute tests.
- `fake_price_book` — in-memory `PriceBook` with a global + a workspace-override price at two `effective_from` dates.
- `seed_run` — inserts `workspaces`/`projects`/`tasks`/`workflow_run`/`agent_runs`(+`agent_steps`) so cost rows can link to a real step via the `agent_steps.id` FK.
- `secretful_payload` — model prompt/result containing API-key-shaped strings for redaction tests.
- `meter` — real `UsageMeter` over `pg` + `fake_price_book` + the in-memory metrics.

**Unit — pricing/cost (`tests/cost/test_pricing.py`):**
- `test_compute_cost_exact` — hand-computed value (AC3).
- `test_compute_cost_none_price_is_zero_unpriced` — `priced=False`, `Decimal(0)` (AC3/AC6).
- `test_price_resolution_prefers_override_and_effective_date` (AC4).

**Unit — meter (`tests/cost/test_meter.py`):**
- `test_record_inserts_event_and_increments_counters` (AC5).
- `test_record_idempotent_on_request_id` — second call → one row, no double count (AC5).
- `test_unpriced_model_records_zero_and_warns` — `forge_unpriced_model_total` ++ (AC6).
- `test_metric_export_failure_is_swallowed` and `test_ledger_failure_increments_failure_counter` (AC7).

**Unit — metrics facade & cardinality (`tests/test_metrics.py`):**
- `test_facade_methods_emit_expected_instruments` — each `record_*`/`observe_*`/`set_*` updates the right named instrument (AC11 partial).
- `test_no_high_cardinality_labels` — registry has no `task_id`/`workflow_run_id`/`user_id`; `workspace_id` only when enabled (AC11).
- `test_unknown_label_bucketed_to_other` (AC11).
- `test_noop_metrics_when_disabled` — `setup_telemetry(enabled=False)` → `NoopMetrics`, no export attempted (AC1/AC18).

**Unit — telemetry/logging (`tests/test_telemetry.py`, `tests/test_logging.py`):**
- `test_setup_idempotent` (AC1).
- `test_logs_carry_trace_context` — a log inside a span includes matching `trace_id`/`span_id`.
- `test_log_and_span_secret_redaction` — nested secret redacted in log JSON and span attribute (AC12).

**Unit — repository/rollups (`tests/cost/test_repository.py`):**
- `test_summary_group_by_phase_provider_model_sums_match_total` (AC9/AC10).
- `test_timeseries_buckets_sum_to_total` (AC10).
- `test_reprice_only_affected_rows_idempotent` (AC15).

**Integration (`apps/worker/tests/`):**
- `test_end_to_end_cost_ledger_metrics_and_trace_step_agree` — fake model client → `UsageMeter` → `pg` + `metric_reader`; assert ledger sum == counter total == stamped `agent_steps.token_usage.cost_usd` (AC8).
- `test_trace_context_propagates_api_to_worker_to_mcp` — `task_always_eager` + `span_exporter`; one `trace_id` across services (AC13).
- `test_reprice_task_audited` (AC15).

**API tests (`apps/api/tests/cost/`, httpx AsyncClient):**
- `test_task_cost_summary` (AC9); `test_summary_and_timeseries_group_by` (AC10).
- `test_viewer_reads_admin_mutates` (AC14); `test_cross_workspace_is_404` (AC14); `test_metrics_endpoint_internal_only` (AC14).
- `test_price_set_and_reprice_enqueue` (AC15).

**Contract tests (`deploy/tests/`):**
- `test_grafana_dashboards_valid` — every dashboard JSON parses, unique `uid`+`title`, only provisioned datasources (AC16).
- `test_prometheus_rules_reference_known_metrics` — rule files parse; metrics ⊆ §4 catalog (AC16).
- `test_observability_profile_hardened` — `docker compose --profile observability config` includes the five services with pinned digest, healthcheck, limits, non-root, `internal` network, no rogue host ports (AC17).

**Frontend (`apps/web/__tests__/cost.test.tsx`, RTL):**
- `test_task_cost_breakdown_renders` — by-phase + by-provider bars from a mocked `CostSummary`.
- `test_cost_timeseries_stacked_by_provider`.
- `test_price_book_admin_only_edit` — edit control hidden for non-admins.

---

## 8. Security & policy considerations

- **Secret redaction is mandatory and defense-in-depth (spec Security → "Secrets stripped from logs, traces, and retrieval results").** F37's canonical `SecretRedactor` (`forge_auth.redaction`) runs as the **last** logging processor and on span attributes before export, so Loki, Tempo, and metric labels never receive a secret. BYOK provider keys are used by the model client but never appear in `ModelUsage`, the cost ledger, span attributes, or logs (AC12). No raw payloads are placed in metric labels.
- **Cardinality = availability.** Unbounded labels (task/workflow/user/workspace ids) would explode Prometheus and become a DoS vector; the cardinality contract (AC11) keeps high-cardinality dimensions in Postgres (ledger) and Tempo (span attributes) only. Allow-listed label values bucket unknowns to `"other"`.
- **Tenant isolation.** `cost_event`/`model_price` carry `workspace_id`; every cost query filters by it and cross-workspace `scope_id` returns **404** (no existence leak). The product Cost views are tenant-scoped; Grafana is an **ops-facing**, infrastructure-level surface for the self-hoster (not per-tenant) and sits behind Caddy auth on the `internal` network.
- **RBAC + audit.** Cost reads require `viewer`+; price-book mutation and reprice require `admin` and emit immutable `cost.price_set`/`cost.repriced` `AuditEvent`s through `cross-cutting/F39-audit-log`'s `AuditSink` (actor, provider/model, since) per the spec's audit requirement. The ledger is append-only except the audited reprice path.
- **No anonymous access / internal `/metrics`.** All `/api/v1/cost/*` routes are authenticated; the Prometheus `/metrics` endpoint is bound to the internal network and never routed through the public proxy.
- **Billing integrity.** Idempotent upsert on `(workspace_id, request_id)` prevents double-billing on retries; a failed ledger write increments `forge_cost_emit_failures_total` so cost gaps are alertable rather than silent. `forge_unpriced_model_total` makes pricing gaps visible (no silent $0 cost hiding real spend).
- **Overhead / resilience.** Emission is guarded (a telemetry failure never aborts a run, AC7); tracing is sampled (`OTEL_TRACES_SAMPLER_ARG`, default 10%); degraded mode (no collector) is a no-op with stdout logs (AC18). Observability degrades, execution never does.
- **MCP audit linkage.** `forge_mcp_calls_total{connection,status}` + `forge_mcp_call_latency_seconds` complement F10's per-call `mcp_call` step, supporting the MCP rule "full audit log: tool name, payload hash, result status, latency."

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** Cross-cutting: a new shared package plus wiring into three apps, a relational cost ledger + pricing + reprice, a cost API + in-product UI, and the full Grafana/Prometheus/Loki/Tempo/OTel-collector profile with provisioned dashboards. Rough split: `forge_obs` telemetry+logging+facade (M), cost ledger+pricing+meter+reprice (M), cost API+CLI (S), in-product cost UI (S/M), Compose profile + dashboards/rules + contract tests (M), instrumentation wiring + propagation across Celery (M).

**Key risks:**
1. **Metric cardinality explosion.** A stray high-cardinality label kills Prometheus. *Mitigation:* the frozen §4 catalog, the cardinality guard + allow-list bucketing, and an enforcing contract test (AC11); ids live in the ledger/traces, not labels.
2. **Cost double-counting / drift between ledger, trace, and counters.** *Mitigation:* one emission point (`UsageMeter`), idempotent `(workspace_id, request_id)` upsert, and an end-to-end test asserting all three agree (AC5/AC8).
3. **Secret leakage into a permanent sink (logs/traces).** *Mitigation:* reuse F37's single canonical `SecretRedactor` (not a copy), run it last and on span attributes, never put payloads in labels, test nested + BYOK-key cases (AC12).
4. **Trace-context loss across the Celery boundary.** A broken propagation makes "task lineage" useless. *Mitigation:* OTel `CeleryInstrumentor` + explicit header carrier + an eager-mode propagation test (AC13).
5. **Optional-stack drift / overhead on the lean default.** Observability must be opt-in and free when off. *Mitigation:* `OBS_ENABLED=false` default with a true no-op path (no exporters constructed), degraded-mode test (AC18), and sampling.
6. **Trace-backend choice beyond the spec's listed services.** The spec lists Prometheus/Grafana/Loki + LangSmith but not a trace store. *Mitigation:* default to **Tempo** (Grafana-native, self-hosted, pairs with Loki/Prometheus) and keep LangSmith as an optional additional sink via env — documented as an ADR so the deviation is explicit and reversible.
7. **Pricing inaccuracy / staleness (BYOK).** Wrong prices misreport spend. *Mitigation:* effective-dated `model_price` with workspace overrides, `forge_unpriced_model_total` visibility, and an audited `reprice` to correct history (AC6/AC15).

---

## 10. Key files / paths (exact)

**packages/observability/forge_obs/**
- `settings.py` — `ObsSettings`
- `telemetry.py` — `setup_telemetry()`, `Telemetry`, shutdown, resource attrs
- `instrumentation.py` — FastAPI/SQLAlchemy/httpx/Celery/Redis instrument helpers + propagation
- `metrics.py` — `ForgeMetrics` (+ `NoopMetrics`), instrument registry, `get_metrics()`
- `logging.py` — `configure_logging()`, `get_logger()`, `bind_context()`, JSON+redaction+trace processors
- `redaction.py` — shared-redaction-filter adapter
- `tracing.py` — `traced()` + standard span attributes + cardinality guard
- `cost/models.py` — `ModelUsage`, `ModelPrice`, `CostRecord`, `CostBucket`, `CostSummary`, `CostTimeseries`
- `cost/pricing.py` — `PriceBook`, `DbPriceBook`, `compute_cost()`
- `cost/meter.py` — `UsageMeter`, `NoopUsageMeter`
- `cost/repository.py` — `CostLedgerRepository`, `CostReadRepository`
- `tests/` — unit tests + fixtures (`metric_reader`, `span_exporter`, `fake_price_book`, `secretful_payload`)

**packages/db/**
- `forge_db/models/cost.py` — SQLAlchemy 2.x ORM for `cost_event`, `model_price`
- `migrations/versions/0NNN_cost_ledger.py` — creates both tables + indexes + default-price seed (down_revision = current head of the shared `packages/db/migrations` history)

**apps/api/forge_api/**
- `observability/init.py` — telemetry+logging startup, `/metrics` registration
- `api/v1/cost.py` — cost router (summary, timeseries, task cost, prices, reprice, metrics)
- `services/cost_service.py` — `CostService` (isolation + RBAC)
- `schemas/cost.py` — request/response schemas
- `cli/cost.py` — `forge-cli cost reprice|price set|summary`
- `tests/cost/test_cost_api.py`, `tests/cost/test_cost_cli.py`

**apps/worker/forge_worker/**
- `observability/init.py` — worker telemetry + Celery signal instrumentation + `UsageMeter` injection
- `tasks/observability.py` — `cost.reprice`, `obs.refresh_freshness_gauges`
- `tests/test_cost_end_to_end.py`, `tests/test_trace_propagation.py`

**apps/mcp-gateway/forge_mcp_gateway/**
- `observability/init.py` — gateway telemetry + `forge_mcp_*` emission + inbound context

**apps/web/**
- `app/(app)/projects/[projectId]/tasks/[taskId]/cost/page.tsx`
- `app/(app)/projects/[projectId]/insights/cost/page.tsx`
- `app/(app)/settings/observability/page.tsx`
- `components/cost/{CostBreakdown,CostTimeseriesChart,CostStat}.tsx`
- `lib/api/cost.ts`
- `__tests__/cost.test.tsx`

**deploy/**
- `docker-compose.yml` — fill the `observability` profile (`otel-collector`,`prometheus`,`grafana`,`loki`,`tempo`)
- `observability/otel-collector/config.yaml`
- `observability/prometheus/prometheus.yml`, `observability/prometheus/rules/forge.rules.yml`
- `observability/loki/loki-config.yaml`
- `observability/tempo/tempo.yaml`
- `observability/grafana/provisioning/datasources/*.yaml`, `provisioning/dashboards/forge.yaml`
- `observability/grafana/dashboards/{workflow-quality,agent-quality,retrieval-quality,cost}.json`
- `caddy/Caddyfile` + `nginx/forge.conf` — `/grafana` reverse proxy behind auth
- `.env.example`, `.env.production.example` — `OBS_*`, `OTEL_*`, `LOKI_*`, `TEMPO_*`, `GRAFANA_*`, `COST_*`, `LANGSMITH_*`
- `tests/test_observability_profile.py`, `tests/test_grafana_dashboards.py`, `tests/test_prometheus_rules.py`

**docs/**
- `docs/architecture/adr-NNNN-trace-backend-tempo.md` — Tempo-vs-LangSmith decision
- `docs/self-hosting/observability.md` — enabling the profile, dashboards, log/trace correlation

---

## 11. Research references (relevant links from the spec/research report)

- Observability Layer responsibility — "Run traces, token/cost logs, task lineage, retrieval debug, eval harness": `docs/FORGE_SPEC.md` §Product Scope.
- Technology Stack — "Observability: OpenTelemetry + Prometheus + Grafana + Loki + LangSmith — Standard self-hostable stack": `docs/FORGE_SPEC.md` §Technology Stack.
- Observability and Evaluation → **Key Metrics** — the exact workflow/agent/retrieval/cost metric families this slice emits, incl. "Cost: token cost per task, per workflow phase, per model provider": `docs/FORGE_SPEC.md` §Observability and Evaluation.
- docker-compose service list — `prometheus`/`grafana`/`loki` listed as Optional (V2); F38 makes them real behind the `observability` profile: `docs/FORGE_SPEC.md` §docker-compose.yml Service List + §Production Docker Compose Requirements (digest pinning, non-root, healthchecks, limits, network segmentation, capped logs).
- Security — Secret redaction ("Secrets stripped from logs, traces, and retrieval results") and immutable, queryable audit log; Rate limiting per workspace: `docs/FORGE_SPEC.md` §Security.
- MCP Security Rules — "Full audit log: tool name, payload hash, result status, latency" (drives `forge_mcp_*` metrics): `docs/FORGE_SPEC.md` §MCP Security Rules.
- Research report — Technology Recommendations row: "Observability — OpenTelemetry + Prometheus + Grafana — Standard self-hostable observability stack; compatible with LangSmith for agent-specific tracing": `docs/forge-research-report.md` §Technology Recommendations.
- OpenTelemetry Python (instrumentation, OTLP, auto-instrumentors used in `forge_obs`): https://opentelemetry.io/docs/languages/python/
- LangSmith (optional agent-trace sink alongside Tempo): https://smith.langchain.com/
- Langfuse self-hosting reference (Compose patterns for a self-hosted observability stack): https://langfuse.com/self-hosting/deployment/docker-compose
- Docker Compose production 2026 (the hardening rules the profile obeys): https://distr.sh/blog/running-docker-in-production/
- Sibling slices: `docs/implementation-slices/v1/F06-single-execution-agent.md` (owns `agent_steps` + `RuntimeDeps.step_sink`; stamps F38's `CostRecord.cost_usd` onto `agent_steps.token_usage.cost_usd`), `docs/implementation-slices/v1/F10-run-trace-viewer.md` (per-run trace that reads that per-step cost), `docs/implementation-slices/v1/F12-eval-harness.md` (offline metric families F38 mirrors online), `docs/implementation-slices/v1/F14-docker-compose-selfhost.md` (base compose + `observability` profile/network this slice fills in), `docs/implementation-slices/cross-cutting/F37-auth-secrets-byok.md` (`SecretRedactor`, RBAC, BYOK vault), `docs/implementation-slices/cross-cutting/F39-audit-log.md` (`AuditEvent`/`AuditSink`).

---

## 12. Out of scope / future

- **Producing the metric values** — F06/F07/F08 (workflow/agent), F05 (retrieval), F02/F12 (spec/eval), F09/F20 (MCP) decide *when* events happen; F38 ships the facade + pipeline they call.
- **The per-run, step-level trace viewer** — F10 owns the rendering and F06 owns the `agent_steps` writes; F38 only returns the `CostRecord` whose `cost_usd` F06 stamps onto the step, and emits aggregate series.
- **The offline golden-suite regression gate** — F12 owns it; F38 is the online/live counterpart and may optionally receive F12 metrics through the facade.
- **Alerting / on-call routing (Alertmanager, PagerDuty, Sentry)** — V2 (the spec lists Datadog/Sentry/PagerDuty/Grafana integrations under V2); F38 ships dashboards + Prometheus recording rules, not alert routing.
- **Kubernetes/Helm observability templating** — V2 (F24); F38 provides the provisioned configs the chart will template.
- **Temporal-native metrics + durable-workflow tracing** — V2 (F25), when the durable engine lands.
- **FX / multi-currency cost and budget enforcement (hard caps, alerts on overspend)** — V1 stores USD and reports; budget guardrails are future FinOps work.
- **Per-workspace / per-tenant Grafana** — V1 Grafana is ops-facing for the self-hoster; per-tenant dashboards are future multi-team work (F30).
- **RUM / frontend performance telemetry and distributed profiling** — future; V1 covers backend services + agent runtime.
