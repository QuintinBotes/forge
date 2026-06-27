# Forge V1 — Morning Report

**Generated:** 2026-06-27
**Build:** overnight swarm (Phase 0 architect → Phase 1 parallel fan-out → Phase 2 integration/verification → Phase 3 report)
**Plan:** `docs/superpowers/plans/2026-06-26-forge-v1-overnight.md`
**Design/safety rules:** `docs/superpowers/specs/2026-06-26-forge-overnight-design.md`

This report is written to be honest, not flattering. Numbers below were read from
actual command output (re-run at report time), not assumed. Where a thing is
parked or weaker than it looks, it says so.

---

## 0. TL;DR

- **Monorepo stands up:** 13 Python packages + 4 apps (`api`, `worker`, `mcp-gateway`, `web`) under uv + pnpm workspaces.
- **Tests:** **941 passed, 3 skipped** (Python) + **28 passed** (web/Vitest) = **969 passing, 3 skipped**. `ruff check .` clean. Coverage **96%** overall.
- **RAG spine works end-to-end** on a real on-disk sample repo: chunk → embed → index → hybrid (semantic + BM25) → RRF (k=60) → rerank → attributed top-k. Golden eval **recall@5 = 1.000, MRR = 1.000** (see the honesty caveat in §4).
- **Biggest honest caveats:** (a) no live Postgres/pgvector was available in the sandbox, so all vector/BM25 SQL paths ran on a dialect-aware SQLite stand-in and against the postgresql dialect *compiler* only — real pgvector execution is parked for Phase 2/CI; (b) eval numbers are perfect because the pipeline ran on a deterministic offline embedder + fixture reranker over small curated golden sets, not a learned model over a large corpus; (c) `make typecheck` (workspace-wide mypy) is broken by a known monorepo invocation bug.

---

## 1. V1 checklist status

Legend: **DONE** = implemented + tested + green gate verified from output · **PARTIAL** = real but with a named gap · **PARKED** = deliberately deferred with a `# PARKED:` marker (see §5).

### Phase 0 — Architect (committed before fan-out)

| Item | Status | Where | Notes |
|---|---|---|---|
| 0.1 Workspace + tooling (uv + pnpm, ruff, Makefile, LICENSE) | DONE | `pyproject.toml`, `ruff.toml`, `Makefile`, `LICENSE`, `pnpm-workspace.yaml` | `ruff check .` green; `uv` workspace resolves. `mypy.ini`/`make typecheck` broken — see PARKED. |
| 0.2 Core data model (22 SQLAlchemy models + Alembic baseline) | DONE (Postgres apply PARKED) | `packages/db/forge_db/models/*`, `migrations/`, `tests/test_models.py`, `tests/test_migration.py` | Models import; `create_all` on SQLite; migration upgrade/downgrade on SQLite; pgvector/tsvector/JSONB verified via offline dialect compile. Live Postgres apply parked. |
| 0.3 Shared contracts (Pydantic DTOs + Protocols) | DONE | `packages/contracts/forge_contracts/*`, `tests/test_contracts.py` | 100% coverage; frozen signatures Phase 1 built against. |
| 0.4 API skeleton, all routers pre-registered | DONE | `apps/api/forge_api/main.py`, `routers/*`, `tests/test_api_skeleton.py` | `/health` 200; every feature router mounted (stubs since replaced by real handlers in Phase 1/2). |
| 0.5 Frontend skeleton (Next.js + Tailwind + shadcn/ui + TanStack Query) | DONE | `apps/web/` | Vitest component tests green (28). Full `next build` not re-run at report time — see Known Gaps. |
| 0.6 Deploy + CI + test infra | DONE (build/digest PARKED) | `deploy/docker-compose*.yml`, `deploy/caddy/`, `.github/workflows/ci.yml`, root `conftest.py` | `docker compose config` validates (`tests/test_deploy_infra.py`, 19 passed). Image build + sha256 pinning + shellcheck parked. |

### Phase 1 — Feature fan-out

