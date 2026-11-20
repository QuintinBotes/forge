# Reliability primitives (graceful shutdown, rate limiting, idempotency)

HARD-11 adds three operator-facing reliability primitives to the API and worker.
All three are **default-on** with safe defaults, degrade cleanly when their
backing store is unavailable, and are configured through `FORGE_`-prefixed env
vars. None require external credentials.

## Graceful shutdown / zero-downtime restart

On `SIGTERM` (a rolling restart, `docker compose up -d --force-recreate api`),
the API runs a FastAPI `lifespan` shutdown that:

1. flips readiness — `GET /health/ready` returns **503** immediately, so a load
   balancer / the compose healthcheck stops routing new traffic to the instance;
2. drains in-flight requests for up to `FORGE_SHUTDOWN_DRAIN_SECONDS` (default
   `30`);
3. disposes the SQLAlchemy engine, closes the Redis pool, and flushes the OTel /
   audit exporters so no telemetry or audit row is lost.

`GET /health` (liveness) stays **200** the whole time the process is up — only
readiness flips. The compose file sets `stop_grace_period: 45s` on `api`
(> drain + slack) and points the healthcheck at `/health/ready`.

The **worker** shuts down warm: Celery traps `SIGTERM`, stops prefetching, and
finishes in-flight tasks before exiting (`stop_grace_period: 120s`,
`stop_signal: SIGTERM`). Paired with `task_acks_late` (below), a task interrupted
by SIGKILL after the grace period is **re-queued**, not lost.

| Endpoint | Meaning | While serving | While draining |
|---|---|---|---|
| `GET /health` | liveness | 200 | 200 |
| `GET /health/ready` | readiness | 200 | **503** |

Set `FORGE_READINESS_REQUIRE_DEPS=true` (compose default in production) to make
`/health/ready` also fail when the DB ping fails; in dev/test it stays ready
without live backends.

## Rate limiting

Every non-health route is rate-limited per caller (token bucket keyed by the API
credential, falling back to client IP). Over budget → **429** with `Retry-After`
and the `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`
headers (also emitted on allowed responses so a client can self-throttle).
`/health`, `/health/ready`, `/healthz`, `/readyz`, and `/` are never limited.

| Env var | Default | Purpose |
|---|---|---|
| `FORGE_RATE_LIMIT_ENABLED` | `true` | master switch (false = clean no-op) |
| `FORGE_RATELIMIT_RPM` | `120` | steady per-caller refill (requests/min) |
| `FORGE_RATELIMIT_BURST` | `60` | burst capacity |
| `FORGE_RATELIMIT_OVERRIDES` | `{}` | JSON `{route: "N/window"}` per-route budgets |

Tighten the expensive hot paths with overrides, e.g.:

```bash
FORGE_RATELIMIT_OVERRIDES='{"/knowledge/search":"30/minute","/knowledge/retrieve":"30/minute"}'
```

> The limiter is **per-process**: in a multi-replica deployment the effective
> limit scales with replica count. A shared Redis-backed limiter is future work
> (see [security.md](security.md)).

## Request idempotency

A client (or the web app on a flaky connection) can safely retry an unsafe
request by sending an `Idempotency-Key` header. The first request runs the
handler and its response is cached under the key (tenant-scoped by credential);
a retry with the **same key + same body** returns the cached response with
`Idempotency-Replayed: true` and does **not** re-run the side effect. A retry
with the **same key + different body** is a client bug → **422**. Server errors
(5xx) are never cached, so a genuine failure can be re-driven.

| Env var | Default | Purpose |
|---|---|---|
| `FORGE_IDEMPOTENCY_ENABLED` | `true` | master switch |
| `FORGE_IDEMPOTENCY_TTL_SECONDS` | `86400` | replay window (24h) |

Idempotency applies to `POST`/`PUT`/`PATCH`/`DELETE` that carry the header; every
other request is untouched. The default store is in-process; a Redis-backed store
is used in production so replays survive across workers.

### Worker task idempotency

The core tasks (`run_agent_task`, `index_source_task`, `sync_source_task`) accept
an optional `idempotency_key` and use a Redis `SETNX` dedup guard so a
re-delivered Celery message returns a `{"deduplicated": true}` marker instead of
re-running. This is what makes `task_acks_late` safe: an interrupted task can be
re-queued and re-run without a double effect.

| Env var | Default | Purpose |
|---|---|---|
| `FORGE_WORKER_PREFETCH_MULTIPLIER` | `1` | bounded in-flight work (fair dispatch) |
| `FORGE_WORKER_MAX_TASKS_PER_CHILD` | `200` | bounds worker memory growth |
| `FORGE_TASK_SOFT_TIME_LIMIT` / `FORGE_TASK_TIME_LIMIT` | `300` / `360` | runaway-task limits (s) |
| `FORGE_TASK_MAX_RETRIES` | `3` | transient-error retry budget (exp. backoff + jitter) |

## Run executor

`FORGE_RUN_EXECUTOR` chooses where an agent run's loop runs: `inline` (inside the
worker's Celery task, under the soft time limit above), `docker` (one container
per run) or `kubernetes` (one Job per run). Unset means `inline` for now. The
`docker` and `kubernetes` executors are not built yet: until they are, every run
fails with an error that names the executor. The worker never runs the loop
itself in place of a failed executor.

