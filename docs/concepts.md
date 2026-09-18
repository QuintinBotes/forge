# Concepts

The mental model behind Forge. Read this once and the rest of the platform —
the API, the CLI, the board, the SDKs — falls into place. For how the pieces are
wired together at runtime, see [Architecture](./architecture.md); to try them,
see [Getting started](./getting-started.md).

Forge turns a **written spec** into **orchestrated engineering work**: a spec
engine plans and validates, a workflow engine sequences the work, an agent
runtime executes it inside a sandbox, a knowledge pipeline grounds the agents in
your code, and a native board tracks it all — on one Postgres-backed platform
you run yourself.

## The core loop

```
Spec  ──►  Workflow  ──►  Agent run  ──►  Pull request  ──►  Approval  ──►  Board
(what)     (sequence)     (execute)       (change)           (gate)         (track)
  ▲                                                                           │
  └───────────────────────────  knowledge / RAG grounds every step  ─────────┘
```

You author a spec; the spec engine validates it; a workflow drives it through
plan → execute → verify; an agent runs each step in a sandbox, grounded by
hybrid retrieval over your codebase; sensitive actions pause for human approval;
and every run, decision, and cost lands on the board and in the audit log.

## Spec & the spec engine

A **spec** (`forge_spec`) is a `manifest.yaml` that describes *what* to build:
`requirements`, `acceptance_criteria`, `open_questions`, `constraints`, target
`repos`, an `execution_mode`, and a `skill_profile`. It is the unit of intent.

The **spec engine** validates the manifest and enforces an **implementation
gate**: while a spec has unresolved `open_questions` its status stays
`clarifying` and no agent may run against it. Only an `approved` spec is
executable. This is what makes Forge *spec-driven* rather than prompt-driven —
ambiguity is resolved before code is written, not after. The
**spec-validation dashboard** surfaces each spec's state.

## Workflow & the workflow engine

A **workflow** (`forge_workflow`) is a **Postgres-backed finite-state machine**
that sequences a feature through its lifecycle — for example
plan → execute → verify → PR → approval. Because state lives in Postgres,
workflows are durable and inspectable; for long-running, durable orchestration
the production stack can run **Temporal** alongside the FSM.

## Agent runtime & sandboxes

The **agent runtime** (`forge_agent`) is a **LangGraph** plan → execute → verify
loop that carries out a workflow step. Each run executes inside a **sandbox** so
agent actions are isolated from the host. Forge models a ladder of isolation
classes:

| Isolation | What it is | Status |
|-----------|------------|--------|
| `worktree` | Git-worktree / host-subprocess isolation | Available (default) |
| `container` | Per-task Docker container | Available |
| `gvisor` | gVisor user-space kernel boundary | Modelled + mapped; gated behind a virtualization-enabled CI job |
| `microvm` | Firecracker micro-VM | Modelled + mapped; gated behind a virtualization-enabled CI job |

The kind is chosen per workspace via `FORGE_SANDBOX_KIND` (defaults to
`worktree`).

The kernel-boundary tiers (`gvisor`, `microvm`) share the same provider seam as
the container sandbox; they run their real-runtime preflight only where the host
supports them.

## Multi-agent coordination

For work that fans out, the **coordinator** (`forge_coordinator`) supervises
multiple agents — dividing a spec across agents and reconciling their results —
rather than driving a single execution agent.

## Adaptive Orchestration

**Adaptive Orchestration** (`forge_orchestration_policy`) sizes a task or spec
before an agent runs it. A **pure, deterministic** scoring function reads
normalized signals — kind, priority, blast radius, file/repo counts, requirement
and acceptance-criteria counts, whether it touches contracts or security,
dependency count, and ambiguity — and returns a `ComplexitySizing`: a **tier**
(`junior` / `medior` / `senior`), a **strategy** (`single` / `swarm`), a score,
and the reasons behind it. The scorer picks neither a model nor an agent itself;
its `tier`/`strategy` output is what the model router and per-role config consume
to resolve the effective model and effort per agent role. It is configurable from
a workspace settings screen (the `/ao` API), and you can preview what a sample
task would be routed to via `POST /ao/routing-preview`. See
[`packages/orchestration-policy/README.md`](../packages/orchestration-policy/README.md).

## Runs & the run-trace viewer

A **run** is one execution of agent work. Every step, tool call, model
interaction, and decision is recorded as a **trace**. The **run-trace viewer**
replays it end to end, so you can see exactly what an agent did and why — the
audit trail for autonomous work.

## Board & work items

The **board** (`forge_board`) is Forge's native project board: **work items**
and runs tracked in one place. If you already live in another tool, Forge can
sync to it instead — see *Integrations* below and
[BYO board](./integrations/byok-and-boards.md).

## Knowledge & retrieval (RAG)

The **knowledge pipeline** (`forge_knowledge`) grounds agents in your codebase
with **hybrid retrieval**: pgvector **cosine** semantic search fused with
Postgres **full-text** (BM25-style keyword) search via **Reciprocal Rank Fusion**
(RRF, k=60), then an optional **reranker** for final ordering. Hybrid + RRF
beats either signal alone — semantic recall plus exact-term precision.

## Approvals

The **approval system** (`forge_approval`) gates sensitive agent actions behind
a human decision. When an agent reaches a gated action it pauses and enqueues an
approval; a human approves or rejects before the action proceeds. Human-in-the-
loop where it matters, autonomous everywhere else.

## Trust layer

Four features make an autonomous change *verifiable*, not merely recorded — each
an append-only, single-table record:

- **Attested Changesets** — a DSSE-signed, in-toto provenance record of what
  actually ran (agent, model, sandbox tier, policy hash, tools, approver, spec
  revision), minted when a PR gate is approved and chained into the audit log.
