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

## Hardening complete — HARD-01 … HARD-14 (production-hardening tier, 2026-07-05)

The 14-slice production-hardening tier (`docs/implementation-slices/hardening/`,
spec `SPEC-PRODUCTION-HARDENING.md`) is closed out. **13 of 14 slices landed on
`main`** (HARD-01 … HARD-13); **HARD-14 was reverted** after review refuted two of
its claims. Gate re-verified at HEAD `9a03c0d` (feat(HARD-06): live-slack) on
2026-07-05: `uv run ruff check .` → *All checks passed*; full suite against real
pgvector on :5433 → **3514 passed, 53 skipped, 0 failed** in 6m32s. Every one of
the 53 skips is a documented live-/cred-/runner-gated tier that skips clean when
its credential or runner is absent (live MCP transport ×14, learned real-corpus
eval ×6, sandbox kernel/container runner ×6, live GitHub creds ×5, compose-build
smoke ×4, live model BYOK ×4, live Slack creds ×3, perf/soak runner ×2, live
reranker creds ×1, promtool/amtool ×2, …). No skip is a masked failure.

### This batch (final four + the reverted spine)

| id      | slug                   | refuted | repaired | decision  |
|---------|------------------------|---------|----------|-----------|
| HARD-02 | live-model-byok        | 0       | no       | committed |
| HARD-03 | live-reranker          | 0       | no       | committed |
| HARD-05 | live-mcp-server        | 0       | no       | committed |
| HARD-06 | live-slack             | 0       | no       | committed |
| HARD-14 | future-scope-execution | 2       | yes      | reverted  |

**HARD-14 reverted (not committed):** review refuted two claims; a repair was
attempted but the final gatekeeper decision was to revert rather than land — no
HARD-14 commit exists in `main`. Its blocker-#5/#7 mandate is otherwise satisfied
at the foundation level (see below): the toolchain already migrated to Python
3.14.6 + re-locked deps (commit `4a75308`, `requires-python >=3.14`), which is
precisely what made HARD-14's forward-compat lane redundant.

### All 14 HARD items — final status

| id      | slug                    | blocker(s) | decision  | commit    |
|---------|-------------------------|------------|-----------|-----------|
| HARD-01 | live-github-app         | #1         | committed | `82ffc1a` |
| HARD-02 | live-model-byok         | #1         | committed | `47bba25` |
| HARD-03 | live-reranker           | #1, #2     | committed | `d93b72f` |
| HARD-04 | real-eval-corpus        | #2         | committed | `a33e068` |
| HARD-05 | live-mcp-server         | #1         | committed | `2d7e726` |
| HARD-06 | live-slack              | #1         | committed | `9a03c0d` |
| HARD-07 | docker-build-and-pin    | #3         | committed | `2312aec` |
| HARD-08 | kubernetes-helm-deploy  | #3, #6     | committed | `5807116` |
| HARD-09 | security-hardening      | #4         | committed | `16ffeb6` |
| HARD-10 | observability-cost-prod | #6         | committed | `8e67f8a` |
| HARD-11 | reliability-maturity    | #6         | committed | `95228d6` |
| HARD-12 | release-engineering     | #4, #6     | committed | `86dce5f` |
| HARD-13 | secrets-config-prod     | #4, #5     | committed | `035eff0` |
| HARD-14 | future-scope-execution  | #5, #7     | reverted  | — (none)  |

**Committed: 13** (HARD-01 … HARD-13). **Reverted: 1** (HARD-14).

### PARKED (live/manual verification) for this batch

- **HARD-02 live-model-byok** — G-MODEL live verification (AC9–AC12) needs a real
  BYOK Anthropic/OpenAI key + outbound network. Tests skip clean today (5 in
  agent-runtime + 2 in worker). Close with: `uv sync --extra providers && set -a;
  source .env.integration; set +a && export FORGE_MODEL_PROVIDER=anthropic
  FORGE_MODEL_MAX_TOKENS=1024 && uv run pytest -m live_model packages/agent-runtime
  apps/worker -q`. Runbook: `docs/runbooks/live-model.md`.
- **HARD-03 live-reranker** — live verification (AC10–AC13) needs `JINA_API_KEY`/
  `COHERE_API_KEY` (or a reachable `JINA_RERANKER_URL`) + network; skips clean.
  Frontend rerank-score surfacing deferred (no numbered AC; would pull in the
  pnpm gate — the API already emits `rerank_score=null` degradation). Self-hosted
  reranker container digest-pin is HARD-08's networked gate. Runbook:
  `docs/runbooks/live-reranker.md`.
