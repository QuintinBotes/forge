# Forge Production Hardening — Master Index (HARD-01 … HARD-14)

The **hardening tier** is 14 full implementation slices (each in the same 12-section
format as `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md`) that turn
the overnight **ALPHA** — where every claim touching the outside world is mock-,
fixture-, or SQLite-backed (`docs/MORNING_REPORT.md` §5/§6) — into a **BETA you can
run** and a **PRODUCTION release a serious team can adopt, self-host, audit, and
trust**. See [`README.md`](./README.md) for what this tier is, how it maps to the 7
release blockers and the product spec, and the order to implement.

> **Engineering contract:** [`SPEC-PRODUCTION-HARDENING.md`](./SPEC-PRODUCTION-HARDENING.md)
> — release-readiness model (ALPHA/BETA/PRODUCTION bars), the 7-blocker→gate map,
> credential-handling rules, and the numbered Definition of Done.
> **Ground truth:** `docs/MORNING_REPORT.md` (honest ALPHA state + every PARKED item),
> `docs/FORGE_SPEC.md` (product surface the hardening proves against reality),
> `docs/implementation-slices/INDEX.md` (the F01–F39 slice set these extend).

---

## Numbering & cross-reference note (read first)

The slice files in this directory (**HARD-01 … HARD-14**) are the **authoritative**
hardening set. They **renumber** the idealized workstreams sketched in
`SPEC-PRODUCTION-HARDENING.md` (which numbered them by subsystem). Several slices
say so in their own header — e.g. HARD-06 ("the SPEC labels the Slack workstream
HARD-07; this program ships it as HARD-06"), HARD-07 ("spec §HARD-08 … renumbered
HARD-07"), HARD-08 ("the spec's HARD-08 *Container & web build* scope").

Because of that renumber, **in-slice references like "HARD-01 (real pgvector
substrate)" or "HARD-10 (crypto)" follow the spec's numbering, not the file
numbering.** The two **stable** identifiers are:

- **Blocker numbers `#1…#7`** (from the program brief), and
- **Gate ids `G-*`** (from the spec's release-readiness model).

Map by blocker and gate, not by raw "HARD-NN" inside prose. The tables below resolve
every gate to the delivered slice(s) that realize it.

A consequence worth stating plainly: the **real-Postgres/pgvector substrate** (spec
gate **G-DB**) and the **whole-workspace typecheck fix** (spec gate **G-TYPES**) do
**not** get a dedicated slice file in the delivered set — they are **cross-cutting
prerequisites** woven through several slices (see the "Cross-cutting gates" note under
the BETA checklist). The release-readiness meta-gate (HARD-12) mechanically checks
them regardless of who owns the fix.

---

## Master table — HARD-01 … HARD-14

Legend — **Needs real creds?**: `Yes` = a user-supplied external credential is required
for the verified/live half (test skips clean without it); `Optional` = a live half
exists but a self-hosted / no-key path is the default; `No` = no external credential
(a *networked/CI runner* or *local container* may still be required — see the Runner
column note below). **Effort**: `S` ≈ ≤1 wk · `M` ≈ 1–2 wk · `L` ≈ 3–4 wk.

| ID | Title | Blocker(s) | Needs real creds? | Effort | Link |
|---|---|---|---|---|---|
| HARD-01 | Live GitHub App Integration | #1 | **Yes** — GitHub App id + `.pem` + test repo | L | [HARD-01-live-github-app.md](./HARD-01-live-github-app.md) |
| HARD-02 | Live Model Provider (BYOK Anthropic/OpenAI) | #1 | **Yes** — `ANTHROPIC_API_KEY` *or* `OPENAI_API_KEY` | M | [HARD-02-live-model-byok.md](./HARD-02-live-model-byok.md) |
| HARD-03 | Live Cross-Encoder Reranker (Jina/Cohere) | #1, #2 | **Optional** — Jina/Cohere key *or* self-hosted reranker; offline default needs none | M | [HARD-03-live-reranker.md](./HARD-03-live-reranker.md) |
| HARD-04 | Real RAG Eval on a Real Corpus (honest recall@k / MRR / nDCG) | #2 | **No** — local `sentence-transformers` embedder; BYOK reranker optional | M | [HARD-04-real-eval-corpus.md](./HARD-04-real-eval-corpus.md) |
| HARD-05 | Live MCP Server over Real Transport (stdio / Streamable-HTTP) | #1 | **Optional** — self-hosted reference server preferred; token only if authed | M | [HARD-05-live-mcp-server.md](./HARD-05-live-mcp-server.md) |
| HARD-06 | Live Slack Integration (bot token + signed slash command + interactivity) | #1 | **Yes** — `SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET` | M | [HARD-06-live-slack.md](./HARD-06-live-slack.md) |
| HARD-07 | Container & Web Build + Image Digest Pinning | #3 | **No** — needs build/registry network (CI runner) | M | [HARD-07-docker-build-and-pin.md](./HARD-07-docker-build-and-pin.md) |
| HARD-08 | Kubernetes Helm Deploy (lint/template/install on local k8s + smoke) | #3, #6 | **No** — needs Docker + local kind/k3d (networked runner) | L | [HARD-08-kubernetes-helm-deploy.md](./HARD-08-kubernetes-helm-deploy.md) |
| HARD-09 | Security Audit (automated) + Threat Model + Pentest Punch-list | #4 | **No** — generated test keys; human pentest stays a punch-list item | L | [HARD-09-security-hardening.md](./HARD-09-security-hardening.md) |
| HARD-10 | Production Observability, Cost Accounting & Telemetry | #6 (supports #1, #2) | **No** external — real-cost AC piggybacks HARD-02; live pipeline needs networked runner | L | [HARD-10-observability-cost-prod.md](./HARD-10-observability-cost-prod.md) |
| HARD-11 | Reliability & Maturity (coverage, migration rollback, load/perf, soak, shutdown, rate-limit, idempotency) | #6 | **No** — migration/perf/soak need live pgvector + resourced runner | L | [HARD-11-reliability-maturity.md](./HARD-11-reliability-maturity.md) |
| HARD-12 | Release Engineering, Supply-Chain & the Automated `RELEASE_READINESS` Gate | #4, #6 | **No** external — release half uses GitHub OIDC + `GITHUB_TOKEN` on CI | M | [HARD-12-release-engineering.md](./HARD-12-release-engineering.md) |
| HARD-13 | Production Secrets & Config Hardening | #4 | **No** external — Vault dev server for optional provider; rotation drill needs live pgvector | M | [HARD-13-secrets-config-prod.md](./HARD-13-secrets-config-prod.md) |
| HARD-14 | Future-Scope Execution (the F40 deferred backlog on the real foundation) | #5 | **No** for the in-scope spine; cred/cluster tail is named & deferred | L (XL tail) | [HARD-14-future-scope-execution.md](./HARD-14-future-scope-execution.md) |

**Runner column note (what cannot run in the no-network sandbox).** Even where
*creds* are `No`, some halves need a **networked/CI runner** or a **live container**:
`docker compose build` / `next build` / registry digest resolution (HARD-07), kind/k3d
install (HARD-08), the OTLP→collector→Grafana pipeline + alert-fire (HARD-10), load/
perf/soak + migration-on-pgvector (HARD-11), the signing/provenance/publish release
half (HARD-12), and the live integration lanes for HARD-01/02/05/06. The **hermetic
default suite stays green and network-free**; every live test is `@pytest.mark.integration`
(or an opt-in CI job) that **skips clean** when its creds/runner are absent.

### Effort roll-up

`L` × 6 (HARD-01, -08, -09, -10, -11, -14) · `M` × 8 (HARD-02, -03, -04, -05, -06, -07,
-12, -13). HARD-14's in-scope deliverable is `L`; the full F40 fan-out (125 `D-*`
items) is an explicitly-scheduled `XL` tail it does **not** finish.

---

## Blocker → workstream coverage (every blocker is owned)

| # | Release blocker | Closed by | BETA/PROD gate(s) |
|---|---|---|---|
| 1 | No real external systems exercised (GitHub App, model/embedder/reranker, MCP, Slack mock/fixture-backed) | HARD-01, HARD-02, HARD-03, HARD-05, HARD-06 | G-GH, G-MODEL, G-RAG-REAL, G-MCP, G-SLACK |
| 2 | Eval numbers offline/deterministic (fake embedder + fixture reranker; perfect 1.000s) | HARD-03, HARD-04 | G-RAG-REAL |
| 3 | `docker compose build` + `next build` never run for real; images not `@sha256`-pinned | HARD-07, HARD-08 | G-BUILD, G-IMG-PINNED |
| 4 | No real security audit / no pentest (secrets, auth/RBAC, MCP write-default, policy) | HARD-09, HARD-12 (supply-chain), HARD-13 (secrets) | G-SEC-AUTOMATED, G-SEC-EVIDENCE, G-CRYPTO |
| 5 | Parked items may remain reverted/parked (LangGraph swap, tree-sitter, Fernet/OAuth, re-lock, 3.14) | HARD-13 (crypto/OAuth), HARD-14 (re-lock, tree-sitter/LangGraph verify, 3.14 lane, F40) | G-PARKED-CLOSED, G-FWD-COMPAT |
| 6 | Maturity gaps: low worker/agent coverage, no load/perf, no migration up/rollback, no multi-tenant soak | HARD-08 (k8s install/migrate), HARD-10 (telemetry/cost), HARD-11 (coverage/migrate/perf/soak), HARD-12 (release discipline) | G-COVERAGE, G-PERF, G-MIGRATE, G-SOAK, G-TYPES |
| 7 | Python 3.14 deferred (RC + pydantic/PEP 649), eslint held at 9 | HARD-14 | G-FWD-COMPAT |