The worker refuses to start when the value is unknown, when a non-inline executor
is combined with the `worktree` sandbox tier, when the executor and
`FORGE_SANDBOX_BACKEND` name different platforms (an unset backend means
`docker`), when a non-inline executor has no `FORGE_RUN_API_URL`, or when
`FORGE_RUN_TIME_LIMIT` or `FORGE_RUN_RECONCILE_GRACE_SECONDS` is not a usable
number of seconds.

With a non-inline executor, `forge.agent.run` prepares the run in the worker,
launches one unit for it, records the executor and unit under the run's
`output.executor`, and returns without waiting. A redelivered message launches
nothing. The unit is given only the environment a run process may hold, never
the database credential. Close-out (`agent.close_out_run`) reads the handoff the
unit recorded, delivers the change, records the terminal events and the outcome,
writes back to the tracker and removes the unit. Close-out is claimed under the
run's row lock, so it happens once however often it is triggered. For now the
reconciler below triggers it, on its first pass after the unit exits.

A close-out is done only when the run's outcome is recorded. If its worker is
killed part way, or the outcome cannot be written, the claim expires after the
hard task time limit (`FORGE_TASK_TIME_LIMIT`) plus a minute and the reconciler
closes the run out again. A retry delivers to the pull request the first attempt
may already have opened. Terminal events the first attempt stored can appear
twice. A handoff or stored objective that cannot be read fails the run with
`run_record_unreadable`.

A reconciler (`agent.reconcile_launched_runs`, on beat) checks every launched run
that is not closed out. A handoff recorded by the time it takes the run's row
lock always wins over its own verdict:

- a unit that exited with a handoff is closed out from it;
- a unit that exited without a handoff, or is missing, leaves the run failed with
  the reason `executor_lost`;
- a unit still running past `FORGE_RUN_TIME_LIMIT` plus the grace period is
  stopped, and the run is recorded escalated (`wall_clock_limit_exceeded`);
- when the executor cannot be reached, nothing is decided until that same
  deadline. After it, the run is recorded escalated
  (`executor_unreachable: the unit may still exist`).

A unit reaches its run record through SCF's API, never the database. At launch
the worker issues a token that belongs to that run alone and puts it in the
unit's environment as `FORGE_RUN_TOKEN`, next to `FORGE_RUN_API_URL`, which the
worker passes on from its own environment. The run process
(`python -m scf_worker.run_main`) reads its objective, appends its events and
records its handoff with that token, and refuses to start when only one of the
two is set. A request that fails to connect, gets a server error or is
rate-limited (`429`, after waiting its `Retry-After`, at most 10 seconds) is
tried up to three times; an event keeps its idempotency key across the attempts,
so it is stored once. If the answer to a handoff is lost and the retry finds the
handoff already stored (`409`), it counts as recorded. Any other refusal is not
retried. When the handoff cannot be recorded, the process exits non-zero, naming
the HTTP status or the connection error. A handoff carries every step of the run,
so its request body may be up to `FORGE_RUN_HANDOFF_MAX_BODY_BYTES` (16 MiB);
every other route keeps the 1 MiB request limit. The
token lives for the wall-clock limit plus the grace period, or
`FORGE_RUN_TOKEN_TTL_SECONDS` if that is shorter. Close-out revokes it, whether
the run ends from its handoff or the reconciler ends it, and so does a failed
launch. The token is never logged, audited or stored on the run; only its hash
is kept until it is revoked.

Launch and close-out each write one audit event (`agent_run.launched`,
`agent_run.closed_out`) with the workspace, run, executor, unit and outcome, and
no environment value. Issuing the token writes `run_token.issued`. A failed audit
write does not fail the run.

| Env var | Default | Purpose |
|---|---|---|
| `FORGE_RUN_EXECUTOR` | `inline` | where the agent loop runs: `inline`, `docker` or `kubernetes` |
| `FORGE_SANDBOX_BACKEND` | `docker` | the platform the executor must match |
| `FORGE_RUN_TIME_LIMIT` | `3600` | a run's wall-clock limit (s), in the run process and the reconciler |
| `FORGE_RUN_RECONCILE_INTERVAL_SECONDS` | `60` | how often the reconciler runs |
| `FORGE_RUN_RECONCILE_GRACE_SECONDS` | `300` | how long past the wall-clock limit a unit may run before it is stopped |
| `FORGE_RUN_API_URL` | unset | the address of SCF's API that a launched unit reaches its run record at; set it on the worker (required with a non-inline executor) |
| `FORGE_RUN_HANDOFF_MAX_BODY_BYTES` | `16777216` | the largest handoff the API accepts (set on the API) |
| `FORGE_RUN_TOKEN` | set per unit | the run's own token; the worker issues it at launch, never set it yourself |

## Related

- [performance.md](performance.md) — load/perf budgets and the multi-tenant soak.
- [upgrade.md](upgrade.md) — migration reversibility gates.
- [security.md](security.md) — rate limiting as a security control.
