# Forge — Production Hardening Spec

> **Purpose.** Forge's overnight ALPHA stands up the whole platform (13 `forge_*`
> packages + 4 apps, ~944 Python tests + 28 web tests green, ruff clean, 96%
> coverage) but every claim that touches the *outside world* is mock-, fixture-,
> or SQLite-backed. This spec is the engineering contract that turns that
> trustworthy-on-paper ALPHA into a **BETA you can run** and a **PRODUCTION
> release a serious team can adopt, self-host, audit, and trust** — by exercising
> the real systems the ALPHA only simulated.
>
> **Relation to existing docs.**
> - `docs/MORNING_REPORT.md` is the honest ground truth: §5 (every PARKED item),
>   §6 (known gaps), §7 (ranked next steps). This spec promotes those gaps into
>   numbered workstreams (HARD-01..HARD-14) with green gates and acceptance
>   criteria. Where the report says "parked / compiles-only / fixture", this spec
>   says exactly what real exercise looks like and how it is verified.
> - `docs/FORGE_SPEC.md` is the product spec; it is the source of truth for *what*
>   each subsystem must do (RAG pipeline, MCP security rules, RBAC roles, deploy
>   best-practices, security table). Hardening does **not** change the product
>   surface — it proves the surface against reality.
> - `docs/implementation-slices/INDEX.md` (F01–F39) and the sample slice
>   `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` define the
>   12-section slice format. Each HARD-NN workstream is intended to be expanded
>   into a full 12-section slice that **extends** the named existing `forge_*`
>   package(s) — never a new parallel package — and conforms to the real
>   `forge_db` schema (singular tables, `Enum(native_enum=False)` str enums) and
>   the frozen `forge_contracts` DTOs/Protocols.
>
> **The honest beta-vs-production ceiling.** Agents in this program *can* stand up
> real Postgres+pgvector, call real model/embedder/reranker/GitHub/Slack/MCP
> endpoints with the supplied credentials, run a genuine local embedder for
> honest eval numbers, build the images, and run load/soak/upgrade tests. Agents
> **cannot**: (a) perform a 3rd-party human penetration test or a formal security
> audit sign-off — HARD-09 delivers automated SAST/secret-scan/dependency-audit/
> RBAC-enforcement evidence and a scoped pentest **punch-list**, but the human
> pentest itself stays an explicit, named gap; (b) operate a real multi-tenant
> production cluster over weeks (we simulate soak, we do not run a real fleet);
> (c) obtain SOC2/compliance attestation. Those three are called out everywhere
> they touch a gate so PRODUCTION is never *claimed* on the back of work that was
> only simulated.

---

## Release readiness model

Three named bars. A bar is met only when **every** gate under it is green from
real command output (evidence captured), not asserted. The whole-suite green gate
(`uv run pytest -q` + `uv run ruff check .` + `make typecheck` + `cd apps/web &&
pnpm test`) must stay green at the **end of every workstream** — no workstream may
leave the tree red, even transiently across a merge.

### ALPHA bar (already met — baseline, restated for delta tracking)
- Whole Python suite green (~944 passed, ≤3 skipped), web suite green (28), ruff
  clean. Coverage ≥ 90% overall.
- All subsystems implemented and unit/contract-tested against fakes/fixtures.
- `docker compose config` validates; CI workflow exists with a pgvector service.
- **Asterisk:** every external boundary is mock/fixture/SQLite; eval numbers are
  deterministic perfect 1.000s; images not built; no real security audit.

### BETA bar (this program's first milestone — "real, with named limits")
Checkable gates:
1. **G-DB** — real Postgres+pgvector exercised: `alembic upgrade head` +
   `alembic downgrade base` + `pytest -m postgres` all green against a live
   pgvector container; the 3 ALPHA skips are now **runs**, not skips. (HARD-01)
2. **G-MODEL** — at least one real BYOK model provider (Anthropic *or* OpenAI)
   completes a live agent run end-to-end behind `forge_agent`, gated by an
   integration marker; redaction verified on the live path. (HARD-02)
3. **G-RAG-REAL** — RAG eval runs on a **learned** embedder (local
   `sentence-transformers`, no API key required) + a real reranker over a real
   multi-file corpus, producing honest recall@k / MRR / nDCG that are reported as
   the headline and are **not** 1.000-by-construction. (HARD-03, HARD-04)
4. **G-GH** — real GitHub App: installation token minted from the env-only
   `.pem`, a branch pushed, a PR opened, and a CI webhook received+verified
   against a real test repo, behind an integration marker. (HARD-05)
5. **G-MCP** — real MCP server reached over HTTP through the gateway: read-only
   default enforced, RFC 8707 token binding sent, audit row written with secrets
   redacted. (HARD-06)
6. **G-SLACK** — real Slack workspace: an approval/notification message posts and
   an inbound slash-command request passes signature verification. (HARD-07)
7. **G-BUILD** — `docker compose build` builds all 4 Dockerfiles and `next build`
   produces a production web bundle, both for real (in CI or a networked runner).
   (HARD-08)
