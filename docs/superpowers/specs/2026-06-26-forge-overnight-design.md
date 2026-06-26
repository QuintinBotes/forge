# Forge — Overnight Swarm Build: Design

**Date:** 2026-06-26
**Status:** Approved
**Source of truth:** `docs/FORGE_SPEC.md` (product spec), `docs/forge-research-report.md` (rationale)

This document defines what an overnight autonomous multi-agent swarm builds for Forge,
how it is orchestrated so parallel work integrates, and the quality/safety gates it runs under.

---

## 1. Decisions (locked)

| Decision | Choice | Consequence |
|---|---|---|
| Overnight ambition | **Maximize V1 breadth** | Attempt every Phase-1 feature concurrently |
| Deep spine | **Knowledge / RAG** | Driven to genuinely-working, with eval numbers |
| Quality posture | **TDD + green gates** | No unit "done" without lint+types+tests passing; park, don't fake |
| Swarm scale | **Max overnight burn** | Large fan-out, 3-vote adversarial verify, bounded loop-until-dry |
| Token ceiling | None set | Bounded-but-aggressive (capped loop rounds, not infinite) |
| Provider posture | BYOK abstraction; Anthropic reference impl | No live external calls overnight (interfaces + fixtures) |

### Honest scope reality
The full `FORGE_SPEC.md` is a multi-month, multi-service platform across V1/V2/V3.
The overnight swarm does **not** produce a finished, production-trustworthy Forge. It produces:
a git-initialized monorepo with the full data model, all 12 packages + 4 apps scaffolded with
**real, tested logic across V1 breadth**, the **Knowledge/RAG pipeline working end-to-end**,
`docker compose` building, CI configured, and a morning report stating exactly what is real,
what is parked, and what is next.

---

## 2. The integration trick: pre-partition + frozen contracts

The failure mode of breadth-in-parallel is pieces that don't integrate. Mitigation:

1. **One sequential architect phase** creates the entire shared substrate *first*:
   the data model, every package's public interface (Pydantic/Protocol), and **all integration
   points pre-stubbed** — the FastAPI app already registers every router; the uv + pnpm
   workspaces already list every member; `docker-compose.yml` is already complete.
2. **After that, every parallel agent edits only files inside its own package directory.**
   Concurrent writes never collide. Shared-file edits (router registration, workspace members)
   were already done in Phase 0, so Phase 1 is conflict-free.
3. **Git commits happen at phase barriers** by a single committer, never concurrently from
   parallel agents (avoids `.git` index races). Worktree isolation is therefore **not** needed —
   boundaries are pre-partitioned — which saves per-agent worktree cost under max burn.
4. **Phase 1 unit tests are isolated** (no shared live services); DB/integration tests that need
   Postgres run serially in Phase 2 against compose.

---

## 3. Four phases (one autonomous Workflow, background)

### Phase 0 — Architect (sequential)
Shared substrate, committed before any fan-out:
- `git init` (done), Apache-2.0 `LICENSE`, root `README.md`
- `uv` workspace + `pnpm` workspace; `ruff` + `mypy`/`pyright` config; root `Makefile`; `.env.example`
- **Full SQLAlchemy 2.x data model** mirroring the spec's Core Data Model
  (Workspace, User, APIKey, RepositoryConnection, MCPConnection, PolicyProfile, SkillProfile,
  KnowledgeSource, RetrievalChunk, Project, Constitution, Epic, SpecDocument, Task, Incident,
  Sprint, Milestone, WorkflowRun, AgentRun, ApprovalRequest, SubAgentRun) + Alembic baseline
- **Interface contracts** (Pydantic models / `Protocol`s) for all 12 packages
- FastAPI skeleton: settings (Pydantic Settings), DB session DI, health, **every router pre-registered as a stub**
- Complete `deploy/docker-compose.yml` (db+pgvector, redis, minio, api, worker, mcp-gateway, web, caddy, autoheal) + `docker-compose.dev.yml`
- GitHub Actions CI: lint, type-check, test, build (Python + web)
- pytest infrastructure with isolated fixtures; `evaluation/` golden-set runner scaffold
- Next.js app skeleton (TS, Tailwind, shadcn/ui, TanStack Query) with API client stub

