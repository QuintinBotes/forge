# Performance & soak evidence

HARD-11 ships a load/perf harness and a bounded multi-tenant soak so an operator
can quote Forge with real numbers instead of asserted ones. The harnesses live
under [`deploy/load/`](../../deploy/load) and
[`packages/evaluation/forge_eval/perf`](../../packages/evaluation/forge_eval/perf)
/ [`.../soak`](../../packages/evaluation/forge_eval/soak). Budgets are in
[`deploy/load/budgets.toml`](../../deploy/load/budgets.toml).

> **Honest asterisk.** The perf bench, API load test, and soak all require a
> resourced / networked runner (live pgvector, ideally a learned embedder). They
> **skip cleanly** in the hermetic sandbox — the *logic* (percentile math,
> cross-tenant isolation, resource sampling) is CI-verified at a tiny scale, but
> the *absolute numbers below are from a reference runner*, not the sandbox. A
> true multi-week production **fleet** soak is out of scope; this is a bounded,
> single-host, minutes-to-hours simulated soak.

## Retrieval latency (p50/p95/p99)

The micro-bench indexes a corpus of `FORGE_PERF_CORPUS_SIZE` chunks through the
real hybrid pipeline and measures per-stage latency:

```bash
# Resourced runner (writes deploy/load/reports/retrieval_latency.json):
FORGE_RUN_PERF=1 FORGE_PERF_CORPUS_SIZE=10000 make perf
```

Reported stages: `total` (full search) and `embed` (standalone embed call, when
an embedder is supplied). The finer `semantic`/`keyword`/`fusion`/`rerank`
split needs in-pipeline timing hooks in `KnowledgeService` and is a documented
follow-up. Budgets (`[retrieval]` in `budgets.toml`): `total_p95_ms = 400`,
`embed_p95_ms = 50`.

| Stage | p50 | p95 | p99 | Corpus | Embedder | Runner |
|---|---|---|---|---|---|---|
| total | _tbd_ | _tbd_ | _tbd_ | 10k | sentence-transformers | _record runner spec_ |
| embed | _tbd_ | _tbd_ | _tbd_ | 10k | sentence-transformers | _record runner spec_ |

> Fill this table from `deploy/load/reports/retrieval_latency.json` on your
> reference runner. Record the machine class (vCPU/RAM/disk) alongside the
> numbers — perf budgets are environment-sensitive.

## API hot-path load (k6 / Locust)

```bash
# Full run (manual/nightly) against a running API with a workspace key:
k6 run -e BASE_URL=http://localhost:8000 -e API_KEY=forge_xxx \
  deploy/load/k6/api_hotpaths.js

# Or Locust:
FORGE_LOAD_API_KEY=forge_xxx locust -f deploy/load/locustfile.py --headless \
  -u 20 -r 5 -t 3m --host http://localhost:8000

# CI non-blocking smoke (a few VUs, a few seconds; visible, not gating):
make load-smoke
```

Thresholds (`[api]` in `budgets.toml`): `health_p95_ms = 50`, `board_p95_ms =
300`, `knowledge_search_p95_ms = 800`, `agent_enqueue_p95_ms = 500`,
`http_req_failed_rate < 0.01`. The k6 script encodes the same p95/error
thresholds so the run fails when a budget is exceeded.

## Multi-tenant soak

```bash
# Bounded soak (N tenants, mixed read/write), writes a SoakReport:
FORGE_RUN_SOAK=1 FORGE_SOAK_TENANTS=5 FORGE_SOAK_DURATION_SECONDS=900 make soak
```

The soak seeds **identical content under every tenant** and asserts, as hard
pass/fail:

- `cross_tenant_leaks == 0` — no tenant ever sees another's rows (the FORGE_SPEC
  multi-tenant isolation property under sustained mixed load);
- `resource_stable == true` — RSS / open-FD / DB-connection samples show no
  unbounded growth beyond a slack threshold;
- `errors == 0` — no error cliff at the target rate.

> Resource sampling uses `psutil` when installed (live RSS + FD count) and falls
> back to the stdlib `resource` module otherwise. The fallback's `ru_maxrss` is a
> high-water mark (monotonic), so install `psutil` on the soak runner for a
> meaningful stability signal.

| Tenants | Duration | Requests | Errors | Cross-tenant leaks | Resource-stable | Runner |
|---|---|---|---|---|---|---|
| 5 | 900s | _tbd_ | _tbd_ | **0** | _tbd_ | _record runner spec_ |

> Fill from the `SoakReport` printed by `make soak` on your reference runner.

## Related

- [reliability.md](reliability.md) — the primitives the soak/load exercise.
- [observability.md](observability.md) — where latency metrics surface at runtime.
