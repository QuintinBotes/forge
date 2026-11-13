# Unified factory architecture

**Date:** 2026-11-07
**Status:** Accepted
**Programme:** `docs/superpowers/specs/2026-11-07-unified-factory-programme.md`
**Decisions:** `docs/architecture/adr/`

Forge is the software factory's **system of record and control plane**. Tervin is its **desktop
face**. The capabilities of lore and utopia are **rebuilt inside forge**. Integration with tervin is
by protocol only.

## 1. Context

```
                ┌────────────────────────── people ──────────────────────────┐
                │  browser (forge web)        desktop (tervin)     IDE agents│
                └───────┬───────────────────────────┬──────────────────┬─────┘
                        │ HTTPS + /ws               │ MCP, forge --json│ MCP
                        │                           │ SSE, /ws, runner │ (Claude Code,
                        ▼                           ▼                  ▼  Codex, Cursor)
┌───────────────────────────────────────── forge (team server) ─────────────────────────────────┐
│                                                                                                │
│  apps/web ──► apps/api ─────────────────────────────────────────────────────────────────────┐  │
│               │ REST routers (board, spec, agent, approvals, policy, world, ...)            │  │
│               │ /mcp  (I: forge's own MCP server — read-only default, writes = proposals)   │  │
│               │ /ws   (realtime: approvals, runs)                                           │  │
│               └──────┬───────────────────────────────────────────────────────────────────┘  │
│                      │ enqueue                                                               │
│  apps/worker ◄───────┘  Celery + Temporal: agent runs (G), lines (K), indexers               │
│      │  tool belt (G) ──► sandbox lattice: worktree < container < gVisor < microVM          │
│      │  finalizer ──► GitHub App (per-run, repo-scoped token) ──► PR + pr gate             │
│      ▼                                                                                       │
│  packages/                                                                                   │
│    world-model (H: utopia++)   knowledge-core (passages)   spec-engine (R→A→T traceability) │
│    agent-runtime (loop, tools) multi-agent-coordinator     workflow-engine (lines)          │
│    approval-sdk  policy-sdk    observability  evaluation   marketplace   ...                │
│      │                                                                                       │
│      ▼                                                                                       │
│  Postgres 16 + pgvector — the only store of record (ADR-0004)                               │
│                                                                                              │
│  apps/mcp-gateway ──► other people's MCP servers (outbound, untrusted)                      │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

Trust directions are separate: `/mcp` on the API serves forge's own data **outward**;
`apps/mcp-gateway` brings other servers' data **inward** and treats it as untrusted.

## 2. Where each absorbed system lands

| Source | Capability | Forge home | Sub-project |
|---|---|---|---|
| utopia | Bitemporal evidence ledger, extraction, identity, ontology, reasoning, governance | `packages/world-model` (`forge_world`) + `/world` | H |
| utopia | MCP read tools + `remember` as proposal | `/mcp` world tools | I |
| utopia++ | Decision ledger and drift; unique-constrained dedup; one store | `forge_world.decisions`, `world_fact` partial unique index | H1 |
| lore | Context assembly, memory, hybrid search, wider index, context evals | `forge_world` + `forge_knowledge` + `forge_eval` | J |
| lore | Implementation loop, review, triage, auto-merge, upkeep, digests | `workflow-engine` templates on the G tool belt | K |
| lore | Traceability statement → test → code | `forge_spec` (stable `R`/`A` ids) + coverage ingest | J/K |
| tervin | Desktop surface, local gated runs, Touch ID approvals | stays in tervin; reaches forge via MCP, `--json`, SSE, `/ws`, runner protocol | L |
| tervin | Rules engine (command risk), event vocabulary, hash-chained ledger ideas | `forge_risk` (programme A), `forge_contracts` events, existing audit chain | A, G3 |

## 3. The world model in one picture

```
           world axis (at) ─────────────────────────────►
record    ┌──────────────────────────────────────────────┐
axis      │ fact v1  [2021 ──────── 2023)  recorded t1   │  invalidated t3 (superseded)
(as_of)   │ fact v2  [2021 ──────── 2024)  recorded t3   │  live
   │      │ fact w   [2024 ───────────►    recorded t3   │  live, closes v2 (end_derived)
   ▼      └──────────────────────────────────────────────┘
read(at, as_of) = facts where holds_at(at) ∧ held_at(as_of), merges resolved at as_of
decision(fact_ids, as_of) → drift = which of those facts are no longer live
```

- Writes from machines are **proposals**; only people decide (ADR-0005).
- `world_fact` is append-only with a one-way tombstone, enforced by a trigger (ADR-0004).
- Extraction calls a model through forge's provider layer; the server, not the model, verifies quotes,
  names, times and offsets (ADR-0011).

## 4. Run lifecycle after G

```
POST /agent/runs ─► agent_run row ─► enqueue forge.agent.run ─► worker
  worker: vault model ─► sandbox (workspace minimum isolation) ─► tool belt for role
  loop: plan ─► act (policy gate per tool call) ─► observe ─► finalize      [budget checked per model call]
  finalize: push branch + open PR with per-run scoped token ─► pr approval gate ─► human merge
  events: persisted + streamed (L) with enforcement = gated | observed
```

Unconfigured activities, providers or clients **refuse**; nothing reports a success it did not
achieve (ADR-0007).

## 5. Cross-cutting rules

| Rule | Where enforced |
|---|---|
| Workspace isolation on every row and route | `WorkspaceScopedModel`; routers scope by `principal.workspace_id`; foreign ids are 404 |
| Machines propose, people decide | `world_proposal`; `agent-runner` lacks `WRITE`; decider ≠ proposer |
| Append-only evidence | Postgres triggers (`attach_immutability_trigger`, `world_fact` tombstone trigger) |
| Every mutation audited | existing audit store; MCP calls audited with argument hashes, never text |
| Shadow before enforce | governance gate (H1), auto-merge (K), risk gates (A) |
| Fail closed | ADR-0007 |
| Licences | ADR-0010: re-implement from design; attribute anything copied; no unlicensed sources |

## 6. Deployment

Unchanged: the existing compose stack, Helm chart and OpenTofu. H and I add no service and no store;
they ship inside `apps/api` and `apps/worker`. L adds a single-user compose profile.
