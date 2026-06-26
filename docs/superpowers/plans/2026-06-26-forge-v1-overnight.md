# Forge V1 Overnight Build — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Executed overnight by the Workflow swarm (one subagent per task, verify + adversarial review between).

**Goal:** Stand up the Forge monorepo with the full data model, all 12 packages + 4 apps scaffolded with real tested logic across V1 breadth, and the Knowledge/RAG pipeline working end-to-end with eval numbers.

**Architecture:** A sequential architect phase freezes the shared substrate (data model, every package's public interface, all integration points pre-stubbed). Then parallel agents implement each package against frozen contracts, editing only files inside their own package directory. Git commits happen at phase barriers. TDD throughout; green gate (ruff + types + pytest) required before any unit is "done".

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, uv, Ruff, pytest, LangGraph, pgvector, Postgres, Redis, Celery; Next.js 15 + TypeScript + Tailwind + shadcn/ui + TanStack Query/Table; Docker Compose; Caddy.

## Global Constraints

- Python 3.12; `uv` for all Python deps; `ruff` for lint+format; `mypy`/`pyright` for types.
- Node 22; `pnpm` workspace; Next.js (app router), TypeScript strict.
- License: **Apache-2.0**. Product name: **Forge**. CLI prefix: `forge`.
- Every package is an installable member of the uv workspace under `packages/`; apps under `apps/`.
- TDD mandatory: write failing test → implement → green. No unit "done" without `ruff check`, type check, and `pytest` all passing on its scope.
- **Park, don't fake:** blocked work gets `# PARKED: <reason>` + a line in `MORNING_REPORT.md`. Never a fake-passing test or swallowed error.
- **No outward actions:** no `git push`, no network deploys, no real external API calls. GitHub/Slack/model/reranker clients are built against interfaces with recorded fixtures.
- Retrieval is always hybrid (semantic + keyword + RRF + rerank). RRF: `score(d)=Σ 1/(k+rank_i(d))`, `k=60`.
- MCP connections default `allow_write: false`; token binding per RFC 8707; audit every call.
- Secrets encrypted at rest; redacted from logs/traces/retrieval results.
- Commits at phase barriers only; conventional-commit messages; co-author trailer.

---

## File Structure (locked in Phase 0)

```
forge/
├── pyproject.toml                 # uv workspace root
├── Makefile                       # setup, dev, test, lint, migrate
├── LICENSE                        # Apache-2.0
├── apps/
│   ├── api/forge_api/             # FastAPI app: main, settings, db, deps, routers/*
│   ├── worker/forge_worker/       # Celery app + tasks (indexer, syncer, agent-runner)
│   ├── mcp-gateway/forge_mcp_gateway/  # MCP client manager service
│   └── web/                       # Next.js frontend (pnpm)
├── packages/
│   ├── contracts/forge_contracts/ # Pydantic DTOs + Protocols shared by all (Phase 0)
│   ├── db/forge_db/               # SQLAlchemy models + Alembic (Phase 0)
│   ├── workflow-engine/forge_workflow/
│   ├── agent-runtime/forge_agent/
│   ├── multi-agent-coordinator/forge_coordinator/
│   ├── spec-engine/forge_spec/
│   ├── board-core/forge_board/
│   ├── knowledge-core/forge_knowledge/
│   ├── integration-sdk/forge_integrations/
│   ├── mcp-sdk/forge_mcp/
│   ├── policy-sdk/forge_policy/
│   ├── skill-sdk/forge_skill/
│   ├── evaluation/forge_eval/
│   └── ui-kit/                    # shared React components (pnpm)
├── deploy/                        # docker-compose*.yml, caddy, scripts
├── examples/                      # policies, skills, workflows, mcp-connectors, specs
└── docs/self-hosting/             # quickstart, docker-compose, backup, restore, ...
```

---

## PHASE 0 — Architect (sequential; one coherent agent; committed before fan-out)

### Task 0.1: Workspace + tooling skeleton
**Files:** Create `pyproject.toml` (uv workspace listing every `packages/*` + `apps/*` member), `ruff.toml`, `mypy.ini`, `Makefile`, `.env.example`, `LICENSE` (Apache-2.0), `README.md`, `pnpm-workspace.yaml`.
**Produces:** an installable workspace; `uv sync` resolves; `make lint` runs.
- [ ] Each package gets `pyproject.toml` + `forge_*/__init__.py` + empty `py.typed`.
- [ ] `Makefile` targets: `setup`, `dev`, `test`, `lint`, `typecheck`, `migrate`, `seed`.
- [ ] Gate: `uv sync` succeeds; `ruff check .` passes on empty skeleton.

### Task 0.2: Core data model (`packages/db`)
**Files:** Create `forge_db/models/*.py`, `forge_db/base.py` (DeclarativeBase, naming convention), `forge_db/session.py`, `alembic.ini`, `migrations/env.py`, baseline migration.
**Interfaces — Produces (every later task consumes these):** SQLAlchemy models for the full spec data model:
`Workspace, User, APIKey, RepositoryConnection, MCPConnection, PolicyProfile, SkillProfile, KnowledgeSource, RetrievalChunk, Project, Constitution, Epic, SpecDocument, Task, Incident, Sprint, Milestone, WorkflowRun, AgentRun, ApprovalRequest, SubAgentRun`. `RetrievalChunk` has `embedding Vector(N)` (pgvector) + `tsv` (tsvector) columns. UUID PKs, `created_at/updated_at`, workspace scoping FK on tenant tables.
- [ ] Write tests: model imports, a metadata.create_all on SQLite (non-vector subset) for unit tests; pgvector columns guarded for Postgres.
- [ ] Alembic baseline migration generates and applies against a Postgres test container.
- [ ] Gate: `pytest packages/db`, types, lint green.

### Task 0.3: Shared contracts (`packages/contracts`)
**Files:** Create `forge_contracts/*.py` — Pydantic v2 DTOs + `typing.Protocol` interfaces for every package's public API.
**Produces — the frozen contracts (verbatim signatures other tasks must match):**
- `KnowledgeStore` Protocol: `index(source_id, chunks: list[Chunk]) -> IndexResult`; `search(query: str, scope: KnowledgeScope, k: int = 10) -> list[RetrievedChunk]`.
- `Retriever` Protocol: `semantic(query, scope, k) -> list[Ranked]`; `keyword(query, scope, k) -> list[Ranked]`; `fuse(rankings: list[list[Ranked]], k: int = 60) -> list[Ranked]`; `rerank(query, candidates, top_n) -> list[RetrievedChunk]`.
- `BoardService` Protocol: CRUD for `Epic/Task/Sprint/Milestone/Incident`; `set_status`, `bulk_update`, `dependency_add` (raises `CycleError`).
- `SpecEngine` Protocol: `constitution_init`, `spec_create`, `spec_clarify`, `spec_plan`, `spec_tasks`, `validate(task_id) -> ValidationReport`; manifest read/write.
- `AgentRuntime` Protocol: `run(objective: AgentObjective) -> AgentRunResult` (LangGraph graph; emits `Step[]`).
- `WorkflowEngine` Protocol: `start(task_id) -> WorkflowRun`; `transition(run_id, event) -> State`; FSM definition loader.
- `PolicyEvaluator` Protocol: `load(repo_root) -> Policy`; `evaluate(action: ToolCall, policy: Policy) -> Decision`.
- `SkillProfileRegistry` Protocol: `get(name) -> SkillProfile`; `inject(profile, context) -> AgentObjective`.
- `MCPClient` Protocol: `connect(conn: MCPConnection)`; `list_resources`, `read_resource`, `call_tool` (audited; read-only default).
- `PMAdapter` Protocol (per spec) + `IntegrationClient` (GitHub/Slack) Protocols.
- `EmbeddingClient` / `RerankerClient` / `ModelClient` Protocols (BYOK; provider-agnostic).
- DTOs: `Chunk, RetrievedChunk, Ranked, KnowledgeScope, AgentObjective, AgentRunResult, Step, ToolCall, Decision, Policy, SkillProfile, ValidationReport, ApprovalRequest`.
- [ ] Tests: every Protocol importable; DTO round-trips (`model_validate`/`model_dump`); enum values match spec (statuses, kinds, roles).
- [ ] Gate: pytest + types + lint green. **This task's signatures are frozen — Phase 1 builds against them.**

### Task 0.4: API skeleton with all routers pre-registered (`apps/api`)
**Files:** Create `forge_api/main.py`, `settings.py` (Pydantic Settings), `db.py` (session DI), `deps.py` (auth stub returning a test principal), `routers/{health,board,spec,knowledge,workflow,agent,policy,mcp,integration,approval,observability,auth}.py` (each a stub returning 501 with typed response models).
**Produces:** `GET /health` → 200; every feature router mounted so Phase 1 fills in handlers without touching `main.py`.
- [ ] Test: `httpx`+ASGI client hits `/health` → 200; each stub route returns its declared schema shape.
- [ ] Gate: pytest + types + lint green.

### Task 0.5: Frontend skeleton (`apps/web`)
**Files:** Next.js app-router scaffold; Tailwind + shadcn/ui init; TanStack Query provider; typed API client stub; `app/(board)/layout.tsx` shell; Cmd+K palette shell.
- [ ] Test: `pnpm build` succeeds; one component test (Vitest/RTL) for the app shell renders.
- [ ] Gate: `pnpm lint` + `pnpm build` + component test green.

### Task 0.6: Deploy + CI + test infra
**Files:** `deploy/docker-compose.yml` (db+pgvector, redis, minio, api, worker, mcp-gateway, web, caddy, autoheal — pinned images, healthchecks, resource limits, named volumes, segmented networks, non-root), `deploy/docker-compose.dev.yml`, `deploy/caddy/Caddyfile`, `.github/workflows/ci.yml` (lint+type+test+build), root `conftest.py` + pytest config + Postgres test-container fixture, `evaluation` golden-set runner scaffold.
- [ ] Test: `docker compose -f deploy/docker-compose.yml config` validates; CI yaml lints.
- [ ] Gate: compose config valid; pytest collects; lint green. **Commit Phase 0.**

---

## PHASE 1 — Parallel fan-out (one subagent per package; TDD; 3-vote review; commit at barrier)

Each task below is independent and edits only its own `packages/<x>/` (+ its API router handler + worker task, which were pre-stubbed in Phase 0). Each follows: **write failing tests → implement against the Task 0.3 contract → ruff+types+pytest green → 3 adversarial reviewers must not refute → self-repair → done.**

### Task 1.1: Knowledge/RAG — chunking (`packages/knowledge-core`) [SPINE]
**Produces:** `chunk_code(path, src) -> list[Chunk]` (Python AST function/class-level; tree-sitter optional, `ast` fallback), `chunk_markdown(path, src) -> list[Chunk]` (semantic paragraph), chunk-type weights table (README 1.3, policy/AGENTS.md 1.5, spec 1.4, summary 1.2, default 1.0).
- [ ] Tests: a sample `.py` yields one chunk per top-level def/class with correct line spans; markdown splits on headings/paragraphs; weights applied.

### Task 1.2: Knowledge/RAG — embeddings + stores [SPINE]
**Produces:** `EmbeddingClient` reference impl (BYOK; deterministic fake for tests), `PgVectorStore.index/search` (cosine), `Bm25Store.search` (Postgres `tsvector`/`ts_rank`).
- [ ] Tests: index 5 chunks into a Postgres test container; vector search returns nearest by fake embedding; BM25 returns exact-identifier match a vector miss would lose.

### Task 1.3: Knowledge/RAG — RRF fusion + reranker + search service [SPINE]
**Produces:** `fuse(rankings, k=60)` (exact formula), `RerankerClient` (Jina HTTP client + fixture-backed fake), `KnowledgeService.search` chaining semantic+keyword→RRF→rerank→top-k with source attribution; wires `/knowledge/search` API route + worker indexer task.
- [ ] Tests: RRF math verified against hand-computed example; end-to-end search on a tiny indexed repo returns attributed chunks; reranker reorders per fixture.

### Task 1.4: Knowledge/RAG — sync modes + golden retrieval eval [SPINE]
**Produces:** full + incremental (git-diff) sync; `forge_eval` golden retrieval set (≥15 query→expected-chunk pairs) + recall@k / MRR report.
- [ ] Tests: incremental re-index touches only changed files; eval runner prints recall@k and asserts a threshold on the fake pipeline.

### Task 1.5: Board core + API (`packages/board-core`)
**Produces:** domain services for Epic/Task/Sprint/Milestone/Incident; dependency graph with cycle detection (`CycleError`); status workflow rules; bulk ops; saved filters; fills `/board/*` routes.
- [ ] Tests: create/read/update/status; adding a cycle raises `CycleError`; bulk status update; filter query.

### Task 1.6: Board UI (`apps/web`)
**Produces:** List + Kanban views, Cmd+K command palette, optimistic status changes with rollback, WS realtime hook, keyboard nav.
- [ ] Tests: component tests for list render, palette open/run-action, optimistic update + rollback-on-error.

### Task 1.7: Spec engine (`packages/spec-engine`)
**Produces:** SDD lifecycle (`constitution_init`→`spec_create`→`spec_clarify`→`spec_plan`→`spec_tasks`→`validate`); `manifest.yaml` read/write (schema from spec); gate enforcement (no implement without approved spec); requirement→task→test traceability; fills `/spec/*` routes.
- [ ] Tests: full lifecycle produces the folder layout + manifest; gate blocks implement when status≠approved; traceability maps R→A→task.

### Task 1.8: Workflow engine (`packages/workflow-engine`)
**Produces:** Postgres-backed FSM; default feature workflow states + transitions (spec's DSL); retry policy (max 3, exp backoff) + escalation (confidence 0.72); YAML DSL parser; fills `/workflow/*` routes.
- [ ] Tests: happy path created→...→merged; checks_failed with budget retries then escalates to needs_human_input; DSL parses to a validated transition graph.

### Task 1.9: Agent runtime (`packages/agent-runtime`)
**Produces:** LangGraph `StateGraph` single-agent loop (plan→act→observe); tool registry with policy-checked dispatch; git-worktree sandbox; AGENTS.md loader; structured `AgentObjective`→`AgentRunResult` with `Step[]`; confidence + handoff. Model calls via `ModelClient` (fake for tests).
- [ ] Tests: graph runs to completion on a scripted fake model; a restricted tool call is denied by the policy gate; worktree created/cleaned; AGENTS.md content enters context.

### Task 1.10: Policy SDK (`packages/policy-sdk`)
**Produces:** `.forge/policy.yaml` loader (spec schema); `PolicyEvaluator.evaluate(ToolCall) -> Decision` (write_rules allow/deny globs, allowed/restricted actions, deploy rules).
- [ ] Tests: write to `app/**` allowed, to `secrets/**` denied; `deploy_prod` restricted; unknown action defaults deny.

### Task 1.11: Skill SDK (`packages/skill-sdk`)
**Produces:** skill-profile loader + registry (backend-tdd, backend-fast, frontend-ui, incident-response, spec-analyst, security-review, chore-fast); behavior injection into `AgentObjective` (requires_plan, tests-before-impl, coverage floors, forbidden shortcuts).
- [ ] Tests: backend-tdd injects test-first + 80% coverage gate; incident-response forbids deploy_prod; unknown profile raises.

### Task 1.12: MCP SDK + gateway (`packages/mcp-sdk`, `apps/mcp-gateway`)
**Produces:** MCP client (connection model from spec), query-through retrieval, audit log (tool, payload hash, status, latency), security (read-only default, RFC 8707 token binding, namespace scoping); fills `/mcp/*` routes + gateway service.
- [ ] Tests: read-only connection rejects a write tool call; audit entry recorded with redacted secrets; namespace scoping filters resources. (Live transport mocked.)

### Task 1.13: Integration SDK (`packages/integration-sdk`)
**Produces:** GitHub App client (repo sync, open PR, reviews, CI webhook parsing) + Slack notifier — all against interfaces with recorded fixtures; `PMAdapter` Protocol surface.
- [ ] Tests: PR-open builds correct payload (fixture); webhook parser maps CI status; Slack notifier formats approval message. No live calls.

### Task 1.14: Observability + audit (`apps/api` observability router)
**Produces:** immutable audit-log writer (every agent action/tool/MCP call/approval), run-trace assembler (step-level) for the trace viewer, OTel span hooks; fills `/observability/*` routes.
- [ ] Tests: audit entries are append-only; run trace reconstructs ordered steps; secret redaction applied.

### Task 1.15: Auth & secrets (`apps/api` auth router)
**Produces:** Better Auth integration (OAuth + API key), encrypted Postgres BYOK vault (Fernet/libsodium), RBAC (admin/member/viewer/agent-runner), per-workspace isolation.
- [ ] Tests: API key auth round-trip; secret encrypted at rest + decrypt; RBAC denies viewer a write; secrets never appear in serialized output.

### Task 1.16: Evaluation harness + golden task set (`packages/evaluation`)
**Produces:** ≥30 golden task inputs with known-good outputs; harness runner; metrics (spec-requirement satisfaction, retrieval recall, regression gate).
- [ ] Tests: harness loads ≥30 tasks; runs the fake pipeline; emits a scorecard; regression threshold gate works.

### Task 1.17: Examples + self-hosting docs
**Produces:** `examples/policies` (5 repo types), `examples/skills`, `examples/workflows`, `examples/mcp-connectors`, `examples/specs`; `docs/self-hosting/{quickstart,docker-compose,backup,restore,upgrade,security,troubleshooting}.md`.
- [ ] Gate: example YAMLs validate against their loaders (cross-check with Tasks 1.10/1.11/1.8/1.12); docs lint.

**Commit at the Phase 1 barrier (after each track's gate passes).**

---

## PHASE 2 — Integration + adversarial verification

### Task 2.1: Wire-up + build
- [ ] All routers serve real handlers; `apps/worker` tasks call real services; `uv sync` + `pnpm build` clean; `alembic upgrade head` against Postgres container; `docker compose build` succeeds.

### Task 2.2: Full-suite green + coverage
- [ ] `pytest` whole workspace green; coverage reported; any red → systematic-debugging or park with reason.

### Task 2.3: Adversarial sweep (max burn)
- [ ] Loop-until-dry bug hunt (bounded rounds) across changed packages; completeness critics map every V1 checklist item → task/file or mark parked; security pass (secrets, policy bypass, MCP write-default, auth).

### Task 2.4: End-to-end spine smoke
- [ ] Index a sample repo → `/knowledge/search` returns attributed, reranked results → eval recall@k printed. This is the proof the RAG spine works.

---

## PHASE 3 — Morning report

### Task 3.1: `MORNING_REPORT.md`
- [ ] V1 checklist status table (done+tested / partial / parked); how to run (`make setup && make dev`); test + coverage summary; RAG eval numbers; known gaps; ranked next steps; commit log. Commit.

---

## Self-Review — spec coverage

Every Phase-1 V1 checklist item maps to a task: board→1.5/1.6; spec engine→1.7; GitHub App→1.13; repo policy→1.10; knowledge hybrid pipeline→1.1–1.4; single agent + LangGraph→1.9; workflow FSM→1.8; plan→execute→verify→PR→approval→1.8+1.9+1.13+1.14; MCP query-through→1.12; run-trace viewer→1.14+1.6; skill profiles→1.11; golden set + eval→1.16+1.4; local quickstart + compose + self-hosting docs→0.6+1.17; Slack→1.13. Data model→0.2; contracts→0.3; auth/secrets/BYOK→1.15; audit log→1.14. No V1 gap unmapped. (V2/V3 intentionally out of scope.)