8. **G-TYPES** — `make typecheck` is green across the whole workspace (the ALPHA
   "source file found twice" bug is fixed) and is a blocking CI gate. (HARD-12)
9. **G-SEC-AUTOMATED** — secret scan, SAST, and dependency audit run in CI with
   zero high/critical findings unwaived; RBAC default-deny + MCP write-default +
   policy default-deny have explicit live-path enforcement tests. (HARD-09)
10. **G-CRYPTO** — production crypto seam active by default: `FernetCipher` (real
    `cryptography`) is the default cipher, `FORGE_SECRET_KEY` is **required** (no
    ephemeral-key fallback) outside an explicit dev flag. (HARD-10)

> BETA explicitly tolerates: human pentest not done (punch-list only), no
> long-duration real-fleet soak, toolchain held at Python 3.13 / eslint 9.

### PRODUCTION bar (adoption-grade — "trustworthy, with one honest asterisk")
All BETA gates **plus**:
11. **G-IMG-PINNED** — every image in `deploy/docker-compose.yml` pinned by
    immutable `@sha256` digest (resolved from a registry); `docker compose
    config` + a digest-presence test confirm no floating tags. (HARD-08)
12. **G-PARKED-CLOSED** — every PARKED item in MORNING_REPORT §5 is either
    closed with a real implementation+test or has a written, dated deferral with
    an owner and a follow-up slice id. Specifically: tree-sitter chunking active
    with a fallback (HARD-11), LangGraph swap verified on the real path
    (HARD-11), OAuth code-exchange implemented against a real IdP (HARD-10),
    `uv lock` re-locked and CI runs `--frozen` (HARD-14).
13. **G-PERF** — retrieval p50/p95/p99 measured on the real embedder+pgvector at
    a defined corpus size and meet documented budgets; a load test of the API
    hot paths meets documented throughput/latency budgets; results published.
    (HARD-13)
14. **G-MIGRATE** — migration upgrade→rollback→re-upgrade proven on a populated
    Postgres (data-preserving), with a documented rollback runbook. (HARD-13)
15. **G-SOAK** — a multi-tenant soak (≥N tenants, mixed read/write/index/agent
    load, ≥ documented duration) runs with no tenant cross-leak, no unbounded
    growth, no FD/connection leak; results published. (HARD-13)
16. **G-COVERAGE** — `apps/worker` and `packages/agent-runtime` coverage ≥ 90%
    with error/escalation/cleanup paths covered; overall ≥ 93%. (HARD-12)
17. **G-SEC-EVIDENCE** — full security evidence pack produced: SBOM, dependency
    audit, SAST report, secret-scan report, RBAC/MCP/policy enforcement matrix,
    secrets-rotation runbook — **and** a scoped 3rd-party pentest punch-list with
    severities and remediation owners. (HARD-09)
18. **G-FWD-COMPAT** — a green Python 3.14-RC CI lane exists (xfail-annotated for
    known pydantic/PEP 649 gaps) and eslint upgrade is evaluated with a written
    go/no-go. (HARD-14)

> **PRODUCTION's one honest asterisk:** items 17's *human pentest* and a
> real-world multi-week multi-tenant fleet soak cannot be executed by agents in
> this sandbox. PRODUCTION is declared "code- and evidence-ready, pending the
> named external human pentest" — that line ships in the release notes verbatim.

---

## The 7 blockers → workstreams mapping

| # | Release blocker (from program brief) | Workstreams that close it | BETA/PROD gate(s) |
|---|---|---|---|
| 1 | No real external systems exercised (GitHub App, model/embedder/reranker, MCP, Slack mock/fixture-backed) | HARD-02, HARD-03, HARD-05, HARD-06, HARD-07 | G-MODEL, G-RAG-REAL, G-GH, G-MCP, G-SLACK |
| 2 | Eval numbers offline/deterministic (fake embedder + fixture reranker; perfect 1.000s) | HARD-01 (substrate), HARD-03 (real embedder/reranker), HARD-04 (eval) | G-RAG-REAL |
| 3 | `docker compose build` + `next build` never run for real; images not pinned by `@sha256` | HARD-08 | G-BUILD, G-IMG-PINNED |
| 4 | No real security audit / no pentest (secrets, auth/RBAC, MCP write-default, policy) | HARD-09 (automated) + named human-pentest punch-list | G-SEC-AUTOMATED, G-SEC-EVIDENCE |
| 5 | Parked items may remain reverted/parked (LangGraph swap, tree-sitter, Fernet/OAuth) | HARD-10 (crypto/OAuth), HARD-11 (tree-sitter/LangGraph), HARD-14 (re-lock) | G-CRYPTO, G-PARKED-CLOSED |
| 6 | Maturity gaps: low coverage on worker/agent-runtime, no load/perf, no migration up/rollback, no multi-tenant soak | HARD-01 (migration on real PG), HARD-12 (coverage/typecheck), HARD-13 (load/perf/soak/migrate) | G-TYPES, G-COVERAGE, G-PERF, G-MIGRATE, G-SOAK |
| 7 | Python 3.14 deferred (RC + pydantic/PEP 649), eslint held at 9 | HARD-14 | G-FWD-COMPAT |

