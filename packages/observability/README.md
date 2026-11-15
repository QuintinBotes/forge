# scf-obs

Observability & cost metrics SDK for Swarm Code Factory (slice F38).

- `scf_obs.metrics` — the typed `ScfMetrics` Key-Metric facade (spec §Observability
  and Evaluation) with a frozen instrument catalog, bounded label cardinality
  (allow-list bucketing to `"other"`, no `task_id`/`workflow_run_id`/`user_id`/raw
  `workspace_id` labels), an in-process recording implementation, and a `NoopMetrics`
  degraded mode.
- `scf_obs.telemetry` — idempotent `setup_telemetry()` returning a `Telemetry`
  handle; installs the process metrics singleton (real or no-op).
- `scf_obs.logging` — structured JSON logs carrying `service`/`trace_id`/`span_id`/
  bound context, with F37's canonical `SecretRedactor` applied **last**.
- `scf_obs.tracing` — lightweight `traced()` spans with W3C-shaped ids used for
  log/trace correlation (real OTLP export is parked until the OpenTelemetry SDK
  dependency lands).
- `scf_obs.cost` — the single cost-emission path: `compute_cost`, `PriceBook`
  (in-memory + DB), the idempotent `UsageMeter`, and the `cost_event`/`model_price`
  ledger repositories (in-memory + SQL over `scf_db`).
