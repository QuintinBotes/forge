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

## Related

- [performance.md](performance.md) — load/perf budgets and the multi-tenant soak.
- [upgrade.md](upgrade.md) — migration reversibility gates.
- [security.md](security.md) — rate limiting as a security control.