Every blocker is owned by at least one workstream; HARD-01 (real DB substrate) is
a shared prerequisite for blockers 2 and 6.

---

## Workstreams

> Convention for each: **Goal** (what real thing gets exercised) · **Gate**
> (the whole-suite green gate is implied for *all*; listed here is the *added*
> acceptance evidence) · **Creds needed?** · **Blocker closed**. Each workstream
> EXTENDS the named existing `forge_*` package/app, conforms to the real
> `forge_db` schema + frozen `forge_contracts`, reads creds from env, and redacts
> secrets. New real-boundary tests live behind a pytest marker (proposed:
> `@pytest.mark.integration`) so the hermetic default suite stays green and
> network-free; the integration lane runs only when creds are present.

### HARD-01 — Real Postgres + pgvector substrate
- **Goal.** Turn the single biggest ALPHA asterisk green: run the real SQL paths
  on a live pgvector Postgres instead of the SQLite stand-in / dialect-compile
  checks. Apply the Alembic baseline, exercise `PgVectorStore` cosine (`<=>`,
  HNSW), `Bm25Store` `ts_rank`/`@@`, and the audit-immutability trigger.
  Extends: `packages/db` (`forge_db`), `packages/knowledge-core`
  (`forge_knowledge.stores`), `apps/api` audit writer; uses the existing root
  `conftest.py` `postgres_url`/`pg_engine` fixtures (already wired for
  `FORGE_TEST_DATABASE_URL` / testcontainers).
- **Gate / added evidence.**
  1. `alembic upgrade head` then `alembic downgrade base` then `upgrade head`
     again, clean, against a live `pgvector/pgvector:pg16` container.
  2. `uv run pytest -m postgres` — the 3 ALPHA skips (`test_test_infra`,
     `test_stores_postgres` ×2) now **execute and pass**; skip count drops to ≤0
     for the postgres marker when the DB is present.
  3. pgvector cosine ranking and BM25 `ts_rank` ranking each assert a known
     ordering on a seeded corpus (not just "compiles").
  4. The audit-immutability trigger rejects UPDATE/DELETE on the audit table
     (real trigger, real error), satisfying FORGE_SPEC "immutable audit log".
- **Creds needed?** No external creds — local container only.
- **Blocker closed.** #6 (migration/DB realism); prerequisite for #2.

### HARD-02 — Real model provider (BYOK Anthropic/OpenAI)
- **Goal.** Drive one real agent run end-to-end through `forge_agent` against a
  real BYOK model provider, replacing the mocked model client. Prove the
  plan→act→observe loop, tool dispatch, confidence/handoff, and the LangGraph
  `StateGraph` execute on a real model's responses, with secret redaction on the
  live request/response path and BYOK keys resolved from the vault, never logged.
  Extends: `packages/agent-runtime` (`forge_agent`), `apps/worker`
  (`forge_worker.agent_runner`), `apps/api/forge_api/auth/vault.py` resolver.
- **Gate / added evidence.**
  1. `@pytest.mark.integration` test: a minimal task runs to a terminal state
     against the real provider chosen by `FORGE_MODEL_PROVIDER` (anthropic|openai)
     using the BYOK key from env; bounded-loop/`GraphError` backstop still holds.
  2. Token/cost accounting recorded to the observability path on the real run.
  3. Redaction assertion: no API key or secret appears in any logged step, trace,
     or audit row from the live run.
  4. Provider is swappable via DI/env; absent creds, the test **skips** (suite
     stays green) — never silently falls back to a fake on the integration lane.
- **Creds needed?** Yes — BYOK Anthropic or OpenAI key via `.env.integration`.
- **Blocker closed.** #1.

### HARD-03 — Real embedder + reranker (BYOK + local open-weight)
- **Goal.** Replace the deterministic fake embedder and fixture reranker with
  real implementations on a callable path: (a) BYOK hosted embedder
  (OpenAI-compatible) and reranker (Jina/Cohere) behind the existing
  `EmbeddingProvider`/`Reranker` protocols; **and** (b) a **local open-weight
  `sentence-transformers` embedder** so honest eval numbers can be produced with
  **no API key**. Extends: `packages/knowledge-core`
  (`forge_knowledge.embeddings`, `forge_knowledge.reranker`); embedding dim
  config must match the real model and the `retrieval_chunk.embedding` vector
  column.
- **Gate / added evidence.**
  1. A local `sentence-transformers` embedder loads and embeds the eval corpus
     offline (no network at call time once the model is cached); its output
     dimension is reconciled with the `vector(N)` column (migration note if the
     ALPHA default differs).
  2. `@pytest.mark.integration`: BYOK hosted embedder and reranker each return
     real scores over a sample query; reranker reorders candidates vs fused order.
  3. Reranker degradation path: with reranker disabled/unhealthy, pipeline
     degrades to weighted-RRF (no crash) — asserted.
  4. No secret leakage; keys from env/vault only.
- **Creds needed?** Optional for (a) BYOK Jina/Cohere + embedder; **not** required
  for (b) the local embedder, which is the BETA-critical path for honest eval.
- **Blocker closed.** #1 and #2 (enables HARD-04).