### Phase 1 — Parallel fan-out (TDD-gated)
Each unit pipeline: **write tests → implement → verify (ruff + types + pytest) → 3-vote adversarial review → self-repair → commit at barrier.**

- **Knowledge/RAG (deepest):** AST + markdown chunking; BYOK embedding client; pgvector store;
  Postgres BM25 full-text; RRF fusion (k=60); Jina reranker client; chunk-type weights;
  `/search` API with source attribution; full + incremental sync modes; **golden retrieval eval (RAGAS-style)**.
- **Board:** domain (epic/task/sprint/incident; dependency graph + cycle detection); FastAPI CRUD/filters/bulk;
  Next.js UI (list, kanban, Cmd+K command palette, optimistic updates, WS realtime).
- **Spec engine:** SDD lifecycle (constitution→specify→clarify→plan→tasks→validate); `manifest.yaml`;
  gates; requirement→task→test traceability.
- **Agent runtime:** LangGraph StateGraph single-agent loop; tool registry; git-worktree sandbox;
  AGENTS.md loading; structured objectives; confidence/handoff.
- **Workflow engine:** Postgres FSM; default feature workflow states + transitions; retry/escalation policy; workflow DSL parser.
- **Policy SDK:** `.forge/policy.yaml` loader + permission evaluation (write rules, allowed/restricted actions).
- **Skill SDK:** skill-profile loader (backend-tdd, frontend-ui, incident-response, spec-analyst) + behavior injection.
- **MCP SDK + gateway:** MCP client, connection model, query-through, audit, security (read-only default, RFC 8707 token binding).
- **Integration SDK:** GitHub App (repo sync, PR, reviews, CI webhooks) + Slack notifications — against interfaces + fixtures.
- **Observability:** run-trace viewer data + immutable audit log; OTel hooks.
- **Auth & secrets:** Better Auth integration; encrypted Postgres BYOK vault; RBAC roles.
- **Evaluation:** 30+ golden task inputs + harness + metrics.

### Phase 2 — Integration + adversarial verification
Wire real implementations into pre-stubbed points; `docker compose build`; migrations apply;
full suite green; **loop-until-dry bug hunt** (bounded) + completeness critics
("which V1 checkbox is unaddressed?") + security pass; end-to-end smoke of the RAG spine on a sample repo.

### Phase 3 — Morning report
`MORNING_REPORT.md`: V1 checklist status (done/tested vs parked), how to run it, test/coverage
results, known gaps, recommended next steps, commit log.

---

## 4. Non-negotiable autonomy & safety rules

- **Park, don't fake.** Blocked units get a clear `# PARKED:` marker + log entry — never a
  fake-passing test or swallowed error. Every parked item appears in the morning report.
- **No destructive / outward actions.** No `git push` to any remote; no network deploys; no real
  external API calls (GitHub/Slack/model providers built against interfaces + recorded fixtures).
  Everything stays local in this repo.
- **Green gate is real.** "Done" for a unit means lint + types + its tests actually ran and passed —
  verified from command output, not asserted.
- **Bounded loops.** Loop-until-dry and self-repair have capped rounds (runaway backstop).

---

## 5. What success looks like by morning

1. `git log` shows a coherent commit history (foundation → features → integration → report).
2. `docker compose -f deploy/docker-compose.yml build` succeeds.
3. The Knowledge/RAG pipeline runs end-to-end on a sample repo and reports eval numbers.
4. `MORNING_REPORT.md` accurately distinguishes real-and-tested from parked.
5. The test suite is green for everything marked "done".

---

## 6. Orchestration mechanics

- A single self-contained **Workflow** script (persisted to a file; resumable) runs in the
  background overnight. The phases above map to `phase()` groups; Phase 1 uses `pipeline()`/`parallel()`
  fan-out with `schema`-validated structured outputs; max-burn quality patterns (3-vote verify,
  loop-until-dry, completeness critics) apply in Phases 1–2.
- Concurrency auto-caps at `min(16, cores-2)`; total agents bounded well under the 1000 cap.
- On completion a task-notification fires; the morning report is surfaced to the user.
