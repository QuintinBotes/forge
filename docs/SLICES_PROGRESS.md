# Slices Progress

Running record of feature-slice swarm runs against the whole-repo green gate
(`uv run ruff check .` clean + full `pytest` against real pgvector on :5433).

---

## Run: F32–F39 (v3 tail, 2026-07-03 → 2026-07-04)

Commit range `49babd7..29d31da` on `main` (one commit per slice, no reverts).
Gate re-verified after F39 on 2026-07-04: `ruff check .` clean; full suite
green against real pgvector on :5433 — **2994 passed, 8 skipped** (all skips
are documented PARKED-environment tiers, e.g. kind/helm e2e), 0 failed,
in 4m19s.

> Provenance note: the orchestrator handoff payload for this run was truncated
> after F37 (mid-way through F37's last parked item). F32–F37 rows below are
> verbatim from the payload; the F38/F39 rows and parked lists are
> reconstructed from repo evidence (commit diffs, module docstrings, and
> `docs/self-hosting/observability.md`), and their refuted/repaired fields are
> marked `—` (not asserted).

### Summary

| id  | slug                       | gate   | refuted | repaired | decision  | commit    |
|-----|----------------------------|--------|---------|----------|-----------|-----------|
| F32 | integration-marketplace    | passed | 0       | no       | committed | `49babd7` |
| F33 | enterprise-sso             | passed | 0       | no       | committed | `802a7f9` |
| F34 | firecracker-sandbox        | passed | 0       | no       | committed | `4f96640` |
| F35 | benchmark-leaderboard      | passed | 0       | no       | committed | `37d3739` |
| F36 | human-approval-system      | passed | 0       | no       | committed | `c554fa8` |
| F37 | auth-secrets-byok          | passed | 1       | no       | committed | `da79021` |
| F38 | observability-cost-metrics | passed | —       | —        | committed | `e98f129` |
| F39 | audit-log                  | passed | —       | —        | committed | `29d31da` |

**Committed:** F32, F33, F34, F35, F36, F37, F38, F39 (all eight).
**Reverted:** none this run.

### PARKED (reason), per slice

#### F32 integration-marketplace
- apps/web Next.js marketplace UI (routes + components + lib/api/marketplace.ts) — parked per precedent to protect the gate; web excluded from the uv workspace and untouched.
- `forge marketplace search|show|list|install|update` CLI subcommands — argparse stubs present that emit a "requires running API — PARKED" message; only the offline `package` author command is implemented (the load-bearing AC20 path).
- GitRegistryClient live git-clone transport — HTTP(S)/file index fetch (incl. raw git URLs) is implemented + SSRF-bounded; native git transport deferred with the worker egress NetworkPolicy.
- deploy compose `marketplace-egress` network + helm NetworkPolicy / values.marketplace.officialRegistry.* — env vars added to .env.example; infra network-policy wiring parked (no infra tests in the gate).
- Wiring official-registry seeding into the real workspace-create path — seed_official_registry/backfill_official_registries helpers + a worker backfill task are implemented and tested (AC2), but the on-workspace-creation hook is not attached (no clean creation hook in-tree).

#### F33 enterprise-sso
- apps/web UI (login HRD email field, settings SSO/SCIM pages) — backend-first precedent; all admin surfaces exist as API routes.
- SAML Single Logout — SP metadata advertises the SLO location but the endpoint returns an honest 501 (the F37 substrate has no browser session store to terminate; sessions are short-lived API-key cookies).
- Redis replay guard — InMemoryReplayGuard (single-process default) + DbReplayGuard over the saml_replay table (documented no-Redis fallback, evicted by the cleanup beat task) are implemented; the Redis-backed guard awaits a slice run against live Redis.
- propagate_deprovision run-cancellation fan-out — the foundation agent_run schema has no per-user ownership column to cancel by; the task re-revokes credentials and writes the sso.deprovision_propagated audit event.
- SCIM per-workspace rate limiting — no rate-limit substrate exists in the foundation yet.
- Live IdP interop certification (Okta/Entra/Google) — fixture-driven offline per spec §12; domain-ownership verification workflow (sso_domain.verified ships admin-asserted with the global-uniqueness guard).

#### F34 firecracker-sandbox
- Real-runtime integration tiers (`@pytest.mark.gvisor` / `@pytest.mark.firecracker` in packages/agent-runtime/tests/sandbox/test_kernel_integration.py) — need runsc / kata-fc registered with a live daemon and /dev/kvm; skipif-gated, run in a virtualization-enabled CI job (macOS dev host has neither).
- AC16 runtime AuditEvent emission + sandbox_instance row writes during the agent loop — F19 never wired sandbox persistence/audit into the loop (in-memory precedent; no `SandboxInstance(` construction or AuditSink usage exists anywhere in agent-runtime/worker). F34 ships the columns, enums, isolation_class_for mapping and SandboxInstanceRead contract; the emission point lands with F19's persistence wiring rather than as a parallel layer.
- apps/api SandboxInstanceRead response surface — apps/api/forge_api/schemas/agent.py does not exist (no agent-run read API yet); the contract extension lives in forge_contracts.SandboxInstanceRead per precedent.
- apps/web SandboxPanel.tsx isolation badge — F10 run-trace viewer component does not exist; apps/web untouched per foundation precedent.
- install-runtimes.sh --firecracker performs registration/merge/verify but defers the distribution-specific Kata binary install (fails with an actionable message pointing at kata-static releases); the gVisor binary install follows the official release channel.
- gVisor image-compat CI gate (python/node/go verification commands under runsc) — belongs to the same Docker/virtualization-gated CI tier as F19's parked image tests.

#### F35 benchmark-leaderboard
- Internal agent-driven benchmark runs (POST /{slug}/{version}/runs + Celery "benchmark" queue + worker task): the in-tree foundation has no F12 EvalRunner/replay-recorder/agent-eval substrate (forge_eval is the retrieval/task scorecard scaffold only), so a live run cannot be executed honestly. The load-bearing path (external submit → deterministic offline verify → admin moderation → public ranking) is fully implemented and synchronous, matching the in-process precedent of earlier slices.
- MinIO signed bundle URLs: no object store exists in-tree. Replay bundles are persisted inline (JSONB, capped by FORGE_BENCHMARK_SUBMISSION_MAX_BYTES → 413) and the public "reproduce" affordance is an API-served payload-free bundle download route (/public/…/submissions/{id}/bundles) consumable directly by `forge-cli bench verify`.
- apps/web UI (public leaderboard pages + internal moderation queue): parked per established slice precedent (backend-first, protect the pnpm gate).
- CLI API-backed subcommands `bench run|submit|leaderboard`: parked exactly like the F32 marketplace CLI (exit 3 with message) — they need a running API. Offline `bench freeze|hash|verify` (the AC21/AC24 load-bearing exit-code paths) are implemented and tested.
- .github/workflows/benchmark-smoke.yml: not added — CI wiring is unverifiable in this sandbox; the freeze/score/verify/rank smoke coverage it would provide runs in-repo via packages/evaluation/tests/benchmark + apps/api/tests/benchmark + apps/api/tests/test_bench_cli.py.
- Audit of denied (403) attempts as "denied" events (AC23 tail): the shared require_permission dependency rejects before the handler/service runs; the five success-path events (submitted/verified|rejected/published/flagged/suite_registered) are emitted exactly once each and tested.

#### F36 human-approval-system
- apps/web Approval UI (AC#20 inbox + review shell + panel registry + a/r/x/e shortcuts) — parked per foundation precedent (apps/web untouched); the gate-agnostic /approvals API + ApprovalContext nine-item envelope it renders are complete.
- SQLAlchemy-backed ApprovalRepository for the API service — the API composition root uses the in-memory repository per foundation precedent (same as the existing /approval router and incident service); the canonical DB tables + repository Protocol boundary are shipped, and the worker sweep/consume paths ARE DB-backed.
- F07 FSM effect wiring (spec/plan resolution hooks emitting spec_approved/plan_approved) and F08 pr provider re-homing — this foundation has no F08 pr ApprovalService/merge-gate provider to re-home; the legacy in-memory /approval router is kept byte-identical (its tests pass unchanged, regression-locked). pr/spec/plan/incident providers register at the composition root (bootstrap pattern shipped); unregistered gates degrade to a read-only fallback context and hook-less resolves return a not_implemented outcome, exactly as the slice's risk #3 prescribes.
- Slack resolution surface (F16) — approval.requested/approval.resolved events are published on the injected ActivityBus; no F16 runtime dependency exists yet.
- MinIO context_ref snapshot writer — the context_ref column + domain field exist; no writer until a producer needs >inline contexts.
- Compose "celery beat" runner service — no beat service exists in deploy compose for ANY existing beat task (F19/F30/F32/F33); F36 follows the established code-registered schedule pattern (forge_worker.beat), pre-existing foundation gap noted.

#### F37 auth-secrets-byok (1 finding refuted during review; not repaired — refutation held)
- Better Auth web runtime + all apps/web UI surfaces (login page, settings account/secrets/api-keys) — apps/web untouched per foundation precedent; the backend JWT seam (HS256 SessionClaims over AUTH_SECRET) is live and tested via mint_jwt-style tests.
- DB-backed swap of the in-memory BYOK vault + inbound API-key stores in apps/api — foundation precedent keeps V1 services in-memory behind the SecretStore/APIKeyBackend protocol seams; the AES-256-GCM envelope vault, key_id platform-key primitives, and platform_api_key table are all shipped and tested, ready for the swap.
- Global no-anonymous-access middleware + /api/v1 route-coverage test (AC12) — conflicts with the foundation (intentionally public routes exist, e.g. public leaderboard and webhook receivers); per-route auth dependencies remain authoritative.
- Redis-backed rate limiting + 429 middleware wiring (AC18) — a real in-process fixed-window RateLimiter implementing the frozen protocol is shipped + tested; Redis backend and global middleware are a drop-in behind the same protocol.
- CLI `users …` administration subcommands — parked in full (no user-admin CLI surface exists in the F37 commit). *Orchestrator payload truncated mid-item here; wording reconstructed from repo evidence.*

#### F38 observability-cost-metrics *(reconstructed from repo evidence)*
- Self-hosted metrics pipeline — the `observability` Compose profile (otel-collector, prometheus, grafana, loki, tempo with pinned digests + provisioned dashboards) and real OTLP export via the OpenTelemetry SDK — parked: this build environment has no third-party network access, so SDK dependencies cannot be locked and image digests cannot be pinned honestly. The `forge_obs` facade (`setup_telemetry()`) is the frozen seam the SDK slots in behind without changing any caller (documented deviation in docs/self-hosting/observability.md).
- Real OTLP span export / log-trace correlation export — no-op providers ship by default (`OBS_ENABLED=false` is free-when-off); `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_TRACES_SAMPLER_ARG` are reserved env vars until the OTel SDK lands.
- apps/web cost dashboard UI — no apps/web files in the commit, per foundation precedent; the in-product surface is the Cost API (`/tasks/{id}/cost`, `/cost/summary`, `/cost/timeseries`, `/cost/prices`, `/cost/reprice`) + `forge cost` CLI.
- Shipped and green: durable Postgres cost ledger (`cost_event` + `model_price`, migration 0021, idempotent on `(workspace_id, request_id)`), typed `ForgeMetrics` facade with frozen instrument catalog + bounded label cardinality, secret-redacted JSON logging, Prometheus text exposition at `GET /observability/metrics`, `cost.reprice` worker task + `obs.refresh_freshness_gauges` beat entry.

#### F39 audit-log *(reconstructed from repo evidence)*
- MinIO `audit.archive` job — no object-store client exists in-tree yet; the streaming `GET /audit/export` endpoint covers the export surface (documented in apps/worker/forge_worker/tasks/audit.py).
- apps/web audit-log viewer UI — no apps/web files in the commit, per foundation precedent; the admin-only `/audit` query/verify/export API is the canonical surface.
- Shipped and green: hash-chained durable audit log in Postgres (migration 0022, `forge_db.audit` chain/writer/repository/redaction), fail-open async `audit.record` sink + daily `audit.verify_chain_all` beat verifier (chain break → `audit.chain_broken` critical event), frozen `forge_contracts.audit` extensions, no HTTP create/update/delete route by design (AC13).

---

*Maintained by the slice-run gatekeeper; append new runs above this line's section boundary as they land.*