### HARD-04 — Real RAG eval (honest recall@k / MRR / nDCG)
- **Goal.** Re-run the retrieval eval through the real local embedder (HARD-03b)
  + a real reranker over a **real, heterogeneous corpus** (not the small curated
  golden set), and publish honest metrics that replace the deterministic 1.000s
  as the headline. Re-examine the Track 1.4 adversarial refutation
  (`forge_knowledge.sync` + `forge_eval.retrieval_eval`) flagged in
  MORNING_REPORT §6. Extends: `packages/evaluation` (`forge_eval`),
  `packages/knowledge-core` eval hooks.
- **Gate / added evidence.**
  1. recall@5, recall@10, MRR, and nDCG@10 computed on the real corpus with the
     learned embedder; numbers are realistic (a perfect 1.000 across the board is
     treated as a red flag to investigate, not a pass).
  2. Ablation recorded: hybrid (semantic+BM25+RRF+rerank) measurably beats
     vector-only and keyword-only on the same corpus (proves fusion adds recall).
  3. A regression gate is set from the *real* baseline (e.g. recall@5 ≥ a chosen
     floor) and wired into the harness; future drops block.
  4. The Track 1.4 refutation is explicitly re-reviewed and marked
     resolved/invalid with a one-line rationale in the eval doc.
  5. MORNING_REPORT §4's headline numbers are superseded; the deterministic
     numbers are relabeled "wiring check only".
- **Creds needed?** No (local embedder); BYOK reranker optional to also report
  hosted-reranker deltas.
- **Blocker closed.** #2.

### HARD-05 — Real GitHub App integration
- **Goal.** Exercise the real GitHub App against a disposable test repo: mint an
  installation access token from the env-only private key
  (`deploy/secrets/github-app.pem`), push a branch, open a PR with spec
  traceability, and receive+verify a CI/push webhook (HMAC signature). Replaces
  the fixture-only payloads in `forge_integrations.github` /
  `forge_integrations.webhooks`. Extends: `packages/integration-sdk`
  (`forge_integrations.github`, `.webhooks`), `apps/api` integration router.
- **Gate / added evidence.**
  1. `@pytest.mark.integration`: JWT→installation-token exchange succeeds using
     the `.pem` loaded from env/secret path; the `.pem` is never logged/committed.
  2. A PR is opened on the test repo and its number/URL asserted; cleanup deletes
     the branch/PR.
  3. A real webhook delivery's HMAC signature verifies with the configured
     secret; a tampered payload is rejected.
  4. Rate-limit/abuse handling and 4xx/5xx error paths covered.
- **Creds needed?** Yes — GitHub App id + private key (`.pem`, env-only) + a test
  repo/installation.
- **Blocker closed.** #1.

### HARD-06 — Real MCP gateway transport
- **Goal.** Connect the gateway to a **real MCP server over HTTP** (a reference
  knowledge-retrieval server) and prove the FORGE_SPEC "MCP Security Rules" on
  the live path: read-only (`allow_write:false`) default, RFC 8707 `resource`
  token binding, namespace scoping, input validation, and a full audit row
  (tool/resource name, payload hash, status, latency) with secrets redacted.
  Replaces the `httpx.MockTransport` stand-in. Extends: `packages/mcp-sdk`
  (`forge_mcp`), `apps/mcp-gateway` (`forge_mcp_gateway`).
- **Gate / added evidence.**
  1. `@pytest.mark.integration`: a live resource read returns normalized snapshots
     over real HTTP transport.
  2. A write/tool call is **denied by default** and only permitted after explicit
     admin enablement — asserted on the live path.
  3. Token binding (`resource` param) is sent; a token without binding is
     rejected by policy.
  4. Audit row written with payload hashed and any secret redacted; namespace
     scoping blocks out-of-scope reads.
- **Creds needed?** Yes if the chosen MCP server needs auth; a self-hosted
  reference server (no external SaaS) is acceptable and preferred.
- **Blocker closed.** #1.

### HARD-07 — Real Slack integration
- **Goal.** Post a real approval/notification message to a real Slack workspace
  and verify an inbound `/forge` slash-command request's signature
  (`X-Slack-Signature` v0 HMAC + timestamp anti-replay). Replaces the
  formatter-against-fixtures state of `forge_integrations.slack`. Extends:
  `packages/integration-sdk` (`forge_integrations.slack`), `apps/api`
  integration/approval routers, `forge_integrations.webhooks` (Slack verifier).
- **Gate / added evidence.**
  1. `@pytest.mark.integration`: a message posts to a test channel via the bot
     token (asserted on the API response `ok:true` / `ts`).
  2. Inbound slash-command signature verification passes for a correctly signed
     request and rejects a bad signature and a stale timestamp.
  3. Approval action (approve/reject) round-trips an interactive payload.
  4. Bot token resolved from env/vault, never logged.
- **Creds needed?** Yes — Slack bot token + signing secret (test workspace).
- **Blocker closed.** #1.