| Item | Status | Where | Notes |
|---|---|---|---|
| 1.1 RAG chunking (Python AST + markdown semantic + weights) | DONE (tree-sitter PARKED) | `packages/knowledge-core/forge_knowledge/chunking.py`, `tests/test_chunking.py` | stdlib `ast` for Python, paragraph for others; chunk-type weight table. tree-sitter multi-lang parked. |
| 1.2 Embeddings + stores (BYOK embedder, PgVector cosine, BM25) | DONE (live pgvector PARKED) | `forge_knowledge/embeddings.py`, `stores.py`, `tests/test_embeddings.py`, `test_stores.py`, `test_stores_postgres.py` | Deterministic fake embedder tested; PgVector/BM25 SQL compiles for postgresql dialect, runs on SQLite stand-in. 2 Postgres-only tests skip. |
| 1.3 RRF fusion + reranker + search service | DONE | `forge_knowledge/fusion.py`, `reranker.py`, `service.py`, `retriever.py`; `apps/api/.../routers/knowledge.py`; `apps/worker/.../indexer.py` | RRF k=60 hand-verified; `/knowledge/search` + `/index` real; indexer Celery task real. Jina client via `httpx.MockTransport`. |
| 1.4 Sync modes + golden retrieval eval | DONE | `forge_knowledge/sync.py`; `apps/worker/.../syncer.py` (`sync_source_task`); `packages/evaluation/forge_eval/retrieval_eval.py`; `tests/test_sync.py`, `test_eval_recall.py` | Full + incremental (git-diff) sync; `/knowledge/sync` real; worker `sync_source_task` registered in `celery_app` (closed in Phase 2). 1 adversarial refutation recorded, not marked repaired — see Known Gaps. |
| 1.5 Board core + API | DONE | `packages/board-core/forge_board/*`; `apps/api/.../routers/board.py`; `tests/test_board_service.py`, `test_dependency_graph.py` | CRUD, status workflow, bulk ops, dependency graph with `CycleError`, filters; tenant scoping tests. |
| 1.6 Board UI (list, kanban, Cmd+K, optimistic + rollback, WS) | DONE | `apps/web/src/components/board/*`, `components/command-palette.tsx`, `lib/realtime/use-board-realtime.ts` | 28 Vitest tests green (list, kanban, palette, optimistic rollback, realtime hook). |
| 1.7 Spec engine (SDD lifecycle + manifest + gates + traceability) | DONE | `packages/spec-engine/forge_spec/*`; `apps/api/.../routers/spec.py`; `tests/test_spec_lifecycle.py`, `test_spec_traceability.py` | constitution→create→clarify→plan→tasks→validate; manifest r/w; gate blocks implement; R→A→task traceability. |
| 1.8 Workflow engine (FSM + retry/escalation + DSL) | DONE | `packages/workflow-engine/forge_workflow/*`; `routers/workflow.py`; `tests/test_fsm.py`, `test_default_workflow.py`, `test_dsl.py` | Default feature workflow; retry (max 3, backoff); escalation; YAML DSL parser. Store is dialect-agnostic (SQLite-tested). |
| 1.9 Agent runtime (graph loop, tool registry, sandbox, AGENTS.md) | DONE | `packages/agent-runtime/forge_agent/*`; `routers/agent.py`; `apps/worker/.../agent_runner.py`; `tests/test_graph.py`, `test_tools_and_policy.py`, `test_sandbox.py` | plan→act→observe loop; policy-gated tool dispatch; git-worktree sandbox; AGENTS.md loader; confidence/handoff. Backed by a real `langgraph.graph.StateGraph` (H5) behind the in-house builder API; `GraphError`/bounded-loop semantics preserved. |
| 1.10 Policy SDK (`.forge/policy.yaml` loader + evaluator) | DONE | `packages/policy-sdk/forge_policy/*`; `routers/policy.py`; `tests/test_evaluator.py`, `test_loader.py` | write allow/deny globs, allowed/restricted actions, default-deny. |
| 1.11 Skill SDK (profiles + behavior injection) | DONE | `packages/skill-sdk/forge_skill/*`; `tests/test_skill_sdk.py` | builtin profiles; injects test-first/coverage floors/forbidden shortcuts; unknown profile raises. |
| 1.12 MCP SDK + gateway (client, query-through, audit, security) | DONE (live transport mocked) | `packages/mcp-sdk/forge_mcp/*`; `apps/mcp-gateway/*`; `routers/mcp.py`; `tests/test_client.py`, `test_security.py`, `test_audit.py` | read-only default, RFC 8707 token binding, namespace scoping, audit with redaction. Transport mocked. |
| 1.13 Integration SDK (GitHub App + Slack + PMAdapter) | DONE (fixtures only) | `packages/integration-sdk/forge_integrations/*`; `routers/integration.py`; `tests/test_github.py`, `test_slack.py`, `test_webhooks.py` | PR-open payload, CI webhook parser, Slack formatter — all against fixtures, no live calls. |
| 1.14 Observability + audit | DONE (DB-trigger immutability PARKED) | `apps/api/.../routers/observability.py`, audit writer; `tests/test_obs_audit.py`, `test_obs_trace.py`, `test_obs_redaction.py`, `test_obs_otel.py` | append-only audit, run-trace assembler, OTel hooks, secret redaction. Postgres immutability trigger compile-checked only. |
| 1.15 Auth & secrets (API key, BYOK vault, RBAC) | DONE (Fernet + OAuth-exchange PARKED) | `apps/api/forge_api/auth/*`; `routers/auth.py`; `tests/test_auth_*.py` | API-key round-trip; RBAC denies viewer writes; encrypt-at-rest via stdlib AEAD (HMAC-SHA256 CTR + encrypt-then-MAC). Fernet/libsodium backend + OAuth code exchange parked; dev uses ephemeral key if `FORGE_SECRET_KEY` unset. |
| 1.16 Evaluation harness + golden task set (≥30) | DONE | `packages/evaluation/forge_eval/*`; `golden/v1_task_set.yaml`; `tests/test_tasks.py`, `test_harness.py` | 36 golden tasks (≥30); harness runs fake pipeline; scorecard + regression gate. |
| 1.17 Examples + self-hosting docs | DONE | `examples/{policies,skills,workflows,mcp-connectors,specs}`, `docs/self-hosting/*`; `examples/tests/*` | 5 policy types, skills, 3 workflows, 4 MCP connectors, 2 specs; 7 self-hosting docs; example YAMLs validate against their loaders (40 tests). |

