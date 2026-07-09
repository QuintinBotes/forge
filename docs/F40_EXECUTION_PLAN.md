# F40 — Deferred-Scope Execution Plan

> Turns the 125 deferred requirements (`D-<THEME>-<n>`) across ~17 themes in
> [`implementation-slices/future/F40-deferred-scope.md`](./implementation-slices/future/F40-deferred-scope.md)
> into a schedulable build. **Key finding:** F40 is *not* greenfield — the prior
> build phases already implemented ~30–70% of every theme; this plan builds the
> remaining **deltas**, gate-verified against the local hardened gate (ruff +
> ruff format + mypy + pytest on real pgvector + web lint/build/typecheck).

## Classification

Each theme is either **BUILDABLE-NOW** (pure Python / stdlib / Postgres / Redis /
in-process — implementable *and* testable against the local gate) or
**INFRA-CEILING** (needs an external service or virtualization — Temporal server,
Qdrant, Vault, Firecracker/gVisor, live cloud/cluster). Infra-ceiling items ship
as *adapter behind the stable seam + contract test (backend mocked/skipped) +
runbook*, never as live-verified builds — matching the repo's existing `PARKED`
convention.

## First swarm — buildable-now, highest-leverage (branch `finalise-f40`)

Built one slice at a time with the gatekeeper pattern (implement → 3-vote
adversarial verify → repair → full-gate commit-or-revert). PM adapters are
sequenced first (user-requested "bring your own project board").

| Slice | Theme | Scope (delta) | Tier |
|---|---|---|---|
| `F40-PM-ADAPTERS-1` | INT | **Asana + Monday + GitHub Projects** adapters behind the existing `PMAdapter`/conformance seam | sonnet |
| `F40-PM-ADAPTERS-2` | INT | **ClickUp + Trello + GitLab** adapters + a **generic/custom connector** (config-driven REST+webhook field/status mapping for any board) | sonnet |
| `F40-MCP-COMPLETE` | MCP | write-tool approval gate, prompts list/get, elicitation, per-connection Redis rate-limit, retrieval ACL predicate (zero ceiling) | opus |
| `F40-POL-GOVERNANCE` | POL | enforce `skill_profiles.allowed` (422), CODEOWNERS + `min_approvals>1` + SLA-escalation + OOO delegation, nested `AGENTS.md` merge, static forbidden-shortcut gate | opus |
| `F40-AUT-CORE` | AUT | consolidate automation onto the shared `conditions` primitive, cron (`SCHEDULED`) trigger, aggregate conditions | opus |
| `F40-AUT-ACTIONS` | AUT | policy-gated external actions, SLA→incident, sprint triggers + auto-start, opt-in auto-merge (default off) | sonnet |
| `F40-PM-DEPTH` | PM | working-day calendar, per-member capacity, estimation scales + history, portfolio velocity/CFD, sprint-goal↔criteria | sonnet |
| `F40-OBS-ANALYTICS` | OBS | MTTR/MTTA, DORA + budgets + FX table, immutable per-run skill snapshot, coverage-over-time rollup | sonnet |

## Second wave (follow-up swarm)

`F40-RET-DEPTH` (recency-decay rerank, cross-source dedup + embedding-dim
migration, PDF text) · `F40-WF-SAGA` (reverse-order compensation, dry-run FSM,
`else`-subtree — also unblocks multi-repo rollback) · `F40-MR-SPINE` (multi-repo
API `repo_targets`→per-repo PR, evidence aggregation, cross-repo multi-agent) ·
`F40-UI-DEPTH` · `F40-EVAL-METRICS` · `F40-GEN-LLM` (behind the mock-ModelClient
seam) · `F40-SEC-HARDEN` (WebAuthn/JWKS, custom roles) · `F40-DOC-PORTAL`.

## Infra-ceiling follow-ups (adapter + contract + runbook only)

Temporal durable execution/replay/schedules (WF-1/7, MA-5, SEC-5, INF-3) ·
real container/microVM sandbox boot + kernel/network enforcement (SBX runtime) ·
ParadeDB `pg_search` true-BM25 (RET-2), Qdrant (RET-7), OCR (RET-5) ·
HashiCorp Vault / External-Secrets (SEC-3), SIEM+WORM (SEC-4), Sigstore/Rekor
(INT-6) · live GitLab/IdP/Slack-Grid e2e · everything cluster/cloud/GPU (INF
install, HPA autoscale, `terraform apply`, GitOps, mesh, GPU) — the IaC chunk
authors the manifests/HCL/values; the green gate proves rendering, not runtime.