### HARD-08 — Container & web build, image digest pinning
- **Goal.** Actually build the 4 Dockerfiles (`deploy/docker/{api,worker,
  mcp-gateway,web}.Dockerfile`) via `docker compose build`, run a real
  `next build` for `apps/web`, then pin every image in
  `deploy/docker-compose.yml` by immutable `@sha256` digest and statically lint
  the shell scripts (`deploy/scripts/{backup,restore}.sh`). Extends: `deploy/`,
  `apps/web` build config, `.github/workflows/ci.yml` (the `compose` job today
  only runs `config`, not `build`).
- **Gate / added evidence.**
  1. `docker compose -f deploy/docker-compose.yml build` succeeds for all 4
     services on a networked runner/CI.
  2. `cd apps/web && pnpm build` (`next build`) produces a production bundle;
     wired into the web CI job (already present) and confirmed green.
  3. Every `image:` in `docker-compose.yml` carries an `@sha256:` digest; a test
     asserts no floating tags remain; `docker compose config` still validates.
  4. `shellcheck` passes on `backup.sh`/`restore.sh` (added to CI).
  5. Containers run as non-root with healthchecks + resource limits per
     FORGE_SPEC "Production Docker Compose Requirements".
- **Creds needed?** No — needs build network/registry access (CI or a networked
  runner). **Cannot run in the no-network sandbox** — explicitly a CI/networked
  gate.
- **Blocker closed.** #3.

### HARD-09 — Security audit (automated) + pentest punch-list
- **Goal.** Produce the automatable half of a real security audit and a scoped
  human-pentest punch-list. Automated: secret scanning (gitleaks/trufflehog),
  SAST (bandit/semgrep), dependency audit (`pip-audit`/`uv` + `pnpm audit`),
  SBOM, and an explicit **enforcement matrix** proving the FORGE_SPEC Security
  table on the live paths: RBAC default-deny per role, MCP write-default-false,
  policy default-deny, secret redaction in logs/traces/retrieval, auth-required
  on every route, rate limiting. Extends: `apps/api/forge_api/auth/*`
  (`rbac`, `vault`, `crypto`, `apikeys`), `forge_policy`, `forge_mcp`, CI.
- **Gate / added evidence.**
  1. Secret scan, SAST, dependency audit run in CI; **zero** high/critical
     findings unwaived (each waiver dated + justified).
  2. Enforcement tests: `viewer` denied writes; `agent-runner` allowed only
     `/search`+`/retrieve`; MCP write denied by default; policy default-deny on an
     unlisted action; redaction holds on a live trace; unauthenticated request →
     401 on a sampled route per router.
  3. SBOM generated and committed to the evidence pack.
  4. A scoped **pentest punch-list** is written: attack surface inventory
     (auth, BYOK vault, MCP transport, webhook verifiers, agent sandbox escape,
     SSRF via MCP/embedder URLs), per-item severity, and remediation owner —
     ready to hand to a human pentester.
- **Creds needed?** No external creds (uses test creds for enforcement tests).
- **Blocker closed.** #4 — *partially and honestly*: the automated audit closes;
  the **human pentest remains a named punch-list item** (cannot be done by agents).

### HARD-10 — Production crypto + OAuth seam
- **Goal.** Un-park the two security seams from MORNING_REPORT §5(6,7): make
  `FernetCipher` (real `cryptography`/libsodium) the **default** at-rest cipher
  for the BYOK vault, **require** `FORGE_SECRET_KEY` (remove the ephemeral-key
  prod fallback; gate dev fallback behind an explicit `FORGE_DEV_INSECURE` flag),
  and implement the OAuth authorization-code → token → user exchange against a
  real IdP (Google/GitHub/GitLab per FORGE_SPEC). Extends:
  `apps/api/forge_api/auth/{crypto,vault,oauth,service}.py`.
- **Gate / added evidence.**
  1. `cryptography` added + re-locked (`uv lock`); `FernetCipher` round-trips and
     is the default; the stdlib-AEAD cipher remains as a tested fallback only.
  2. App refuses to start in prod mode without `FORGE_SECRET_KEY` (asserted);
     dev-insecure path requires the explicit flag and logs a loud warning.
  3. Existing-data decrypt path verified (vault values written under the old
     cipher still readable, or a documented re-encrypt migration provided).
  4. `@pytest.mark.integration`: OAuth code exchange against a real IdP (or a
     conformant test IdP) yields a user; state/PKCE validated; CSRF/replay guarded.
- **Creds needed?** OAuth client id/secret for one IdP (for the live exchange
  test); crypto/secret-key work needs none.
- **Blocker closed.** #5.

### HARD-11 — Un-park tree-sitter chunking + verify LangGraph swap
- **Goal.** Close the remaining ALPHA "PARKED/looks-done" code items:
  (a) add the tree-sitter multi-language chunking backend behind the existing
  `forge_knowledge.chunking` registry with a line-window fallback (the spec's
  required Python-AST + markdown path already exists; tree-sitter is the drop-in
  breadth upgrade); (b) verify the LangGraph swap (MORNING_REPORT §5(5) marks it
  DONE/H5) actually executes on the real agent path under HARD-02 and that
  `recursion_limit → GraphError` semantics hold. Extends:
  `packages/knowledge-core` (`forge_knowledge.chunking`), `packages/agent-runtime`
  (`forge_agent.graph`).
