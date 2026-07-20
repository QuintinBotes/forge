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
- **Adaptive Orchestration** (`forge_orchestration_policy`) — deterministically
  sizes a spec or task's complexity (tier: junior/medior/senior, strategy:
  single/swarm) and resolves the effective model/tier routing per agent role,
  configurable from a workspace settings screen.
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

Next: **[Architecture](./architecture.md)** for how these run together, or
**[Getting started](./getting-started.md)** to try them.