> Shared prerequisite: the **real Postgres + pgvector substrate** (spec gate **G-DB**)
> underwrites blockers #2 and #6. It has no dedicated slice file; it is exercised
> inside HARD-08 (Alembic hook on in-cluster pgvector), HARD-09 (audit-immutability
> trigger), HARD-10 (durable cost ledger), HARD-11 (migration round-trip + perf +
> soak), and HARD-13 (rotation drill on a populated DB).

---

## BETA gate checklist — "real, with named limits"

BETA is met only when **every** box is green from captured command output (not
asserted), and the **whole-suite green gate** holds at the end of every workstream:
`uv run pytest -q` + `uv run ruff check .` + `uv run ruff format --check .` +
`make typecheck` + `cd apps/web && pnpm test`.

- [ ] **Whole-suite green gate** — hermetic suite green + network-free; every creds-bearing test skips clean when creds absent. *(enforced/checked by HARD-12; respected by all)*
- [ ] **G-DB** — `alembic upgrade head` → `downgrade base` → `upgrade head` + `pytest -m postgres` green on live pgvector; the 3 ALPHA skips now **execute**. *(cross-cutting → HARD-11, with HARD-08/09/10/13)*
- [ ] **G-MODEL** — one BYOK provider completes a live `forge_agent` run end-to-end behind the integration marker; redaction verified on the live path. *(HARD-02)*
- [ ] **G-RAG-REAL** — recall@5/recall@10/MRR/**nDCG@10** on a real corpus via a learned local embedder + real reranker; honest (not 1.000-by-construction); hybrid beats single-leg in ablation; `MORNING_REPORT.md §4` headline superseded. *(HARD-03 + HARD-04)*
- [ ] **G-GH** — live installation-token mint from the env-only `.pem` + branch push + PR open + verified webhook HMAC (tamper rejected) on a test repo. *(HARD-01)*
- [ ] **G-MCP** — live MCP read over real transport through the gateway; write-default-deny, RFC 8707 token binding, namespace scoping, redacted audit row. *(HARD-05)*
- [ ] **G-SLACK** — live message post (`ok:true`/`ts`) + slash-command/interactivity signature verification (bad/stale rejected). *(HARD-06)*
- [ ] **G-BUILD** — `docker compose build` (all 4 images) + `next build` succeed for real on a networked runner; web standalone bundle. *(HARD-07; HARD-08 proves it deploys on k8s)*
- [ ] **G-TYPES** — `make typecheck` is one green command across the workspace ("source file found twice" fixed) and a blocking CI step. *(cross-cutting prerequisite; mechanically checked by HARD-12's readiness gate)*
- [ ] **G-SEC-AUTOMATED** — secret scan + SAST + dependency audit green in CI (zero unwaived high/critical); RBAC default-deny, MCP write-default-false, policy default-deny enforcement tests pass on live paths. *(HARD-09)*
- [ ] **G-CRYPTO** — `FernetCipher` default; `FORGE_SECRET_KEY` **required** (no silent ephemeral fallback outside an explicit dev flag); config var-name drift closed. *(HARD-13)*

**BETA explicitly tolerates:** no human pentest (punch-list only), no real-fleet
multi-week soak, toolchain held at Python 3.13 / eslint 9. A `BETA_REPORT.md` (HARD-12)
records each gate's evidence and restates these tolerations verbatim.

---

## PRODUCTION gate checklist — "trustworthy, with one honest asterisk"

All BETA gates **plus** (each green from captured evidence):

- [ ] **G-IMG-PINNED** — every `image:` in `deploy/docker-compose.yml` pinned by immutable `@sha256` digest; a test asserts no floating tags; `docker compose config` validates; shellcheck green on deploy scripts. *(HARD-07; HARD-08 enforces the same on the Helm prod profile)*
- [ ] **G-PARKED-CLOSED** — every `MORNING_REPORT.md §5` PARKED item closed with code+test, or a dated, owned, slice-linked deferral: OAuth code-exchange live + crypto default *(HARD-13)*; tree-sitter active w/ fallback + LangGraph verified on the real path + `uv lock` re-locked + CI `--frozen` *(HARD-14)*; shellcheck wired *(HARD-07)*. *(HARD-13, HARD-14)*
- [ ] **G-PERF** — retrieval p50/p95/p99 at corpus size N meet documented budgets; API hot-path load test meets documented budgets; results published. *(HARD-11; cost/latency telemetry from HARD-10)*
- [ ] **G-MIGRATE** — populated-DB `upgrade → rollback → re-upgrade` is data-preserving, with a documented rollback runbook in `docs/self-hosting/upgrade.md`. *(HARD-11; hook ordering + rollback mechanics also proven on kind in HARD-08)*
- [ ] **G-SOAK** — bounded multi-tenant soak: zero cross-tenant leak (row-id assertions), bounded memory/connections/FDs, published soak report. *(HARD-11)*
- [ ] **G-COVERAGE** — `apps/worker` and `packages/agent-runtime` ≥ 90% each (error/escalation/cleanup paths covered); overall ≥ 93%. *(HARD-11)*
- [ ] **G-SEC-EVIDENCE** — full evidence pack: SBOM (source-tree + per-image), dependency audit, SAST report, secret-scan report, RBAC/MCP/policy enforcement matrix, secrets-rotation runbook — **plus** a scoped human-pentest punch-list with severities + owners. *(HARD-09 + HARD-07/08 image SBOMs + HARD-12 source SBOM/provenance/signing + HARD-13 rotation runbook)*
- [ ] **G-FWD-COMPAT** — a Python 3.14-RC CI lane runs (xfail-annotated for known pydantic/PEP 649 gaps); eslint upgrade has a written go/no-go; `uv lock` re-locked, CI runs `uv sync --frozen`. *(HARD-14)*
- [ ] **`RELEASE_READINESS.md` meta-gate** — `forge-release-readiness --bar production` runs/inspects every gate above and renders a dated, evidenced MET/NOT-MET report; non-zero exit if the bar is not met. *(HARD-12)*
- [ ] **Honest asterisk shipped verbatim** in the release notes: "Code- and evidence-ready for production, pending an external human penetration test and a real multi-week multi-tenant fleet soak — neither performable by the build agents; both are named, scoped, and handed off." Compliance attestation (SOC2 etc.) listed as out of scope. *(HARD-09 punch-list + HARD-12 readiness footer)*

> **PRODUCTION's one honest asterisk:** the **3rd-party human penetration test** and a
> **real multi-week, multi-tenant production fleet soak** cannot be executed by build
> agents. HARD-09 ships the automated evidence + scoped pentest punch-list; HARD-11
> ships a *bounded, simulated* soak. Both human-only items stay `MANUAL-PENDING` in the
> HARD-12 readiness gate forever until a signed attestation is filed — they are never
> auto-greened.

---

## At-a-glance: what each gate needs to actually run

| Gate | Owning slice(s) | Creds | Runner |
|---|---|---|---|
| G-DB | HARD-11 (+08/09/10/13) | none | live pgvector container |
| G-MODEL | HARD-02 | BYOK model key | networked |
| G-RAG-REAL | HARD-03 + HARD-04 | none (local embedder); reranker optional | local (model cache) |
| G-GH | HARD-01 | GitHub App + `.pem` + repo | networked |
| G-MCP | HARD-05 | self-hosted server (token if authed) | local/networked |
| G-SLACK | HARD-06 | Slack bot token + signing secret | networked |
| G-BUILD / G-IMG-PINNED | HARD-07 (+08) | none | networked + registry |
| G-TYPES | cross-cutting (checked by HARD-12) | none | local |
| G-SEC-AUTOMATED / G-SEC-EVIDENCE | HARD-09 (+07/08/12/13) | none | CI |
| G-CRYPTO | HARD-13 | none | local |
| G-PARKED-CLOSED | HARD-13, HARD-14 | OAuth IdP client (one live test) | local/networked |
| G-PERF / G-MIGRATE / G-SOAK / G-COVERAGE | HARD-11 (+08, +10) | none | resourced + live pgvector |
| G-FWD-COMPAT | HARD-14 | none | CI (3.14-RC lane) |
| `RELEASE_READINESS` | HARD-12 | GitHub OIDC (release half) | CI |