- **Gate / added evidence.**
  1. tree-sitter emits function/class chunks with correct line ranges for ≥2
     non-Python languages; unsupported languages fall back to line-window
     (asserted); `uv lock` updated for the `tree_sitter` dep.
  2. Chunk-type weights and dedup (`content_hash`) still hold with the new
     backend; no regression in HARD-04 eval.
  3. A test confirms the real `langgraph.StateGraph` executes the loop and that
     exceeding `recursion_limit` raises `GraphError` (bounded-loop backstop).
- **Creds needed?** No.
- **Blocker closed.** #5.

### HARD-12 — Coverage, error-paths, and whole-workspace typecheck
- **Goal.** Raise coverage on the least-covered, most side-effecty code —
  `apps/worker` (78.9%) and `packages/agent-runtime` (82.4%) — focusing on error,
  escalation, retry, and worktree-cleanup paths; and fix the workspace-wide mypy
  "source file found twice" bug so `make typecheck` is one green command and a
  blocking CI gate. Extends: `apps/worker` (`forge_worker`),
  `packages/agent-runtime` (`forge_agent`), `mypy.ini`/`Makefile`.
- **Gate / added evidence.**
  1. `make typecheck` exits 0 across the whole workspace (module-mode invocation
     per the existing Makefile comment); CI `Type-check` step green and blocking.
  2. `apps/worker` and `packages/agent-runtime` coverage ≥ 90% each, with named
     tests for: Celery task failure/retry, escalation/handoff on low confidence,
     git-worktree sandbox cleanup on crash, and policy-denied tool dispatch.
  3. Overall coverage ≥ 93% (no regression below ALPHA's 96% target band beyond
     the agreed worker/agent floor).
- **Creds needed?** No.
- **Blocker closed.** #6.

### HARD-13 — Load, performance, migration upgrade/rollback, multi-tenant soak
- **Goal.** Produce the maturity evidence ALPHA lacks: retrieval latency
  p50/p95/p99 on the real embedder + pgvector at a defined corpus size; an API
  hot-path load test; a populated-DB migration upgrade→rollback→re-upgrade proof;
  and a multi-tenant soak verifying isolation and resource stability. Extends:
  `packages/knowledge-core`, `apps/api`, `packages/db` (migration tooling),
  `packages/evaluation` (perf harness), plus a `deploy/` load profile.
- **Gate / added evidence.**
  1. Retrieval p50/p95/p99 measured at corpus size = documented N and meet
     documented budgets (FORGE_SPEC "retrieval latency p50/p95/p99" metric);
     results published.
  2. API load test (e.g. board/search/retrieve hot paths) meets documented
     throughput + latency budgets; no error-rate cliff under target concurrency.
  3. Migration: `upgrade head` on a seeded DB → `downgrade` one rev →
     `upgrade head` again, **data-preserving**, with a rollback runbook in
     `docs/self-hosting/upgrade.md`.
  4. Multi-tenant soak: ≥ N tenants under mixed read/write/index/agent load for
     the documented duration with zero cross-tenant leak (row-id assertions),
     bounded memory/connections/FDs (no leak), and a published soak report.
- **Creds needed?** No external creds (local embedder + local PG); a networked/CI
  runner with enough resources. **Real multi-week fleet soak is out of scope —
  this is a bounded, simulated soak; the limitation is stated in the report.**
- **Blocker closed.** #6.

### HARD-14 — Toolchain forward-compat + dependency re-lock
- **Goal.** Stop deferring the toolchain risk: add a Python 3.14-RC CI lane
  (xfail-annotated for known pydantic v2 / PEP 649 deferred-annotation gaps),
  evaluate the eslint 9 → latest upgrade with a written go/no-go, and re-lock the
  whole workspace (`uv lock`) so `forge_eval`'s declared deps (`forge-knowledge`,
  `forge-db`) are pinned and CI runs `uv sync --frozen`. Extends:
  `pyproject.toml`, `uv.lock`, `apps/web` eslint config, `.github/workflows/ci.yml`.
- **Gate / added evidence.**
  1. `uv lock` produces a committed lockfile; CI uses `--frozen` and is green on
     3.13 (the supported lane).
  2. A 3.14-RC CI lane runs the suite; known-failing cases are `xfail` with a
     linked upstream reason; the lane is non-blocking but visible.
  3. eslint upgrade evaluated: either upgraded with web suite green, or a dated
     written deferral with the blocking reason.
- **Creds needed?** No.
- **Blocker closed.** #7.

---

## Credentials & secrets handling

The user is supplying **real** credentials. Non-negotiable handling rules, applied
by every workstream that takes creds (HARD-02, -03, -05, -06, -07, -10):

1. **Env-only ingress.** All secrets are read from process env, sourced from a
   **gitignored** `.env.integration` at the repo root (and the GitHub App private
   key from `deploy/secrets/github-app.pem`). `.gitignore` already ignores
   `.env`, `.env.*` (except `*.example`), and `*.pem` — verified. `deploy/secrets/`
   must be added to `.gitignore` if not already covered, and **never** staged.