- **HARD-05 live-mcp-server** — AC8 *durable-Postgres audit row* awaits a
  Postgres-backed `AuditStore` (only `InMemoryAuditStore` exists); the bridge,
  redaction, every-op and hash-chain assertions pass offline + live against the
  in-memory store. Third-party hosted-SaaS MCP connector + human SSRF/transport
  pentest are out of agent scope (handed to HARD-09's punch-list). Close durable
  row with: `FORGE_MCP_AUDIT_BACKEND=db … MCP_LIVE_TRANSPORT=true uv run pytest -m
  live_mcp -q`.
- **HARD-06 live-slack** — AC1/AC2/AC11 need a disposable Slack test workspace +
  `SLACK_BOT_TOKEN` / `SLACK_TEST_CHANNEL` / `SLACK_SIGNING_SECRET` + network;
  skips clean. One-time operator step: create the Slack app (scopes
  `chat:write`+`commands`), install to the test workspace, point the `/forge`
  slash-command + interactivity Request URLs at the deployed API. Runbook:
  `docs/runbooks/live-slack.md`.

### The 7 release blockers — CLOSED vs still gated

| # | Release blocker | Owning slices | Status |
|---|-----------------|---------------|--------|
| 1 | No real external systems exercised (GitHub App, model, reranker, MCP, Slack) | HARD-01/02/03/05/06 | **Code CLOSED · live green CRED-GATED** — production clients + env-gated live lanes shipped; G-GH/G-MODEL/G-SLACK live half needs real creds+network; G-MCP self-hosted path proven offline (durable Postgres audit row awaits HARD-01's DB `AuditStore`). |
| 2 | Eval numbers offline/deterministic (fake embedder, 1.000s) | HARD-03, HARD-04 | **CLOSED** — HARD-04 lands honest recall@k / MRR / nDCG@10 on a real corpus via a learned local `sentence-transformers` embedder (no creds); only the optional live cross-encoder reranker leg is cred-gated and has an offline default. |
| 3 | `docker compose build` / `next build` never run; images not `@sha256`-pinned | HARD-07, HARD-08 | **Pin-enforcement CLOSED · real build RUNNER-GATED** — no-floating-tag `@sha256` assertions + Helm/compose validation run in-suite; the actual 4-image build + `next build` + kind/k3d install need a networked CI runner (no creds). |
| 4 | No real security audit / no pentest | HARD-09, HARD-12, HARD-13 | **Automated CLOSED · human pentest PENTEST-GATED** — secret-scan/SAST/dep-audit + RBAC/MCP-write-default-deny/policy-default-deny matrix + SBOM/provenance green; the scoped human penetration test stays a named, owned punch-list item (MANUAL-PENDING, never auto-greened). |
| 5 | Parked items may stay reverted/parked | HARD-13, HARD-14 | **Named §5 items CLOSED · F40 spine DEFERRED** — G-CRYPTO (Fernet default, `FORGE_SECRET_KEY` required) + OAuth land via HARD-13; the re-lock leg is done at the foundation (`4a75308`). HARD-14's systemic F40 future-scope execution machinery was **reverted** → forward-scope stays a scheduled, deferred backlog. |
| 6 | Maturity gaps (coverage, load/perf, migration up/rollback, soak) | HARD-08/10/11/12 | **Mostly CLOSED · real fleet soak MANUAL-PENDING** — coverage, G-TYPES, durable cost ledger, migration up→rollback→re-upgrade, and a bounded simulated soak land; perf/soak execution is runner-gated and the *multi-week multi-tenant fleet soak* is a named MANUAL-PENDING item. |
| 7 | Python 3.14 deferred; eslint held at 9 | HARD-14 (foundation) | **CLOSED (at foundation)** — the tree is on Python 3.14.6 with re-locked deps (`4a75308`, `requires-python >=3.14`); this superseded HARD-14's forward-compat lane. The eslint upgrade go/no-go remains a documented web-toolchain note. |

**Net:** blockers **#2 and #7 are fully CLOSED**; **#3, #4 (automated half), #6
(bulk)** are code-CLOSED with their live execution runner-gated; **#1** is
code-CLOSED with live verification **cred-gated**; **#4 (human pentest)** and
**#6 (multi-week fleet soak)** remain the two honest, never-auto-greened
**MANUAL-PENDING** asterisks the PRODUCTION bar ships verbatim; **#5** closes its
named §5 items but leaves the F40 future-scope spine deferred (HARD-14 reverted).

---

*Maintained by the slice-run gatekeeper; append new runs above this line's section boundary as they land.*
