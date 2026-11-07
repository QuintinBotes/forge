# J + K — Context, memory and automation lines (lore++)

**Date:** 2026-11-07
**Status:** J: Approved, implementation-ready (decisions in §2.5). K: DRAFT — scope and interfaces only.
**Depends on:** J: H1, I. K: G, J.
**Blocks:** programme acceptance (unified-factory-programme §5 items 4–5)
**Decisions:** ADR-0003, ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0014, ADR-0017, ADR-0018

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

### 2.5 Decisions (settled 2026-11-07)

**Lexical leg — real BM25 on Postgres, no new dependency (ADR-0017).** Today `Bm25Store` ranks with
`ts_rank` on Postgres (not BM25; lore has the same flaw) and with real Okapi BM25 only on SQLite.
J replaces the Postgres path with *candidate-then-rescore*:

1. Candidates: chunks matching an **OR** tsquery of the query terms (`to_tsquery('simple', t1 | t2 …)`,
   built from the existing `tokenize`), ordered by `ts_rank_cd`, limit 200, workspace/scope filtered.
   OR semantics keeps BM25's recall; `plainto_tsquery` (AND) loses it. The stored `tsv` is
   `to_tsvector('simple', ' '.join(tokenize(content)))`, so candidates and the rescorer see the same
   tokens: the `english` config kept `self.retry` or `v1.2.3` as one lexeme, stemmed, and dropped
   stopwords, so Postgres missed chunks SQLite found (review of the first pass, 2026-11-07).
   Queries drop one shared English stopword list on both dialects; a stopword-only query returns
   nothing on either.
2. Rescore the candidates with exact Okapi BM25 (k1, b as today) using corpus statistics kept
   incrementally at index time in `retrieval_term_stats(workspace_id, term, df)` and
   `retrieval_chunk.token_count`, plus a per-workspace `chunk_count`/`total_tokens` row. Upserts and
   deletes adjust the counts in the same transaction as the chunk write.
3. One shared `bm25_score(query_terms, doc_terms, df, n_docs, avg_len)` pure function serves both
   dialects, so SQLite and Postgres rank identically on the same corpus (tested).

Migration slug: `retrieval_bm25_stats` (next free number, ADR-0009), with a backfill over existing chunks.

**Vector leg and reranker — configured or explicitly absent, never faked (ADR-0018).**

- Embedder: `HttpEmbeddingClient` (OpenAI-compatible) from `FORGE_EMBEDDING_MODEL`,
  `FORGE_EMBEDDING_BASE_URL` (SSRF-validated with the existing `url_validator` seam),
  `FORGE_EMBEDDING_DIMENSION` (must equal the `retrieval_chunk.embedding` column's
  `EMBEDDING_DIM`, else startup refuses), and the key from `FORGE_EMBEDDING_API_KEY`.
  `SentenceTransformerEmbeddingClient` remains selectable with `FORGE_EMBEDDING_PROVIDER=local`.
- With no embedder configured: search runs **BM25-only** and every response carries
  `legs: ["bm25"]` and `vector_leg: "unconfigured"`; indexing stores chunks without embeddings; any
  route that requires vectors returns 503. `DeterministicEmbeddingClient` is constructed only by
  tests (a repository test forbids it outside test code, like `example.invalid`).
- Reranker: default `FORGE_RERANK_PROVIDER` becomes `none` (no rerank, reported as
  `rerank: "off"`); `fixture` is test-only; `jina`/`cohere`/`selfhosted` unchanged.

**`context_refs`.** A JSON list on `agent_run` of `{"kind": "fact"|"chunk"|"spec"|"memory", "id": str}`,
written by `assemble_context` when called with an `agent_run_id`, and copied into the run result
artifacts. Migration slug `agent_run_context_refs`.

**Memory and outcome feedback.**

- `world_fact_feedback(fact_id, run_id, outcome ∈ {reinforced, weakened}, created_at)` — append-only.
- Effective confidence (computed on read, never stored):
  `min(1, confidence × 0.5^(age_days / half_life_days) × 1.1^reinforced × 0.8^weakened)` with
  `half_life_days = 30` for `layer = memory` and no decay for other layers.
- Trigger: the GitHub `pull_request` webhook already handled by the integration router. A closed PR
  whose head branch is `forge/run-<hex>` (the finalizer's naming) resolves to its `agent_run`;
  `merged = true` writes `reinforced` for every fact/memory ref in `context_refs`, otherwise
  `weakened`. Idempotent per `(run_id, fact_id)`.

**Not now.** `lore_*` MCP aliases: deferred until a lore user asks. The benchmark port (§2.4) follows
J's implementation as its own task.

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

## 4. Interfaces to settle before K is implementation-ready

- The DSL station kinds (`agent`, `service`, `human`) and their persistence in `workflow-engine`.
- The `workflow_template` installer in `forge_marketplace`.

(J's open interfaces are settled in §2.5.)

## 5. Out of scope

Running lore, its `floor` engine or its planning-editor packages (unlicensed; ADR-0010). Kubernetes `Agent` CRDs.