2. **Never committed, never logged, never in fixtures.** No real cred value may
   appear in source, tests, snapshots, lockfiles, CI logs, audit rows, traces, or
   error messages. The existing redaction filter (shared across `forge_api`,
   `forge_mcp`, `forge_knowledge`) is the single source of truth; new live paths
   re-apply it defensively (as F05 §8 already prescribes for MCP).
3. **Resolved at call time from the vault.** BYOK keys live in the encrypted
   per-workspace vault (`forge_api.auth.vault`) keyed by `APIKeyKind`
   (`model_provider`, `integration_token`, `mcp_token`); workstreams resolve them
   per request and discard — they are not held in module globals or echoed.
4. **`.pem` is a file path, not a value.** The GitHub App private key is loaded
   from `deploy/secrets/github-app.pem` (path from env), used to sign the JWT,
   and the key material never enters logs or the DB.
5. **Integration tests are creds-gated and skip-clean.** Every real-boundary test
   is `@pytest.mark.integration` and **skips with a clear reason** when its creds
   are absent — so the default hermetic suite stays green and network-free in the
   sandbox, and the integration lane only runs where creds exist. No live path
   ever silently falls back to a fake on the integration marker.
6. **`.env.integration.example`** (no secrets, key names only) is committed so the
   required variable set is discoverable; the real `.env.integration` is not.
7. **Rotation runbook.** HARD-09 ships `docs/self-hosting/security.md`
   credential-rotation procedures; agent tokens get automatic expiry per the
   FORGE_SPEC Security table.

Required env keys (names only; values live in `.env.integration`):
`FORGE_SECRET_KEY`, `FORGE_MODEL_PROVIDER`, `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`,
`EMBEDDING_PROVIDER` / `EMBEDDING_MODEL`, `JINA_API_KEY` or `COHERE_API_KEY`,
`JINA_RERANKER_URL`, `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY_PATH` /
`GITHUB_WEBHOOK_SECRET` / `GITHUB_TEST_REPO`, `MCP_SERVER_URL` / `MCP_TOKEN`,
`SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET`, `OAUTH_CLIENT_ID` /
`OAUTH_CLIENT_SECRET`, `FORGE_TEST_DATABASE_URL`.

---

## Sequencing & dependencies

**Critical path (what unblocks what):**

```
HARD-01 (real PG+pgvector)  ──┬─> HARD-03 (real/local embedder+reranker) ─> HARD-04 (honest eval)
                              │
                              ├─> HARD-13 (perf/migration/soak; needs real PG + real embedder)
                              │
HARD-12 (typecheck/coverage) ─┴─> (keeps suite green; gate for everything)

HARD-02 (real model) ──> HARD-11b (LangGraph verified on real path)
HARD-10 (crypto/secret-key) ──> all creds-bearing workstreams (vault must be real)
HARD-08 (build+pin) ── independent, needs networked runner/CI
HARD-09 (security) ── runs continuously; depends on real auth (HARD-10) for live RBAC tests
HARD-14 (re-lock/3.14) ── must land after deps are added (HARD-03, -10, -11) so the lock is complete
```

Recommended order:
1. **HARD-01** and **HARD-12** first — real DB substrate + a green whole-workspace
   typecheck/coverage floor make every later gate trustworthy.
2. **HARD-10** next — the real crypto/secret-key/vault must exist before live BYOK
   creds flow through it (HARD-02/03/05/06/07 all resolve keys from the vault).
3. **HARD-03 → HARD-04** — local embedder gives honest eval without waiting on any
   API key; this is the highest-signal BETA deliverable for blocker #2.
4. **HARD-02, HARD-05, HARD-06, HARD-07** — the four real-external integrations,
   parallelizable, each creds-gated and independent.
5. **HARD-11** — tree-sitter (independent) + LangGraph verification (after HARD-02).
6. **HARD-08** — build + digest pin on a networked runner/CI.
7. **HARD-13** — perf/migration/soak (needs HARD-01 + HARD-03 real paths).
8. **HARD-14** — re-lock last, after all new deps (`cryptography`, `tree_sitter`,
   `sentence-transformers`) are added; stand up the 3.14-RC lane.

**What requires real creds (cannot complete on the local hermetic path):**
HARD-02 (model key), HARD-05 (GitHub App + repo), HARD-06 (MCP server, if authed),
HARD-07 (Slack), HARD-10's OAuth-exchange test (IdP client). HARD-03's local
embedder, HARD-01, HARD-04, HARD-11, HARD-12, HARD-13 need **no external creds**.

**What needs a networked/CI runner (no-network sandbox cannot do it):**
HARD-08 (`docker compose build`, `next build`, registry digest resolution),
HARD-13 (resource-heavy load/soak), the 3.14-RC lane in HARD-14, and the live
integration lanes for HARD-02/05/06/07.

**What needs a human and cannot be done by agents at all (named, not hidden):**
- A 3rd-party **human penetration test** — HARD-09 produces the punch-list and the
  automated evidence; the pentest engagement itself is an external gap.
