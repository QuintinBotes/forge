# Unified Factory Programme — sub-projects G–L

**Date:** 2026-11-07
**Status:** Approved (decomposition and sequencing)
**Extends:** `2026-09-19-software-factory-programme.md` (A–F). Its six principles bind here too.
**Architecture:** `docs/architecture/unified-factory.md`
**Decisions:** `docs/architecture/adr/0001`–`0020`
**Research:** `research_notes/Forge unified software factory plan/` on the operator's machine (not
tracked); summarised in §2.

---

## 1. Goal

Make forge the one software factory by absorbing three neighbouring systems:

| System | What it is | What forge does with it |
|---|---|---|
| **lore** (Apache-2.0, TS, K8s) | "The software agent factory": org context and memory for Claude Code over 42 MCP tools, plus 14 YAML automation lines (ticket → PR, review, triage, auto-merge, digests) | **Beats it.** Every lore capability gets a forge equivalent that is measurably better (§4). Nothing is embedded. |
| **deeplethe/utopia** (Apache-2.0, Rust) | A self-hosted "enterprise world model": a bitemporal, evidence-backed knowledge graph where agents propose and people decide | **Rebuilds it natively, plus more.** A clean-room Python re-implementation on forge's Postgres, with the parts utopia never built (decision ledger, drift, unique-constrained ledger, `at`-aware reads, unified review). |
| **QuintinBotes/tervin** (MIT, Rust/Tauri) | A macOS desktop terminal that drives real agent CLIs behind permission gates and keeps a hash-chained ledger | **Becomes forge's local face.** Forge stays the system of record; tervin consumes it through MCP, a JSON CLI, a run event stream and an ACP shim. |

## 2. Why — the evidence in one table

| Finding | Source | Consequence |
|---|---|---|
| Forge's main agent paths build `AgentRunner` with an **empty tool registry**; nothing enqueues `forge.agent.run`; the Temporal pipeline's agent/PR activities default to success with `pr_url=https://example.invalid/pr/1`. | `forge_backend.md` Q3 | A factory that cannot change code is not a factory. **G is a prerequisite** for every automation line. |
| Lore scored **0.57** on its own 60-question agent benchmark against **0.86** for utopia, and already switched its read default to utopia. | lore `context-store-agent-questions.md` | The fastest way to beat lore's core (context for agents) is a better world model. **H precedes J.** |
| Lore's lost points: issues/PRs/commits not searchable (11.3 of 25.5), truncated requirements, no code-symbol map; "BM25" is `ts_rank`; PR-outcome feedback is dead (#2385); memories silently dropped ~60 days. | `lore.md` capability table | J's acceptance criteria are written against exactly these failures. |
| Utopia never built Actions, a tool-call gate, decision replay or scenarios; its dedup has a known race; full text is "now only". | `utopia.md` rebuild blueprint | These are H's "++". |
| Forge has only an MCP **client** gateway; its MCP server is a test double. Tervin accepts tools only as a process, a JSON CLI, MCP, or `.tervin/` data. | `forge_backend.md`, `tervin.md` | **I (forge MCP server)** serves lore-style context to Claude Code *and* is tervin's first seam. |
| Both forge and tervin v3 plan to own board, specs, coordinator and approvals. | `tervin.md` overlap | ADR-0002 settles it: forge is the core, tervin the face. |
| Most 2026 factories stop at "PR merged"; few close incident → fix PR, ship tamper-evident provenance, or measure first-attempt merge rate. | `software_factory_landscape.md` | Folded into J/K acceptance criteria and programme C (outer loop). |

## 3. Sub-projects

| ID | Name | Depends on | Status of its spec | Delivers |
|----|------|-----------|--------------------|----------|
| **G** | Agents that act | F | Approved, implementation-ready | A policy-gated tool belt on every agent path; real PR opening; canned-success fallbacks removed |
| **H** | World model (utopia++) | — | Approved, implementation-ready (H1); H2–H4 at interface depth | `packages/world-model` (`forge_world`), migration, `/world` API |
| **I** | Forge MCP server | H1 | Approved, implementation-ready | `POST {api_prefix}/mcp` Streamable-HTTP JSON-RPC server, read-only by default |
| **J** | Context and memory (lore++ part 1) | H1, I | DRAFT — scope and interfaces | `assemble_context`, memory lifecycle, wider index, eval gate |
| **K** | Automation lines (lore++ part 2) | G, J | Approved, implementation-ready (ADR-0020; P0 first) | Ticket → PR loop, review, triage, merge bookkeeping, digests, on `workflow-engine` |
| **L** | Tervin bridge | I, G | DRAFT — scope and interfaces | Run event SSE stream, `forge --json`, ACP stdio shim, token-by-env-var auth |
| **M** | Bring your own tracker | G | Approved (M1, M2 ready; M3 after G1d) | Tracker owns work items (ADR-0019); GitHub Issues adapter; tracker-wins write-back; issue-driven dispatch |

Ordering: **G** and **H1** are independent and can run in parallel. **I** needs H1's read surface.
**J** needs H1 and I. **K** needs G and J. **L** needs I and G's run events.

