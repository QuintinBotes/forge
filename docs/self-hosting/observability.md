# Observability & Cost Metrics (F38)

Forge's observability substrate is **opt-in and free when off**: the lean
default (`OBS_ENABLED=false`) boots every service with no-op telemetry
providers, structured JSON logs on stdout (captured by Docker's capped
json-file logs), and a fully working cost ledger + in-product Cost API — the
ledger is Postgres, not the metrics stack.

## What ships today

- **`forge_obs`** (`packages/observability`): one `setup_telemetry()` init per
  service (`forge-api`, `forge-worker`, `forge-mcp-gateway`), the typed
  `ForgeMetrics` Key-Metric facade with a frozen instrument catalog and bounded
  label cardinality, secret-redacted JSON logging with `trace_id`/`span_id`
  correlation, and the single `UsageMeter` cost-emission path.
- **Durable cost ledger**: `cost_event` + `model_price` (Alembic
  `0021_f38_cost_ledger`, seeded with global default prices). Idempotent on
  `(workspace_id, request_id)` — retries never double-bill.
- **Cost API**: `GET /tasks/{id}/cost`, `GET /cost/summary`,
  `GET /cost/timeseries`, `GET/POST /cost/prices`, `POST /cost/reprice`
  (admin, audited as `cost.price_set` / `cost.repriced`), all
  workspace-isolated (cross-workspace ids → 404).
- **Metrics exposition**: `GET /observability/metrics` renders the in-process
  registry in the Prometheus text format (authenticated; empty when disabled).
- **Worker**: `cost.reprice` task + the `obs.refresh_freshness_gauges` beat
  entry (samples `forge_mcp_freshness_lag_seconds` per MCP connection, 60s).
- **CLI**: `forge cost compute | price-set | reprice | summary`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `OBS_ENABLED` | `false` | Master switch; off = no-op providers, no export attempted |
| `OTEL_SERVICE_NAME` | per app | Service resource attribute |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP collector endpoint (reserved; see below) |
| `OTEL_TRACES_SAMPLER_ARG` | `0.1` | Trace sampling ratio (reserved) |
| `OBS_METRIC_WORKSPACE_LABEL` | `false` | Cardinality guard: never label series with workspace ids unless opted in |
| `FORGE_OBS_FRESHNESS_INTERVAL_SECONDS` | `60` | Beat cadence of the MCP freshness gauge |

## Parked (documented deviation)

The full self-hosted pipeline — the `observability` Compose profile
(`otel-collector`, `prometheus`, `grafana`, `loki`, `tempo` with pinned
digests + provisioned dashboards) and real OTLP export via the OpenTelemetry
SDK — is **parked**: this build environment has no third-party network access,
so SDK dependencies cannot be locked and image digests cannot be pinned
honestly. The `forge_obs` facade is the frozen contract; when the OTel SDK
lands it slots in behind `setup_telemetry()` without changing any caller.
Tempo remains the intended self-hosted trace backend (Grafana-native companion
to Loki/Prometheus), with LangSmith as an optional additional sink via
`LANGSMITH_*` env — the spec's stack list names no trace store, so this is the
recorded decision.