- A **real multi-week, multi-tenant production fleet soak** — HARD-13 delivers a
  bounded simulated soak; a true fleet over weeks is not reproducible in-sandbox.
- **Compliance attestation** (SOC2 etc.) — out of scope for the codebase; noted so
  it is never implied by a green gate.

---

## Definition of Done

### DoD — BETA (numbered, testable)
BETA is DONE when all of the following are green from captured command output:

1. Whole-suite green gate passes: `uv run pytest -q` (≤ the agreed integration
   skips when creds absent), `uv run ruff check .`, `uv run ruff format --check .`,
   `make typecheck` (exit 0), and `cd apps/web && pnpm test`.
2. **G-DB:** `alembic upgrade head` + `downgrade base` + `pytest -m postgres` all
   green on live pgvector; the 3 ALPHA skips now execute. (HARD-01)
3. **G-TYPES:** `make typecheck` is one green command across the workspace and a
   blocking CI step. (HARD-12)
4. **G-RAG-REAL:** recall@5/recall@10/MRR/nDCG@10 reported on a real corpus via a
   learned local embedder + real reranker; numbers are honest (not 1.000-by-
   construction); hybrid beats single-leg in the ablation; MORNING_REPORT §4
   headline superseded. (HARD-03, HARD-04)
5. **G-MODEL:** one BYOK provider completes a live `forge_agent` run end-to-end
   behind the integration marker, with redaction verified. (HARD-02)
6. **G-GH:** live installation-token mint + PR open + verified webhook on a test
   repo. (HARD-05)
7. **G-MCP:** live MCP read over HTTP through the gateway with write-default-deny,
   token binding, and a redacted audit row. (HARD-06)
8. **G-SLACK:** live message post + slash-command signature verification. (HARD-07)
9. **G-BUILD:** `docker compose build` (4 images) and `next build` succeed for
   real on CI/networked runner. (HARD-08)
10. **G-CRYPTO:** `FernetCipher` default, `FORGE_SECRET_KEY` required (no silent
    ephemeral fallback). (HARD-10)
11. **G-SEC-AUTOMATED:** secret scan + SAST + dependency audit green in CI (no
    unwaived high/critical); RBAC/MCP-write/policy default-deny enforcement tests
    pass on live paths. (HARD-09)
12. Every creds-bearing test skips cleanly (suite stays green) when creds are
    absent; no real secret appears in source, logs, fixtures, or the lockfile.
13. A `BETA_REPORT.md` records each gate's evidence and restates the explicit BETA
    tolerations (no human pentest, no fleet soak, toolchain held at 3.13/eslint 9).

### DoD — PRODUCTION (numbered, testable)
PRODUCTION is DONE when all BETA items hold **and**:

14. **G-IMG-PINNED:** every `image:` in `deploy/docker-compose.yml` is pinned by
    `@sha256` digest; a test asserts no floating tags; `docker compose config`
    validates; `shellcheck` green on deploy scripts. (HARD-08)
15. **G-PARKED-CLOSED:** every MORNING_REPORT §5 PARKED item is closed with code
    + test, or carries a dated, owned, slice-linked deferral. Specifically:
    tree-sitter active w/ fallback (HARD-11), LangGraph verified on the real path
    (HARD-11), OAuth code-exchange live (HARD-10), `uv lock` re-locked + CI
    `--frozen` (HARD-14), shellcheck wired (HARD-08).
16. **G-PERF:** retrieval p50/p95/p99 at corpus size N meet documented budgets;
    API hot-path load test meets documented budgets; results published. (HARD-13)
17. **G-MIGRATE:** populated-DB upgrade→rollback→re-upgrade is data-preserving
    with a documented rollback runbook in `docs/self-hosting/upgrade.md`. (HARD-13)
18. **G-SOAK:** bounded multi-tenant soak passes — zero cross-tenant leak, bounded
    memory/connections/FDs, published report. (HARD-13)
19. **G-COVERAGE:** `apps/worker` and `packages/agent-runtime` ≥ 90% each (error/
    escalation/cleanup paths covered); overall ≥ 93%. (HARD-12)
20. **G-SEC-EVIDENCE:** security evidence pack complete — SBOM, dependency audit,
    SAST report, secret-scan report, RBAC/MCP/policy enforcement matrix,
    secrets-rotation runbook (`docs/self-hosting/security.md`) — **plus** a scoped
    human-pentest punch-list with severities + owners. (HARD-09)
21. **G-FWD-COMPAT:** a Python 3.14-RC CI lane runs (xfail-annotated for known
    pydantic/PEP 649 gaps); eslint upgrade has a written go/no-go. (HARD-14)
22. The release notes carry the honest asterisk **verbatim**: "Code- and
    evidence-ready for production, pending an external human penetration test and
    a real multi-week multi-tenant fleet soak — neither performable by the build
    agents; both are named, scoped, and handed off." Compliance attestation is
    listed as out of scope.

> A gate is only "done" with captured evidence (command + output, or report
> artifact). No PRODUCTION gate may be claimed on the strength of work that was
> only simulated or fixture-backed — that is precisely the ALPHA debt this program
> exists to retire.