Relation to A–F: none of G–L changes a gate's decision, so principle 1 (shadow first) applies only
where a sub-project adds automation that decides (H's governance gate ships shadow-only; K's
auto-merge is shadow-only until A's agreement measurement exists). G–L reuse A's changeset
identity when A lands; until then they key on `workflow_run_id`.

## 4. "Better than lore" — the scoreboard

Each row is a lore capability, the forge home, and the measurable bar that counts as better. "Bar"
items become acceptance criteria in the owning spec.

| Lore capability | Forge home | Better means (bar) | Sub-project |
|---|---|---|---|
| Context assembly (`lore_assemble_context`) | `forge_world` + `forge_knowledge` | Beats 0.86 on a forge-run port of the 60-question agent benchmark; whole requirements never truncated; document status shown; conflicts flagged with both sides | J |
| Hybrid search | `forge_knowledge` retriever | Real BM25 (not `ts_rank`) + vector + RRF + reranker; recall scored in CI; a missing leg is an error, never silent (lore #2386) | J |
| Ingest | `forge_knowledge.sync` + `forge_world.ingest` | Docs, code, issues, PRs, commits, CLAUDE.md/AGENTS.md; code-symbol index; idempotent by content hash | J |
| Key-value memory | `forge_world` (`layer=memory`) | One store, migrations always applied, per-workspace RBAC, provenance and edit history | J |
| Temporal facts | `forge_world` ledger | Bitemporal `at`/`as_of` reads (lore only has an issue, #3889); human review of extracted facts | **H1** |
| Knowledge graph | `forge_world` | One store (Postgres), ontology, multi-hop with proof trees | **H1** |
| Utopia mirror | — | Not needed: the world model is native | **H1** |
| PR-outcome feedback | `forge_world` + run outcomes | Retrieved context of every run is linked to its outcome; the link is written in production code and tested end to end | J |
| Passive capture | `forge_world` proposals | Captured lessons are proposals, never auto-org-wide; per-workspace opt-in | J |
| Secret redaction | existing `forge_knowledge.redaction` | Already present; audited | — |
| Spec traceability graph | `forge_spec` traceability (R→A→T→test) | Stable statement IDs (lore #4124), paginated reads, Postgres | J |
| Spec impact / explain | `forge_spec` + `forge_world` | Impact shown in the PR gate | K |
| Spec upkeep / drift | `forge_world` decisions + `forge_spec` | Continuous on push, not weekly | K |
| Implementation loop | `workflow-engine` + G | Parallel tickets, hard per-run budget, lint before push, real PRs | K |
| PR code review | `workflow-engine` + red-team gate | Fail-closed; severity with audit trail | K |
| Auto-merge | approvals + policy | Fail-closed (lore #2374 fails open); shadow until measured agreement | K |
| Issue triage | `workflow-engine` + incidents | Duplicate detection; any connected tracker (Linear, Jira, GitHub Issues, …) owns the item (ADR-0019) | K, M |
| Feature planning | `forge_spec` Spec Studio | Already real-time co-editing; add approvals and live traceability | — |
| Repo onboarding | K | Idempotent with verified checklist | K |
| Digests / changelog | K | Audience-specific; Slack and email | K |
| Event bus / scheduler | `forge_automation` + Celery beat | Already present | — |
| Assembly-line engine | `workflow-engine` (Temporal + DSL) | Native; lore depends on its unlicensed external `floor` engine | K |
| Agent/skill definitions | `forge_skill` + marketplace | Versioned with actor audit (lore #2266) | — |
| Claude Code plugin | **I** + a forge plugin manifest | MCP server reached by URL + token; no laptop build (lore #1122) | I |
| Local task runner | tervin (L) | Runs locally behind tervin's gates, audited centrally | L |
| CI helpers | G tool belt | `ci_failures`, `ci_job_log`, `pr_status` tools | G |
| Run observability | existing observability + L SSE | OTel-native, linked to cost/audit/incidents | L |
| Spend | existing cost router | Hard budgets per run (lore has none; ≥$2,415/weekend) | G |
| LLM abstraction | existing providers + router | Already present | — |
| AuthN/AuthZ | existing RBAC/SSO/SCIM | Per-user tokens; no full-access shared token | — |
| Audit | existing audit + attestations | Already tamper-evident | — |
| Incident ingest | existing incidents | Triggers triage line | K |
| Context evals | existing benchmarks | Continuous, gated on regression | J |

## 5. Programme-level acceptance

The programme is done when:

1. An agent run started from the API changes code in a sandbox and opens a real PR, with every tool
   call policy-gated and audited (G).
2. A document ingested into a workspace yields evidence-backed facts that can be read at any `at`
   and `as_of`; an agent's `remember` creates a proposal that only a human can accept (H1).
3. Claude Code, configured with only a URL and a token, can query forge's world model and knowledge
   over MCP (I).
4. Forge beats 0.86 on the ported agent-question benchmark (J).
5. A labelled ticket travels to a reviewed, mergeable PR without a human touching code, with a hard
   budget, and fails closed when any dependency is down (K).
6. Tervin lists forge's specs, approvals and world-model facts through MCP and shows a forge run
   live through the event stream (L).

## 6. Amendments to the 2026-09-19 assessment

- Capabilities 4 (Orchestration) and 5 (Execution) are re-scored from **Strong** to **Partial**:
  the architecture is complete, but on the main paths agents have no tools and the Temporal
  activities default to success (§2). G restores them.
- Capability 3 (Knowledge) stays Strong for passages and is **Partial** for facts: there is no
  ledger of what is true, since when, according to whom. H closes it.

## 7. Migration ledger

Numbers are assigned at merge time (ADR-0009).

| Number | Slug | Owner | State |
|---|---|---|---|
| 0041 | `world_model` | H1 | this branch |
| next free | `risk_classification` | A | spec approved, not implemented |
| next free | `agent_run_context_refs` | J | draft |

## 8. What this programme deliberately does not do

- Embed lore, utopia or tervin code or services. Everything is re-implemented (ADR-0003, ADR-0008,
  ADR-0010).
- Port tervin's terminal, PTY or macOS integration into forge. They stay in tervin.
- Replace forge's Temporal/workflow engine, providers, RBAC or audit. G–L extend them.
- Introduce Dgraph, Tantivy, or any store beside Postgres (ADR-0004).