### Phase 2 — Integration + verification

| Item | Status | Where | Notes |
|---|---|---|---|
| 2.1 Wire-up + build | PARTIAL | routers + worker tasks | All routers serve real handlers; worker tasks (`index_source`, `sync_source`, `run_agent`) registered. `uv sync`/`alembic upgrade`/`docker compose build` against Postgres parked (no Postgres/registry network). |
| 2.2 Full-suite green + coverage | DONE | whole workspace | 941 passed / 3 skipped, 96% coverage. |
| 2.3 Adversarial sweep | DONE | across packages | Bug-hunt + completeness mapping reflected in this checklist; security pass present in `apps/api/tests/test_security_fixes.py`. |
| 2.4 End-to-end spine smoke | DONE | `tests/test_spine_smoke.py` | Indexes `examples/sample-repo`, hybrid search returns attributed/reranked hits, prints recall@k/MRR. 7 passed. |

### Not built (intentionally out of V1 scope)

| Item | Status | Notes |
|---|---|---|
| Multi-agent coordinator | STUB | `packages/multi-agent-coordinator/forge_coordinator/__init__.py` is a placeholder (no task 1.x targets it; multi-agent orchestration is V2 per the plan's self-review). |

---

## 2. How to run it

### First-time setup
```bash
make setup        # uv sync (Python) + pnpm install (Node)
# then bring up infra and migrate:
make dev          # docker compose -f deploy/docker-compose.dev.yml up
make migrate      # alembic upgrade head   (needs Postgres up)
make seed         # seed a demo workspace
```
> Note: `make dev`, `make migrate`, and the full `docker compose build` require Docker + a Postgres/pgvector container and base-image network access — none of which were available in the build sandbox, so those paths are **not** verified here (see §5).

### Run the tests
```bash
# Python (whole workspace): 941 passed, 3 skipped
make test                       # == uv run pytest
uv run pytest packages/knowledge-core   # one package
uv run ruff check .             # lint (clean)

# Web (Vitest): 28 passed
cd apps/web && pnpm test
```

### Run the RAG eval / spine smoke
```bash
# Full end-to-end spine smoke + golden eval, printed:
uv run pytest tests/test_spine_smoke.py -s
# or run the smoke directly as a script:
uv run python tests/test_spine_smoke.py

# Compact in-package retrieval eval (recall@k / MRR on a synthetic repo):
uv run pytest packages/knowledge-core/tests/test_eval_recall.py -s

# Golden task-set harness (≥30 tasks):
uv run pytest packages/evaluation -s
```

### Postgres-backed tests (Phase 2 / CI)
```bash
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/forge_test
uv run pytest -m postgres     # currently skipped without a live pgvector DB
```

---

## 3. Test + coverage summary

**Python:** `uv run pytest` → **941 passed, 3 skipped, 1 warning** in ~30s.
**Web:** `pnpm test` → **28 passed** (8 files).
**Lint:** `ruff check .` → **All checks passed.**
**Typecheck:** `mypy packages apps` → **FAILS (exit 2)** — known monorepo bug (see §5).

The 3 skips are all the same parked reason (no live Postgres):
`tests/test_test_infra.py::...`, `packages/knowledge-core/tests/test_stores_postgres.py` (×2).

The 1 warning: `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` (FastAPI TestClient; cosmetic).

**Coverage: 96% overall** (12,396 statements, 442 missed). Per package (implementation only, excluding test files):

| Package / app | Coverage |
|---|---|
| packages/contracts | 100.0% |
| packages/db | 97.6% |
| packages/spec-engine | 97.5% |
| packages/workflow-engine | 96.9% |
| packages/skill-sdk | 96.7% |
| packages/policy-sdk | 96.6% |
| packages/board-core | 95.6% |
| apps/mcp-gateway | 94.5% |
| packages/evaluation | 94.5% |
| packages/knowledge-core | 93.7% |
| packages/mcp-sdk | 93.7% |
| apps/api | 93.2% |
| packages/integration-sdk | 93.1% |
| packages/agent-runtime | 82.4% |
| apps/worker | 78.9% |

(`multi-agent-coordinator` reports 100% but is a 2-statement placeholder — ignore.)

---

## 4. RAG eval numbers

End-to-end spine smoke (`tests/test_spine_smoke.py`, real pipeline over `examples/sample-repo`):

- 7 mixed natural-language + exact-identifier queries, all retrieve their expected file in top-5.
- Exact identifier `verify_token` recovered at **rank 1** (proves the BM25 keyword leg).
- Pipeline confirmed: semantic (cosine) + keyword (BM25) → **RRF (k=60)** → cross-encoder rerank + chunk-type weight boost → attributed top-k (path / source_id / source_uri / line span).

Golden retrieval eval (`packages/evaluation`, 22 cases, k=5):

```
mean recall@5 = 1.000   MRR = 1.000   hit_rate = 1.000   passed = 22/22
gate (recall@5 >= 0.850): PASS
```

In-package retrieval eval (`packages/knowledge-core/tests/test_eval_recall.py`): ≥15 query→expected-path pairs over a synthetic indexed repo, asserts conservative recall@k / MRR thresholds — green.
Golden task harness (`packages/evaluation`): **36** tasks (≥30 required), scorecard + regression gate green.

> **Honesty caveat — read this before trusting the 1.000s.** These scores are perfect because the eval runs on a **deterministic offline pipeline** (signed feature-hashing embeddings + a fixture token-overlap reranker) over **small, curated golden sets** built alongside the code, with an **in-memory SQLite** backend computing cosine/BM25 in Python (no live pgvector). This proves the *wiring and ranking logic* are correct end-to-end; it does **not** measure real-world retrieval quality with a learned embedding model on a large heterogeneous corpus. Re-run against a real BYOK embedder + Jina reranker + pgvector on a realistic corpus before quoting these numbers externally.

---

## 5. PARKED items (every one, with reason)

1. **Live Postgres / pgvector execution** — no Postgres or network in the sandbox. Affected: `packages/db` baseline-migration apply (`tests/test_migration.py`), `PgVectorStore.search` cosine + `Bm25Store.search` ts_rank (`packages/knowledge-core/tests/test_stores_postgres.py`), and the Postgres audit immutability trigger. *Mitigation:* migration runs as real alembic upgrade/downgrade on SQLite; all Postgres SQL is verified to compile against the postgresql dialect (`VECTOR(1536)`, `TSVECTOR`, `JSONB`, `<=>`, `to_tsvector/plainto_tsquery/ts_rank/@@`). 3 tests skip with a clear reason. **Closes in Phase 2 / CI** via `FORGE_TEST_DATABASE_URL` (pgvector service container).

2. **`make typecheck` (workspace-wide mypy) broken** — `uv run mypy packages apps` fails (exit 2): *"Source file found twice under different module names."* This is a pre-existing monorepo invocation/roots bug (reproduces on already-committed `packages/contracts`), not introduced by feature work. Per-package mypy via package discovery (`mypy -p <pkg> --explicit-package-bases`) is clean. **Belongs to tooling (Task 0.1).**

3. **tree-sitter multi-language chunking** — `tree_sitter` not installed; active path is stdlib `ast` for Python + paragraph chunking for everything else (`packages/knowledge-core/forge_knowledge/chunking.py`). The required V1 deliverable (Python AST + markdown semantic + weights) is fully implemented; tree-sitter is a drop-in backend for later.

4. **Live model/reranker/embedding HTTP calls** — forbidden overnight (no external network). `JinaRerankerClient` and `HttpEmbeddingClient` (OpenAI-compatible BYOK) are wired and exercised only via `httpx.MockTransport`. Real-provider path not called.

5. **LangGraph swap** — DONE (H5). The agent runtime now executes on a real `langgraph.graph.StateGraph` (langgraph 1.2.x, added to `packages/agent-runtime`); `forge_agent/graph.py` is a thin adapter over it that preserves the builder API, `GraphError` construction semantics, and the bounded-loop backstop (LangGraph `recursion_limit` → `GraphError`).

6. **Crypto backend (Fernet/libsodium)** — `cryptography`/libsodium not installed and `uv sync` disallowed this phase. The default, fully-tested cipher is stdlib authenticated encryption (HMAC-SHA256 CTR keystream + encrypt-then-MAC, per-message key separation) in `apps/api/forge_api/auth/crypto.py`. `FernetCipher` is the untested production seam.

7. **OAuth authorization-code exchange** — the callback→tokens→user exchange in `apps/api/forge_api/auth/service.py` is parked (needs a live IdP); API-key auth is fully implemented and tested. Also: if `FORGE_SECRET_KEY` is unset, dev falls back to an ephemeral key (`# PARKED-FOR-PROD`).

8. **Docker image build + sha256 digest pinning** — `docker compose build` of the 4 Dockerfiles not runnable (no network for base images / `uv sync` / `pnpm`); images pinned to explicit version tags instead of immutable `@sha256` digests (digest resolution needs registry network). `docker compose config` *does* validate. PARKED markers in `deploy/docker-compose.yml` + `deploy/README.md`; **closes in Phase 2 Task 2.1**.

9. **shellcheck** — not installed; `deploy/scripts/{backup,restore}.sh` were not statically linted.

10. **`forge_eval` lockfile** — `pyproject` declares `forge-knowledge` + `forge-db` deps but was not re-locked (`uv sync` forbidden this phase). The editable workspace venv resolves them (all tests pass); CI re-locks.

---

## 6. Known gaps (honest)

- **No real database/infra was exercised.** Everything DB-shaped ran on SQLite or dialect-compile checks. The *first* thing to do with real Postgres+pgvector is run `alembic upgrade head` and the `-m postgres` suite; until then, treat all SQL paths as "compiles + unit-tested logic," not "proven on Postgres."
- **Eval scores are offline/deterministic** (see §4 caveat). They validate wiring, not real retrieval quality.
- **`docker compose build` and full `next build` were not run** at report time (no base-image network; build not re-verified). `pnpm test`/`ruff`/`pytest` were. The compose file only passed `config` validation.
- **Track 1.4 adversarial review** recorded **1 refutation, repaired=false** in the run data, yet its green gate passed. The sync/eval code is green now, but that refutation deserves a fresh look (it may have been judged invalid, or silently superseded in Phase 2) — re-review `packages/knowledge-core/forge_knowledge/sync.py` + `forge_eval/retrieval_eval.py`.
- **Multi-agent coordinator is a stub** (V2 scope) — no orchestration of multiple agents exists.
- **Workspace mypy is red** (§5 item 2): there is currently no single command that type-checks the whole repo.
- **Lowest-coverage units** are `apps/worker` (78.9%) and `packages/agent-runtime` (82.4%) — the most "side-effecty" code (Celery tasks, worktree sandbox) is the least covered; that's where untested error paths most likely hide.
- **Provider/transport realism:** GitHub, Slack, MCP transport, model, embedder, and reranker are all fixture/mock-backed. No interaction with a real external system has ever happened.

---

## 7. Ranked next steps

1. **Stand up real Postgres+pgvector and turn the skips green.** `alembic upgrade head` against the container, then `pytest -m postgres` (vector cosine, BM25 ts_rank, migration apply, audit immutability trigger). This is the single highest-value validation — it removes the biggest "unproven" asterisk.
2. **Fix `make typecheck`.** Resolve the mypy "source file found twice" by setting explicit package bases / `mypy_path` so one command type-checks the whole workspace, then make it a CI gate.
3. **`docker compose build` + `next build` end-to-end** with network, then pin images by `@sha256` digest and lint shell scripts (shellcheck). Verify the 4 Dockerfiles actually build.
4. **Re-run the RAG eval against a real BYOK embedder + Jina reranker on a realistic corpus** and publish honest recall@k/MRR — replace the deterministic-fake numbers as the headline.
5. **Close the parked review on Track 1.4** (sync/eval refutation) and add the dedicated tree-sitter chunking backend.
6. **Wire the production crypto + OAuth seams:** swap in `FernetCipher` (add `cryptography`), require `FORGE_SECRET_KEY`, and implement the OAuth code exchange against a real IdP fixture/integration.
7. **Raise coverage on `apps/worker` and `agent-runtime`**, especially error/escalation/cleanup paths.
8. **Re-lock dependencies** (`uv lock`) so `forge_eval`'s declared deps are pinned, and run the full CI workflow once with services.

---

## 8. Commit log

```
88de1ce feat: phase 2 integration, fixes, verification, and RAG spine smoke
864c700 feat: Forge V1 feature implementations (phase 1 fan-out)
8a9f3b5 checkpoint: phase 1 partial build + 39 slices (unverified, session-limit cutoff)
4cbf33d feat(phase0): 0.6 deploy+ci+test-infra
6cf1e6f feat(phase0): 0.5 web skeleton (apps/web)
371127b feat(phase0): 0.4 api skeleton (apps/api)
6c2316a feat(phase0): 0.3 contracts (packages/contracts)
189f534 feat(phase0): 0.2 data-model (packages/db)
1203b97 feat(phase0): 0.1 workspace+tooling
2175914 docs: V1 overnight implementation plan
098644f docs: overnight swarm build design + repo init
```

(`docs: morning report` is committed on top of the above as Phase 3.)
