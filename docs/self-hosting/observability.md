# Observability & Cost Metrics (F38)

Swarm Code Factory's observability substrate is **opt-in and free when off**: the lean
default (`OBS_ENABLED=false`) boots every service with no-op telemetry
providers, structured JSON logs on stdout (captured by Docker's capped
json-file logs), and a fully working cost ledger + in-product Cost API — the
ledger is Postgres, not the metrics stack.

## What ships today

- **`scf_obs`** (`packages/observability`): one `setup_telemetry()` init per
  service (`forge-api`, `forge-worker`, `forge-mcp-gateway`), the typed
  `ScfMetrics` Key-Metric facade with a frozen instrument catalog and bounded
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
- **CLI**: `scf cost compute | price-set | reprice | summary`.

## GenAI spans for agent runs

A task's agent run (single-agent or supervised, from the Celery task, a workflow
run or a run process) is a trace shaped the way the OpenTelemetry GenAI semantic
conventions describe, so an operator's own backend can attribute time and money to
a run. The reviewer run (`scf_worker/review.py`) and the held-out verifier run
(`scf_worker/held_out_drafting.py`) are not traced yet. The conventions are pinned to commit
`622b2b6073b15e5ed886c3c12a65a892b9cd5c70` of
`open-telemetry/semantic-conventions-genai`, read on 2026-11-20 (the repository had
no tag; ADR-0016). The pin and every `gen_ai.*` name SCF emits live in
`packages/observability/scf_obs/genai.py`.

```text
invoke_agent                          INTERNAL  one per run (single-agent path)
├── chat {gen_ai.request.model}       CLIENT    one per model call
└── execute_tool {gen_ai.tool.name}   INTERNAL  one per executed tool call

invoke_workflow                       INTERNAL  one per supervised run
└── invoke_agent {role}               INTERNAL  one per role run, each with the tree above
```

| Attribute | On | Value |
|---|---|---|
| `gen_ai.operation.name` | every span | `invoke_agent`, `invoke_workflow`, `chat` or `execute_tool` |
| `gen_ai.agent.name` | role spans | the subagent role, such as `implementer` |
| `gen_ai.provider.name` | `chat` | the configured provider (`anthropic`, `openai`, `openai_compatible`, `local_cli`), or `scf.scripted` for canned output |
| `gen_ai.request.model` | `chat` | the model the call requested, else the model the client runs |
| `gen_ai.response.model` | `chat` | the model the provider reported |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | `chat`, and summed on agent and workflow spans | as the provider reported them; absent when it reported none |
| `gen_ai.tool.name` | `execute_tool` | the tool's name; `unknown_tool` for a name the model invented that no registered tool has |
| `error.type` | any failed span | the exception's class name; `tool_error` for a tool that returned a failure; the provider's failure stop reason (such as `budget_exhausted`) for a model call |
| `scf.cost.usd` | `chat`, and summed on agent and workflow spans | the call's cost in US dollars, priced like the run's `model_usage` artifact; absent when the model has no known price, and absent on an agent or workflow span when any call under it was unpriced |
| `scf.run.id`, `scf.workspace.id` | agent and workflow spans | the SCF run and workspace; a role span carries the role run's own id |

A tool call the policy gate denies never reaches the tool registry, so it has no
span. No span carries prompt text, completion text, tool arguments, tool output or
an exception's message, and every name and value passes through the shared secret
redactor. Spans always land in the worker's in-memory span store (the newest
10,000) and also go to the OTLP exporter when one is configured, in which case a
log line written inside a span carries that exported span's `trace_id` and
`span_id`. A telemetry failure is logged and never fails the run.

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
honestly. The `scf_obs` facade is the frozen contract; when the OTel SDK
lands it slots in behind `setup_telemetry()` without changing any caller.
Tempo remains the intended self-hosted trace backend (Grafana-native companion
to Loki/Prometheus), with LangSmith as an optional additional sink via
`LANGSMITH_*` env — the spec's stack list names no trace store, so this is the
recorded decision.
