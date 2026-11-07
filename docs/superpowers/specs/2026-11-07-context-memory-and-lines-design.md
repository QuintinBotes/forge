# J + K — Context, memory and automation lines (lore++)

**Date:** 2026-11-07
**Status:** DRAFT — scope and interfaces only. Each becomes implementation-ready after H1 and I land.
**Depends on:** J: H1, I. K: G, J.
**Blocks:** programme acceptance (unified-factory-programme §5 items 4–5)
**Decisions:** ADR-0003, ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0014

---

## 1. Purpose

lore does two jobs: it gives Claude Code org-wide **context and memory** over MCP, and it
runs **automation lines** that turn tickets into PRs. This spec rebuilds both on forge, to the "better
means" bars in the programme scoreboard (§4 of `2026-11-07-unified-factory-programme.md`).

## 2. J — Context and memory

### 2.1 Context assembly

`assemble_context(task, budget_tokens, template)` returns one token-budgeted bundle:

1. World-model facts about the entities named in the task, holding *now*, with evidence and conflict
   markers showing both sides.
2. Passages from `forge_knowledge` (BM25 + vector + RRF + reranker), **whole requirements never
   truncated** (a requirement block is atomic; it is dropped, not cut).
3. Specs and ADRs with their **status** (draft / approved / superseded).
4. Relevant memories (`layer = memory`) by effective confidence.
5. Repo instruction files (AGENTS.md, CLAUDE.md, `.cursor/rules`) read per tervin's instruction
   readership table.

Templates: `default`, `review`, `implementation`, `research`. Exposed as MCP tool
`assemble_context` and REST `POST /context/assemble`. Every bundle records the ids it contained
(`context_refs`) on the run that requested it.

### 2.2 Memory lifecycle

Memories are world facts with `layer = memory`:

- Written only through proposals or by a person (ADR-0005). Passive capture (session summaries,
  task lessons) produces proposals, opt-in per workspace.
- Expiry is `valid_to`. Effective confidence = `confidence × 0.5^(age / half_life)`, computed on
  read; no cron mutates rows.
- **Outcome feedback that works** (lore #2385 is dead code): when a run's PR merges, each fact in
  its `context_refs` gets a `reinforced` event; when rejected, `weakened`. Half-life is a function of
  those events. The link is written by the production finalizer and covered by an end-to-end test.

### 2.3 Wider index

Ingest issues, PRs (title, body, review threads), commits (message + touched paths), CI failure
summaries, and repo instruction files, through existing PM/GitHub adapters into `forge_knowledge`;
named entities and decisions from them into `forge_world`. A code-symbol index maps symbol → file
(tree-sitter chunking already exists). Idempotent by content hash.

### 2.4 Retrieval quality is measured

- Production defaults replace `DeterministicEmbeddingClient` and the fixture reranker; hash and
  fixture clients become test-only (ADR-0007).
- The BM25 leg is real BM25 (pg_search/ParadeDB or an in-repo BM25 over `tsvector` statistics —
  chosen after licence review); a missing leg is an error, never silent.
- A port of lore's 60-question agent benchmark runs in `forge_eval`. **Bar: ≥ 0.86** (utopia's
  score on lore's set), hallucination ≤ 7%, p50 ≤ 17.8k tokens. A regression fails CI.

## 3. K — Automation lines

Lines are forge workflow-DSL definitions with `agent`, `service` and `human` stations, distributed
as signed `workflow_template` marketplace packages (the kind is reserved today).

| Line | Lore source | Forge bar |
|---|---|---|
| Implementation loop | `implementation-loop.yaml` (17 stations) | Parallel tickets with dependency-aware scheduling; failing tests first; lint and tests before push; hard per-run budget; real PR via G2; never merges |
| Code review | `code-review*.yaml` | Fail-closed check run; severity with audit; suggestions verified to apply |
| Triage | `issue-triage.yaml` | Reproduce in sandbox; duplicate detection via world model; Jira/Linear through PM adapters; incidents and alerts as inputs |
| Spec → tickets | `feature-planning.yaml` | From Spec Studio approvals, with stable `R`/`A` ids |
| Spec upkeep | `spec-upkeep.yaml` (weekly) | Continuous on push: a spec approval records a world decision; drift is H1's `drift()` |
| Auto-merge | `auto-merge.yaml` | Shadow-only until programme A measures agreement; **fails closed** when any dependency is unreachable (lore #2374 fails open) |
| Merge bookkeeping | `merge.yaml` | Closes tickets, reinforces context (J §2.2), records the outcome |
| Onboarding | `onboard.yaml` | Idempotent with a verified checklist |
| Digest / changelog | `daily-digest.yaml`, `weekly-changelog.yaml` | By audience; Slack and email; agent-run aware |

Measured bars on a dogfood repository over a comparable batch: merged-PR yield above lore's 43% and
all-in cost per merged PR below lore's ~$105 (self-reported by lore; measured by programme C/D).

## 4. Interfaces to settle before implementation-ready

- The DSL station kinds (`agent`, `service`, `human`) and their persistence in `workflow-engine`.
- The `workflow_template` installer in `forge_marketplace`.
- The `context_refs` column on `agent_run` (migration: next free number, ADR-0009).
- Whether to ship `lore_*` MCP aliases for existing lore users (cheap; decided by demand).

## 5. Out of scope

Running lore, its `floor` engine or its planning-editor packages (unlicensed; ADR-0010). Kubernetes `Agent` CRDs.