- **Time-Travel Runs** — deterministic record-replay of an agent run from a
  redacted cassette, plus a counterfactual **fork** that diverges onto a
  different model from a chosen point.
- **Red-Team Gate** — a heterogeneous adversary must fail to break a candidate
  change (with an *executed* failing test or a real spec-violation) before the
  change reaches the human implementation gate.
- **Self-Eval Gate** — blocks a model/prompt/router config change that regresses
  a workspace's private per-repo regression suite below a frozen baseline.

Some of these ship with parked or Phase-A limits (the Red-Team Gate records a
parked-pass when no adversary is wired; the Self-Eval Gate no-ops at the API
layer without an injected runner) — each is stated plainly in
**[Trust layer](./trust-layer.md)**.

## Realtime co-editing

Two WebSocket channels (`forge_api.routers.realtime`), both authenticated
per-workspace, back the live surfaces. `/ws` is a one-way, server→client **board
push**: the server pushes JSON envelopes as board entities change and the web
board hook maps them onto query keys to invalidate.

`/ws/spec/{spec_id}` is **collaborative spec editing** over the **Yjs** binary
sync protocol (the web client uses `yjs` + `y-websocket`). The server owns an
*authoritative* CRDT document (a `pycrdt` `Doc` holding a `Text` for `spec.md`
and `manifest.yaml`), seeded from the canonical spec engine; connected peers
converge against it. Presence and cursors ride the same socket as ephemeral Yjs
*awareness* frames, but identity (display name and colour) is **stamped
server-side** from the authenticated principal, so no peer can spoof another.
Write gating lives at the router — a read-only principal may observe, but a
doc-mutating frame from one is a policy violation and closes the socket. The Y
document is ephemeral: on quiesce (a short idle gap, or the last editor leaving)
the room materialises `spec.md` back through the engine and records exactly
**one** `SpecVersion` checkpoint (not one per keystroke), attributed to the most
recent editor.

## Policies, skills, integrations & MCP

Forge is configured declaratively through SDKs:

- **Policy** (`forge_policy`) — a `.forge/policy.yaml` that constrains what
  agents may do (allowed actions, repos, guardrails).
- **Skills** (`forge_skill`) — reusable **skill profiles** (e.g. `backend-tdd`)
  that shape how an agent works on a given spec.
- **Integrations** (`forge_integrations`) — declarative integration definitions,
  including the pluggable **project-management adapters** (Jira, Linear, Asana,
  Monday, GitHub Projects, ClickUp, Trello, GitLab, and a generic connector).
- **MCP** (`forge_mcp`) — a **Model Context Protocol** gateway that exposes tool
  sources to agents through a managed client.

## Secrets, vault & BYOK

Secrets are **envelope-encrypted** and stored in a per-workspace **key vault**
(`forge_auth`): a versioned KEK wraps per-workspace data-encryption keys, with
AAD binding so a blob for one workspace is cryptographically useless in another.
**Bring-your-own-key (BYOK)** model-provider credentials are stored the same way.
See [BYOK](./integrations/byok-and-boards.md).

## Observability, cost & audit

- **Observability** (`forge_obs`) — structured, **redaction-aware** telemetry and
  per-run **cost metrics**, so token spend and latency are attributable.
- **Audit log** — an append-only trail of who did what, when, surfaced in the
  audit view and retained for compliance.

## Access control: SSO, SCIM & RBAC

For teams, Forge provides **SAML SSO** and **OIDC** single sign-on plus
**SCIM** user provisioning (`forge_api`, on `forge_authz` roles) — configured
from an admin UI — and **multi-team RBAC** (`forge_authz`) — role-based access
scoped per team and workspace.

## Marketplace, benchmarks, orchestration policy & deployment gates

- **Integration marketplace** (`forge_marketplace`) — publish and install
  integrations, from the in-app UI or the offline `forge marketplace package`
  CLI.
- **Benchmark leaderboard** — submit, verify, and rank agent benchmark runs,
  with a public leaderboard UI (backed by an offline `forge bench` CLI for
  submission).
- **Adaptive Orchestration** (`forge_orchestration_policy`) — deterministic
  complexity sizing and per-role model/tier routing; see
  [Adaptive Orchestration](#adaptive-orchestration) above.
- **Deployment gates** (`forge_deploy`) — policy-gated promotion of changes
  through environments.

## Glossary

| Term | Meaning |
|------|---------|
| **Spec** | `manifest.yaml` describing what to build; the unit of intent |
| **Implementation gate** | Spec-engine rule that blocks runs on unapproved specs |
| **Workflow** | Postgres FSM sequencing a feature's lifecycle |
| **Run** | One execution of agent work, fully traced |
| **Sandbox** | Isolated environment an agent run executes in |
| **Skill profile** | Reusable configuration for how an agent works |
| **Policy** | `.forge/policy.yaml` constraining agent actions |
| **RRF** | Reciprocal Rank Fusion (k=60) — fuses semantic + keyword search |
| **BYOK** | Bring-your-own-key model-provider credentials |
| **MCP** | Model Context Protocol — tool-source gateway for agents |
| **Approval** | Human decision gating a sensitive agent action |
| **Attested Changeset** | DSSE-signed in-toto provenance record of what actually ran ([trust layer](./trust-layer.md)) |
| **Cassette** | Redacted record of a run's LLM + tool calls, replayed by substitution ([Time-Travel Runs](./trust-layer.md#time-travel-runs)) |
| **Red-Team Gate** | Heterogeneous adversary that must fail to break a change before the human gate |
| **Self-Eval Gate** | Blocks a config change that regresses a workspace's private per-repo suite |

Next: **[Architecture](./architecture.md)** for how these run together, or
**[Getting started](./getting-started.md)** to try them.
