# F40 — Deferred Scope (Follow-up Roll-up)

> Phase: future · consolidates section-12 deferred work from F01-F39 · implement AFTER V1 (and where noted, after V2).

> **What this slice is.** A single schedulable home for the 125 deferred requirements
> (`D-<THEME>-<n>`) harvested from the `## 12. Out of scope / future` block of every slice in
> [`docs/implementation-slices/`](../INDEX.md) and consolidated in
> [`SPEC-FUTURE-deferred-scope.md`](./SPEC-FUTURE-deferred-scope.md). It is a *roll-up index*,
> not a monolith: each of the 17 themes below is an independently-shippable mini-slice with its
> own targets, contracts, acceptance criteria, TDD plan, and default-off flag. Sections 3, 6,
> and 7 are organized by theme cluster so each cluster reads as a self-contained slice; sections
> 4/5/8/9/10 carry the cross-theme contracts, sequencing spine, security posture, effort, and
> file map. Every requirement traces to one or more originating `F-id`s and the numbered owning
> slice(s) that already specify the V1 baseline it extends.

---

## 1. Intent — what & why

Forge's 39 numbered slices (V1 `F01–F16`, V2 `F17–F26`, V3 `F27–F35`, cross-cutting `F36–F39`)
each ship a deliberately bounded V1-or-baseline scope and explicitly push richer capability to
their `§12`. Read slice-by-slice, that deferred work is invisible to planning: it gets
re-discovered, re-scoped, and re-estimated every time a backlog grooming session opens a slice.
The deferred-scope spec already harvested, de-duplicated, and re-stated those 125 items as
buildable requirements with acceptance criteria. **F40 is the implementation slice that turns
that backlog into a schedulable build plan** — it assigns each theme concrete package/app
targets, names the V1/V2/V3 seams each item rides on, fixes the cross-theme contracts, and lays
out the sequencing spine so the highest-leverage "spine" items (Temporal, multi-repo, supervised
multi-agent, conditional policy, Helm chart, MCP sync-and-index, automation engine) land before
their fan-outs.

The enabling invariant for almost all of this work is **protocol stability shipped in V1/V2**:
`WorkflowEngine` (F07), `SandboxProvider`/`SandboxCommandRunner` (F06/F19), `KeywordSearcher`/
`Reranker`/vector-store (F05), `PMAdapter`/`PMSyncEngine` (F18), `NotificationEvent` (F16),
`AuditSink`/audit chain (F39), `SpecGenerator`/`PostmortemComposer` (F02/F17), and the
`Condition`/`ConditionGroup` primitive (F29). The majority of deferred items are **backend swaps
behind a stable seam** (Temporal for the FSM, BM25 for FTS, Qdrant for pgvector, Vault for the
Postgres vault, Firecracker for Docker) or **additive layers** (sync-and-index over query-through,
conditional rules over flat policy, multi-repo over single-repo). Where a V1 caller leaked a
backend-specific assumption, that leak is the blocker — the slice calls those out per theme.

Non-goals are inherited verbatim from the spec's permanent non-goals (§12 here): no
Turing-complete policy/automation expression language (`ConditionGroup` is the ceiling), no
in-place editing of bundled YAML/benchmark definitions, no new guards/effects without a code
change, no auto-image-updates or unattended marketplace updates, no incident auto-remediation and
no auto-approval of gates without a human, no local-password/magic-link auth, and no
WS-Federation/LDAP/AD direct bind.

## 2. User-facing behavior / journeys

Representative cross-theme journeys (each tagged with theme · originating `D-id`):

- **WF · D-WF-1/D-WF-7 — Durable workflow survives a crash.** An operator kills the worker
  mid-run; on restart the task resumes from its last durable state with no lost or duplicated
  effect, identical to the FSM backend's observable transitions. Backend is chosen per-run via
  config (`postgres_fsm | temporal`) with no DSL change.
- **MR · D-MR-1/D-MR-2 — One task, many repos.** A member files a task targeting two repos;
  the run opens one PR per repo (each gated by its own policy), fuses retrieval across both repos
  into a single ranked list, and aggregates per-criterion evidence across repos in the
  traceability view.
- **MA · D-MA-1/D-MA-2 — Supervised run.** A task with `execution_mode=supervised` spawns
  implementer/reviewer/security subagents under `max_parallel=K`; each subagent is denied actions
  outside its scoped tool set; the parent run trace links every `SubAgentRun`.
- **RET · D-RET-1/D-RET-2 — Indexed MCP + true BM25.** A Confluence MCP source is polled on its
  SLA and its resources become searchable indexed chunks with `mcp://` provenance; keyword recall
  improves after swapping FTS for ParadeDB BM25, transparently to callers.
- **POL · D-POL-1/D-POL-5 — Conditional gate.** A rule "if branch=`main` require reviewer Y"
  changes the decision by branch; a PR touching a CODEOWNERS path routes to the owner and stays
  blocked until two distinct approvals land.
- **AUT · D-AUT-1/D-AUT-2/D-AUT-8 — Saved automations.** "When status=merged, close linked spec
  task" fires deterministically; a cron rule fires with no inbound event; sprint carryover
  auto-rolls on completion.
- **INT · D-INT-1/D-INT-3/D-INT-5 — External systems.** A Forge task two-way-syncs to a Jira
  issue and a Linear issue; a GitLab repo opens an MR and is merge-gated; assignee/reviewer
  events deliver email with a working approve deep-link.
- **SEC · D-SEC-1/D-SEC-3 — Enterprise hardening.** A user signs in via SAML; SCIM provisions/
  deprovisions accounts; secrets resolve from Vault with no plaintext at rest in the app DB.
- **OBS · D-OBS-1/D-OBS-3 — Deep observability.** A project dashboard shows cost trend, retry
  rate, and p50/p95 latency over a window; a supervised run renders a nested subagent DAG with
  diff overlays.
- **INF · D-INF-1/D-INF-2 — Kubernetes.** `helm install` brings up a working stack; a pod failure
  reschedules with no data loss and the HPA scales the API under load.
- **SBX · D-SBX-1/D-SBX-3 — Isolation ladder.** Agent commands run in a per-run container that
  cannot reach the host Docker socket and is destroyed at run end; a later run executes inside a
  Firecracker microVM passing the same sandbox contract tests.
- **PM/UI/DOC/EVAL/GEN — depth surfaces.** Sprint burndown + velocity charts render; a
  requirement→test→evidence traceability dashboard shows coverage and gaps; a versioned docs
  portal builds with search; two retrieval configs are compared side-by-side with per-metric
  deltas; the LLM `SpecGenerator` meets or beats the template baseline on the golden set.

## 3. Vertical slice — by theme cluster

Each cluster is a mini-slice: **Covers** (D-ids) · **Owning/originating** (numbered slices) ·
**Targets** (packages/apps) · **Interfaces** · **Depends on** (slices by `<phase>/<id>-<slug>`
and intra-backlog D-ids). Phase tags `V2 | V3 | Post-V3` per the spec.

### 3.WF — Workflow Engine V2 & Durable Execution

- **Covers:** D-WF-1 (Temporal backend) · D-WF-2 (incident workflow on shared engine) · D-WF-3
  (visual editor) · D-WF-4 (branching action trees) · D-WF-5 (dry-run simulation) · D-WF-6
  (runbook saga + compensation) · D-WF-7 (operational Temporal maturity). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F25-temporal-integration`, `v3/F28-workflow-visual-editor`;
  originating F01, F06, F07, F12, F17, F18, F20, F21, F25, F27, F28.
- **Targets:** `packages/workflow-engine/` (add `temporal/` backend behind the existing
  `WorkflowEngine` Protocol; keep DSL/evaluator/state-vocab untouched), `apps/worker/` (Temporal
  worker + activity registration), `apps/api/app/api/v1/workflow.py` (backend selector,
  `definitions/{name}` graph data), `apps/web/app/(app)/workflow/editor/` (canvas), `deploy/`
  (Temporal server profile/subchart — see 3.INF).
- **Interfaces:** existing `WorkflowEngine` Protocol + state vocabulary + transition table
  (F07) is the contract — Temporal is a backend, not a new API; `WORKFLOW_BACKEND` config
  (`postgres_fsm | temporal`) per-run; exactly-once effect dispatch via the F08 effect
  dispatcher; canonical YAML interchange round-trip for definitions; saga compensation hooks on
  the F17 runbook executor.
- **Depends on:** `v1/F07-feature-workflow-fsm` (Protocol + state vocab — **hard, must stay
  Protocol-only**), `v1/F08-plan-execute-verify-pr-approval` (effect dispatch), `v2/F17-incident-workflows`
  (incident definition + runbook executor), `v2/F21-workflow-automations` (rule actions for
  D-WF-4), `v3/F28-workflow-visual-editor` (canvas owns D-WF-3/4/5 authoring surface),
  `3.INF` D-INF-3 (Temporal stack deploy). D-WF-1 is the V2 spine; D-WF-2/6/7, D-MA-5, D-SEC-5,
  D-OBS-8 all gate on it.

### 3.MA — Multi-Agent Orchestration

- **Covers:** D-MA-1 (supervised mode) · D-MA-2 (`max_parallel`) · D-MA-3 (custom pattern DSL) ·
  D-MA-4 (LLM-reasoned adaptive planning) · D-MA-5 (distributed fan-out) · D-MA-6 (AI merge-conflict
  resolution). Phases V3 → Post-V3.
- **Owning/originating:** `v3/F27-supervised-multi-agent`; originating F04, F06, F08, F22, F25, F27, F28, F29.
- **Targets:** `packages/agent-runtime/` (supervisor coordinator, subagent spawner, scoped
  specialist tool sets), `apps/api/app/models/agent.py` (`SubAgentRun` rows + parent linkage),
  `packages/workflow-engine/` (custom `multi_agent` DSL block parsed by the same loader),
  `apps/worker/` (distributed subagent execution for D-MA-5).
- **Interfaces:** deterministic, policy-driven supervisor (NOT a prompted reasoning agent —
  research-report mandate); `execution_mode=supervised`; `subagent_policy` (role + `allow_subagents`
  + `max_parallel`); runtime tool gate enforcing scoped tool sets; `SubAgentRun` linkable from
  `RunTrace.agent_run_ids` (already forward-compatible in F10); built-in pattern set
  (Orchestrator-Worker / Sequential / Fan-out-in / Debate / Dynamic-handoff) + custom DSL patterns.
- **Depends on:** `v1/F06-single-execution-agent` (agent loop + runtime tool gate),
  `v1/F04-repo-policy` (`allow_subagents`, role), `cross-cutting/F36-human-approval-system`
  (approval gates), `3.WF` D-WF-1 (durable join for D-MA-5), `3.WF` D-WF-3 (editor for D-MA-3),
  `3.EVAL` D-EVAL-4 (measure D-MA-4 regressions). D-MA-1 is the spine for all other MA items,
  D-MR-4, D-EVAL-4/8, D-OBS-3.

### 3.MR — Multi-Repo Execution

- **Covers:** D-MR-1 (single task across repos) · D-MR-2 (cross-repo retrieval merge + evidence
  aggregation) · D-MR-3 (mixed-provider/GitLab) · D-MR-4 (cross-repo multi-agent) · D-MR-5
  (multi-service incident context) · D-MR-6 (atomic cross-repo merge + coordinated promotion + rollback) · D-MR-7
  (dependency inference + per-repo divergent retry). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F22-multi-repo-execution`; originating F02, F03, F05, F06, F08,
  F17, F22, F23, F25, F27, F31.
- **Targets:** `packages/agent-runtime/` (multi-target run loop), `apps/api/app/models/task.py`
  (`repo_targets[]` with >1 entry, per-repo PR tracking), `packages/knowledge-core/` (cross-repo
  retrieval fusion — see 3.RET), `apps/api/app/services/traceability.py` (per-criterion evidence
  aggregation via `EvidencePort` — F23), merge-ordering/saga in `packages/workflow-engine/`.
- **Interfaces:** `repo_targets: list[RepoTarget]`; one PR/MR per repo each gated by its own
  composed repo policy; run trace links all PRs to one task; merge-gate failure on any repo blocks
  completion; cross-repo retrieval returns one fused ranked list; `EvidencePort` aggregates
  evidence per criterion across repos; atomicity via merge-queue or saga compensation (D-MR-6).
- **Depends on:** `v2/F22-multi-repo-execution` (owner), `v1/F06`, `v1/F08` (PR flow), `v1/F03-github-app`
  (mirrors), `v1/F04-repo-policy` (per-repo policy), `v1/F05-hybrid-knowledge-retrieval` (retrieval),
  `v2/F23-spec-validation-dashboard` (traceability), `3.INT` D-INT-3 (GitLab for D-MR-3), `3.MA`
  D-MA-1 (D-MR-4), `3.WF` D-WF-6 (saga for D-MR-6), `v2/F17-incident-workflows` (D-MR-5). D-MR-1
  precedes every other MR item.

### 3.RET — Advanced Retrieval & Knowledge

- **Covers:** D-RET-1 (MCP sync-and-index) · D-RET-2 (true BM25 / ParadeDB `pg_search`) · D-RET-3
  (ColBERT rerank + recency-decay leg) · D-RET-4 (subscription-driven re-index) · D-RET-5 (binary/
  non-text indexing) · D-RET-6 (cross-source dedup + embedding-model migration) · D-RET-7
  (external vector store / Qdrant) · D-RET-8 (knowledge-provenance traceability). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F20-mcp-sync-and-index`, `v1/F05-hybrid-knowledge-retrieval`;
  originating F03, F05, F09, F17, F20, F23.
- **Targets:** `packages/knowledge-core/` (`search/bm25_paradedb.py` behind `KeywordSearcher`;
  `search/colbert_reranker.py` behind `Reranker`; `search/qdrant.py` behind the vector-store
  seam; `sync/mcp_indexer.py`; `chunking/binary.py` OCR/parsing), `apps/worker/tasks/knowledge.py`
  (MCP poll + subscription consumers + re-index), `apps/api/app/api/v1/knowledge.py`
  (`sync_mode=mcp_sync_and_index`), migration for `EMBEDDING_DIM` change tooling.
- **Interfaces:** existing `KeywordSearcher` / `Reranker` / `EmbeddingProvider` Protocols and the
  `reciprocal_rank_fusion` contract (F05) are the seams — all swaps are non-breaking;
  `mcp://` `source_uri` provenance carried through; `resources/subscribe` +
  `notifications/resources/updated` push (D-RET-4) over the gateway; cross-source dedup yields a
  single result with multiple provenances; provenance links each criterion to ranked chunk(s).
- **Depends on:** `v1/F05-hybrid-knowledge-retrieval` (all seams), `v1/F09-mcp-gateway-v1`
  (gateway), `v2/F20-mcp-sync-and-index` (owner of D-RET-1), `v1/F04-repo-policy`
  (`allowed_namespaces`), `v1/F12-eval-harness` (golden set for D-RET-3 tuning), `v2/F23-spec-validation-dashboard`
  + `3.UI` D-UI-5 (D-RET-8 surfacing), `3.MCP` D-POL-1 (ACLs for D-MCP-6 cross-ref). D-RET-1
  precedes D-RET-4/5/6 and D-MCP-6; D-RET-2 is independent and parallelizable.

### 3.MCP — MCP Gateway & Protocol Features

- **Covers:** D-MCP-1 (mutating/write tool calls) · D-MCP-2 (stdio transport) · D-MCP-3 (consume
  prompts capability) · D-MCP-4 (server-side elicitation) · D-MCP-5 (per-connection rate limits) ·
  D-MCP-6 (per-resource ACL propagation). Phases V2 → Post-V3.
- **Owning/originating:** `v1/F09-mcp-gateway-v1`; originating F09, F20.
- **Targets:** `apps/api/app/services/mcp_gateway/` (write path behind admin gate + approval;
  stdio subprocess transport + lifecycle; prompts retrieval; elicitation surfacing; per-connection
  rate limiter), `packages/knowledge-core/sync/mcp_indexer.py` (ACL filter at query time).
- **Interfaces:** `allow_write=true` AND recorded admin approval required to mutate; every mutation
  audit-logged with payload hash; stdio servers launched/health-checked/terminated under sandbox
  limits; advertised `prompts` retrievable and usable with provenance; elicitation routed to the
  approver; per-connection throttle returns a typed error (not a hard run failure); per-document
  upstream entitlement enforced at retrieval time.
- **Depends on:** `v1/F09-mcp-gateway-v1` (owner), `cross-cutting/F36-human-approval-system`
  (write approval + elicitation HITL), `3.POL` D-POL-1 (policy gate for D-MCP-1, ACLs for D-MCP-6),
  `3.SBX` D-SBX-1 (process sandboxing for D-MCP-2), `3.RET` D-RET-1 (D-MCP-6 rides indexed ACLs),
  `3.INF` D-INF-9 (rate-limiting platform for D-MCP-5).

### 3.SBX — Sandboxing & Isolation

- **Covers:** D-SBX-1 (Docker per-task) · D-SBX-2 (K8s Job/Pod) · D-SBX-3 (Firecracker/gVisor) ·
  D-SBX-4 (per-language curated images) · D-SBX-5 (warm snapshot/restore pool) · D-SBX-6 (runtime
  hardening + in-container file/git tools). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F19-container-sandboxing`, `v3/F34-firecracker-sandbox`;
  originating F03, F06, F08, F19, F22, F25, F27, F34.
- **Targets:** `packages/agent-runtime/sandbox/` (`SandboxProvider` Protocol + `DockerSandboxProvider`,
  `K8sJobSandboxProvider`, `FirecrackerSandboxProvider`, `GVisorSandboxProvider`), `apps/worker/`
  (runner routing), `deploy/` (socket-proxy, PodSecurityContext/NetworkPolicy, image allowlist
  env), warm-pool manager.
- **Interfaces:** `SandboxProvider` Protocol (introduced by D-SBX-1, stable thereafter) so each
  rung is a drop-in; per-run container destroyed at end; no host-Docker-socket reachability
  (proxy-mediated); enforced CPU/mem/time limits terminate cleanly on breach; non-root +
  default-deny NetworkPolicy on K8s; kernel-level isolation verified for microVM;
  `FORGE_SANDBOX_IMAGE_*` allowlist for D-SBX-4; file/git tools confined to the mounted worktree.
- **Depends on:** `v1/F06-single-execution-agent` (`SandboxCommandRunner`), `v1/F14-docker-compose-selfhost`
  (compose runtime), `v2/F19-container-sandboxing` (owner of D-SBX-1, the ladder root),
  `3.INF` D-INF-1 (Helm for D-SBX-2/3), `v3/F34-firecracker-sandbox` (owner of D-SBX-3). Strictly
  incremental ladder: D-SBX-1 → D-SBX-2 → D-SBX-3; D-MCP-2 and D-SBX-4/5/6 ride on D-SBX-1.

### 3.POL — Advanced Policy & Governance

- **Covers:** D-POL-1 (conditional engine) · D-POL-2 (deployment gates + env promotion + rollout
  strategies + non-GitHub providers) · D-POL-3 (task-level action composition) · D-POL-4 (hard
  `skill_profiles.allowed` enforcement) · D-POL-5 (advanced approval routing: multi-reviewer +
  CODEOWNERS + SLA escalation + delegation) · D-POL-6 (workspace/cross-repo inheritance) · D-POL-7 (in-UI
  policy authoring/commit) · D-POL-8 (nested AGENTS.md merge + PolicyProfile bootstrap) · D-POL-9
  (bounded auto-remediation profile) · D-POL-10 (static-analysis `forbidden_shortcuts`). Phases V2 → Post-V3.
- **Owning/originating:** `v3/F29-advanced-policy-engine`, `v3/F31-deployment-gates`; originating
  F01, F02, F03, F04, F06, F08, F11, F16, F17, F28, F29, F31, F36.
- **Targets:** `packages/contracts/conditions/` (`Condition`/`ConditionGroup`/`ConditionOp`
  primitive lifted by F29 — shared), `packages/repo-policy/` (conditional evaluator, env matrices,
  workspace inheritance, nested AGENTS.md merge), `apps/api/app/api/v1/policy.py` (PR-to-policy
  authoring), `apps/api/app/services/verification/` (static-analysis gate for D-POL-10),
  promotion-workflow states + environment registry (D-POL-2).
- **Interfaces:** **evaluation MUST be total and non-Turing-complete** (no expression can
  loop/diverge — `ConditionGroup` is the ceiling, an explicit permanent non-goal); conditional
  decisions compose with flat policy without changing flat-only behavior; task-level restrictions
  narrow (never widen) the composed repo decision; `kind=deploy` approval gate; `min_approvals>1`
  + CODEOWNERS routing; hard `422` on disallowed skill profile; documented nested-precedence merge;
  PolicyProfile seeds a `.forge/policy.yaml` PR; static check fails on swallowed errors.
- **Depends on:** `v1/F04-repo-policy` (evaluator/loader/snapshot/router), `v3/F29-advanced-policy-engine`
  (owner of D-POL-1 — the governance spine), `cross-cutting/F36-human-approval-system`
  (approval primitive), `v1/F08` (PR flow + verification service), `v1/F11-skill-profiles`
  (registry for D-POL-4/9), `v1/F03-github-app` (PR backend for D-POL-7), `3.SEC` D-SEC-2 (multi-team
  RBAC for D-POL-6), `v3/F31-deployment-gates` (owner of D-POL-2). D-POL-1 precedes D-POL-2/3/6,
  D-AUT-4, D-MCP-6, D-AUT-10.

### 3.AUT — Automations & Rule Engine

- **Covers:** D-AUT-1 (WHEN/IF/THEN engine) · D-AUT-2 (scheduled triggers) · D-AUT-3 (aggregate
  conditions) · D-AUT-4 (external-side-effect actions) · D-AUT-5 (LLM-assisted actions) · D-AUT-6
  (LLM skill-profile suggestion) · D-AUT-7 (SLA/SLO breach auto-declare incident) · D-AUT-8
  (sprint-event automations) · D-AUT-9 (auto-merge opt-in) · D-AUT-10 (consolidate onto shared
  condition primitive). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F21-workflow-automations`; originating F01, F03, F07, F11, F12, F17, F21, F26, F29, F31, F35.
- **Targets:** `packages/forge-automation/` (deterministic evaluator, dry-run mode, trigger types,
  aggregate rollups, action registry), `apps/worker/` (Celery Beat / Temporal schedules for
  D-AUT-2), `apps/api/app/models/automation.py`, refactor to consume `packages/contracts/conditions`
  (D-AUT-10).
- **Interfaces:** rule = trigger + conditions + actions, **deterministic and side-effect-isolated
  (dry-run testable), separate from the FSM**; `SCHEDULED` trigger (cron/recurring/relative);
  cross-entity rollup conditions; external actions (webhook/issue/deploy) policy-gated + audited;
  agent-class actions delegate to a spec/workflow run (engine stays deterministic);
  profile-suggestion returns confidence + rationale, human-overridable; SLO breach → standard
  `IncidentAlert` intake; sprint carryover/auto-start; org-opt-in auto-merge (default off);
  shared `Condition`/`ConditionGroup`/`ConditionOp`.
- **Depends on:** `v2/F21-workflow-automations` (owner of D-AUT-1 — the automation spine),
  `v1/F01-project-board` (board entities/events), `v1/F07-feature-workflow-fsm` (transition
  events), `3.INT` D-INT-1 + D-INT-4 (external actions/ops for D-AUT-4/7), `3.POL` D-POL-1
  (per-action policy for D-AUT-4, primitive for D-AUT-10), `v1/F06` (agent runtime for D-AUT-5),
  `v1/F11` + `v1/F12` (D-AUT-6), `v2/F26-sprint-velocity` (D-AUT-8), `cross-cutting/F36` + D-POL-1
  (D-AUT-9). D-AUT-1 precedes D-AUT-2/3/4/5/7/8/9.

### 3.INT — Integrations & Marketplace

- **Covers:** D-INT-1 (Jira & Linear) · D-INT-2 (Asana & Monday) · D-INT-3 (GitLab provider) ·
  D-INT-4 (Datadog/Sentry/PagerDuty/Grafana + Prometheus/Alertmanager routing) · D-INT-5 (email
  notifications) · D-INT-6 (marketplace + plugin-as-code + Sigstore signing + inter-package deps) ·
  D-INT-7 (self-service GitHub App Manifest) · D-INT-8 (deeper PM sync) · D-INT-9
  (PM fidelity & two-way schema). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F18-pm-adapters`, `v3/F32-integration-marketplace`; originating
  F01, F02, F03, F09, F13, F16, F17, F18, F21, F22, F28, F32, F38.
- **Targets:** `packages/pm-adapters/` (`PMAdapter` Protocol + `PMSyncEngine` + jira/linear/asana/
  monday adapters), `packages/providers/gitlab/` (repo + MR/CI/review events), `apps/api/app/services/notifications/`
  (email subscriber on `NotificationEvent`), `apps/api/app/services/integrations/` (ops alert
  intake), marketplace registry + install/version/policy-gate service (D-INT-6), App Manifest
  onboarding flow (D-INT-7).
- **Interfaces:** `PMAdapter` Protocol + `PMSyncEngine` two-way mapping `ForgeTask`↔external,
  field-validated (unmapped fields reported not dropped); adapter conformance suite (Jira/Linear →
  Asana/Monday must pass identically); GitLab repo connect/mirror/MR/merge-gate parity; email on
  the shared `NotificationEvent` contract with approve deep-link; community connectors/skills/rule
  templates published/discovered/installed, versioned + policy-gated; manifest-flow App
  registration; deep PM sync (comments/attachments/subtasks/dependency graph/sprint mapping); ADF
  round-trip + two-way label/state creation + custom-field mapper UI.
- **Depends on:** `v2/F18-pm-adapters` (owner of D-INT-1), `v1/F01` (serializable `ForgeTask`),
  `v1/F03-github-app` (App patterns + provider abstraction for D-INT-3/7), `v1/F16-slack-notifications`
  (notification contract for D-INT-4/5), `v2/F17-incident-workflows` (intake for D-INT-4),
  `v3/F32-integration-marketplace` (owner of D-INT-6), `3.POL` D-POL-1 + `3.WF` D-WF-3 (marketplace
  gating/template interchange). D-INT-1 precedes D-INT-2/8/9.

### 3.SEC — Enterprise Security, SSO & Secrets

- **Covers:** D-SEC-1 (SAML+OIDC SSO + SCIM + auth hardening: WebAuthn/2FA, JWKS) · D-SEC-2
  (multi-team RBAC + custom roles + org tier + break-glass) · D-SEC-3 (external secrets/Vault +
  BYOK versioning) · D-SEC-4 (tamper-evident hash chain + DB immutability + SIEM export/WORM/
  partitioning) · D-SEC-5 (per-tenant Temporal namespace) · D-SEC-6 (multiple Slack workspaces /
  Enterprise Grid). Phases V2 → Post-V3.
- **Owning/originating:** `v3/F33-enterprise-sso`, `v3/F30-multi-team-rbac`; originating F07, F10,
  F14, F16, F18, F24, F25, F29, F30, F33, F37, F39.
- **Targets:** `cross-cutting auth` (`apps/api/app/services/auth/` SAML + SCIM provisioning,
  RBAC hierarchy), `apps/api/app/services/secrets/` (Vault/External-Secrets resolver behind the
  vault interface), `apps/api/app/services/audit/` (Merkle/append-only proofs) + Alembic DB
  immutability trigger on `workflow_transition`/audit tables, Temporal namespace-per-tenant
  wiring, Slack Enterprise Grid install.
- **Interfaces:** SAML IdP sign-in; SCIM create/deactivate provisions/deprovisions; external-account
  auto-map via directory (not email heuristics); workspace/team/project-scoped roles per the role
  matrix; secrets resolve from Vault with **no plaintext at rest in the app DB**; DB trigger
  rejects post-hoc edit/delete of audit/transition rows, detectable by the chain verifier;
  namespace-per-tenant with no cross-tenant visibility; two Slack teams under one Grid org.
- **Depends on:** `cross-cutting/F37-auth-secrets-byok` (base auth/vault/RBAC), `v3/F30-multi-team-rbac`
  (owner of D-SEC-2), `v3/F33-enterprise-sso` (owner of D-SEC-1), `cross-cutting/F39-audit-log`
  (D-SEC-4), `3.WF` D-WF-1 (D-SEC-5 Temporal namespaces), `3.INF` D-INF-1 (Helm for D-SEC-3),
  `v1/F16-slack-notifications` (D-SEC-6). D-SEC-2 precedes D-POL-6 and D-SEC-1.

### 3.OBS — Observability & Audit (deep)

- **Covers:** D-OBS-1 (cross-run analytics + DORA + FX/budgets + per-tenant Grafana + RUM) · D-OBS-2 (online/canary eval) · D-OBS-3 (deep
  multi-agent trace viz) · D-OBS-4 (live canvas overlay) · D-OBS-5 (trace export bundle + WS
  bidirectional) · D-OBS-6 (per-run skill-profile snapshot) · D-OBS-7 (historical coverage/trend
  snapshots + org rollup + org audit aggregation) · D-OBS-8 (Temporal codec server) · D-OBS-9 (incident analytics). Phases V2 → Post-V3.
- **Owning/originating:** `cross-cutting/F38-observability-cost-metrics`, `v1/F10-run-trace-viewer`;
  originating F10, F11, F12, F17, F22, F23, F25, F26, F27, F28, F31, F38, F39.
- **Targets:** Grafana/Prometheus dashboards in `deploy/`, `apps/web/app/(app)/analytics/`,
  `apps/web/components/trace/` (nested DAG + diff overlay, live canvas overlay, export bundle),
  `apps/api/app/api/v1/trace.py` (export endpoint + WebSocket multiplexer), `agent_run` skill-profile
  snapshot column, `traceability_rollup_history` table + recompute job, Temporal codec-server
  endpoint, incident analytics view.
- **Interfaces:** project dashboard = cost trend + retry rate + p50/p95/p99 over a window; canary
  slice scored live alongside offline; nested subagent DAG with per-subagent diff overlay; canvas
  highlights active state/edge for a live run; run exports to a self-contained reproducible bundle;
  WebSocket both receives events and accepts a control command (e.g. pause); immutable per-run
  skill-profile snapshot (analogous to `RepoPolicySnapshot`); coverage-over-time + org rollup;
  authenticated codec server decrypts Temporal payloads (unauth denied); MTTR/MTTA/remediation-accept-rate.
- **Depends on:** `cross-cutting/F38-observability-cost-metrics` (metrics — owner of D-OBS-1),
  `v1/F10-run-trace-viewer` (read model + event stream), `3.MA` D-MA-1 (D-OBS-3), `3.WF` D-WF-1
  (D-OBS-8) + D-WF-3 (D-OBS-4 canvas), `v1/F11` + `cross-cutting/F39` (D-OBS-6),
  `v2/F23-spec-validation-dashboard` + `3.UI` D-UI-4 (D-OBS-7), `v2/F17` (D-OBS-9), `3.EVAL`
  D-EVAL-2 (D-OBS-2).

### 3.EVAL — Evaluation & Benchmarking

- **Covers:** D-EVAL-1 (per-language verification toolchains) · D-EVAL-2 (A/B eval UI) · D-EVAL-3
  (public benchmark + leaderboard + signed provenance + federated + rank timelines) · D-EVAL-4 (multi-agent golden cases + supervised grading) ·
  D-EVAL-5 (expanded metrics) · D-EVAL-6 (auto-generated golden cases) · D-EVAL-7 (experiment
  tracking) · D-EVAL-8 (SAST + dependency-audit backends). Phases V2 → Post-V3.
- **Owning/originating:** `v1/F12-eval-harness`, `v3/F35-benchmark-leaderboard`; originating F05,
  F06, F08, F11, F12, F27, F35.
- **Targets:** `packages/verification/` (pluggable per-language runners + coverage parsers),
  `apps/web/app/(app)/eval/` (A/B UI + experiment tracking), `apps/api/app/api/v1/eval.py`
  (leaderboard submission/scoring), `packages/eval-harness/` (multi-agent golden cases, expanded
  metrics, recorder→golden promotion), security-role `run_sast`/`audit_dependencies` backends.
- **Interfaces:** per-language lint/test/coverage runner reports coverage (TS/Go/...); side-by-side
  config compare with per-metric deltas + significance; reproducible leaderboard scoring;
  supervised-run grading against multi-agent golden cases; embedding answer-similarity + full RAGAS
  suite; sanitized production run promoted to a deterministic re-runnable golden case; multi-arm
  experiment history/tags; SAST + dep-audit findings into the run trace.
- **Depends on:** `v1/F12-eval-harness` (harness + recorder + metrics — owner of D-EVAL-1/2),
  `v1/F08` (verification service for D-EVAL-1/8), `v1/F11` (canonical step list),
  `v3/F35-benchmark-leaderboard` (owner of D-EVAL-3), `v3/F32-integration-marketplace` (D-EVAL-3),
  `3.MA` D-MA-1 (D-EVAL-4/8), `v1/F05` (retrieval metrics for D-EVAL-2). D-EVAL-2 precedes D-EVAL-7
  and D-OBS-2; D-EVAL-4 underpins D-MA-4.

### 3.UI — Advanced UI & Collaboration

- **Covers:** D-UI-1 (real-time presence + collaborative editing) · D-UI-2 (roadmap drag-to-reschedule
  + dependency propagation) · D-UI-3 (cross-workspace saved-filter sharing + org views) · D-UI-4
  (requirement-traceability dashboard) · D-UI-5 (approval knowledge-provenance panel) · D-UI-6
  (per-user notification preferences) · D-UI-7 (inline traceability-gap editing) · D-UI-8 (richer
  Slack UX). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F23-spec-validation-dashboard`, `v1/F01-project-board`; originating
  F01, F02, F05, F09, F16, F23, F28.
- **Targets:** `apps/web/app/(app)/board/` (presence/collab, roadmap scheduling, shared filters),
  `apps/web/app/(app)/traceability/` (dashboard + inline gap edit), `apps/web/components/approval/`
  (`KnowledgeProvenancePanel`), `apps/web/app/(app)/settings/notifications/` (per-user prefs),
  Slack `views.open` modals + threaded run-driving (`apps/api` Slack interaction path).
- **Interfaces:** multi-cursor presence + CRDT/conflict-free merge (V1 is optimistic single-writer);
  drag reschedules dependents per the dependency graph and shows the cascade; org-shared filters
  visible per RBAC; requirement→test→evidence matrix with coverage/gaps; `KnowledgeProvenancePanel`
  lists sources (incl. `mcp://`) linkable to `source_uri`; per-user mute + DM-vs-channel honored
  by routing; gap annotate→assign-as-task inline; Slack approval modal + threaded reply drives a
  run step (recorded).
- **Depends on:** `v1/F01-project-board` (entities/roadmap/filters), `v2/F23-spec-validation-dashboard`
  (owner of D-UI-4; `/traceability` + verdicts), `cross-cutting/F36` (approval UI for D-UI-5),
  `v1/F05` + `v1/F09` (provenance for D-UI-5), `v1/F16` (routing for D-UI-6/8), `3.WF` D-WF-3
  (draft editor for D-UI-1), `3.SEC` D-SEC-2 (org scoping for D-UI-3). D-UI-4 precedes D-UI-7 and
  feeds D-OBS-7.

### 3.INF — Deployment, Scaling & Infrastructure

- **Covers:** D-INF-1 (Helm chart + kind/minikube) · D-INF-2 (multi-node HA autoscale) · D-INF-3
  (Temporal + observability stack deploy) · D-INF-4 (cloud images + managed datastores + Terraform) ·
  D-INF-5 (GitOps + chart signing) · D-INF-6 (one-line VM installer) · D-INF-7 (Gateway API default
  + mesh) · D-INF-8 (GPU reranker scheduling) · D-INF-9 (platform rate limiting) · D-INF-10
  (materialized `milestone_progress`). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F24-kubernetes-helm`; originating F01, F09, F13, F14, F19, F24, F25.
- **Targets:** `deploy/helm/` (chart, kind/minikube profile, HPA, NetworkPolicy, Temporal subchart,
  observability subcharts, GPU node-selectors), `deploy/terraform/` (managed datastores),
  `deploy/scripts/install.sh`, `apps/api` edge middleware (rate limiter for D-INF-9), Argo/Flux +
  OCI signing pipeline, materialized `milestone_progress` table + recompute (conditional D-INF-10).
- **Interfaces:** `helm install` → working stack, CI-verified Ingress deploy + kind/minikube
  profile; pod failure reschedules with no data loss + HPA scales API; observability/Temporal
  profile deploys working dashboards + worker target; Terraform provisions managed datastores +
  chart consumes external endpoints; signed OCI chart deployable via Argo/Flux; `curl|sh` stands up
  single-node Forge; `HTTPRoute` CI-verified default + multi-cluster topology; reranker schedules
  on GPU nodes; quota breach → typed 429 + metered; roadmap progress from cache within staleness
  budget (built only on measured regression).
- **Depends on:** `v2/F24-kubernetes-helm` (owner of D-INF-1 — the K8s spine), `v1/F14-docker-compose-selfhost`
  (images/contracts — **chart consumes, does not fork build ownership**), `cross-cutting/F37`
  (auth), `3.WF` D-WF-1 (D-INF-3 Temporal subchart), `v1/F13-local-quickstart` (D-INF-6). D-INF-1
  precedes D-SBX-2, D-INF-2/3/4/5/7/8, D-SEC-3.

### 3.DOC — Docs Platform & Demo Experience

- **Covers:** D-DOC-1 (hosted versioned docs portal) · D-DOC-2 (operator runbooks + Helm guidance) ·
  D-DOC-3 (managed-datastore backup/restore + air-gapped/multi-region guides) · D-DOC-4 (docs
  localization) · D-DOC-5 (guided demo experience). Phases V2 → Post-V3.
- **Owning/originating:** `v1/F15-selfhosting-docs`, `v1/F13-local-quickstart`; originating F13, F15.
- **Targets:** `docs/` portal (Docusaurus/MkDocs) with versions/search/i18n, `docs/operators/`
  runbooks, `docs/install/` air-gapped + multi-region, `apps/web` `is_demo`-gated banner/tour +
  scripted demo runner.
- **Interfaces:** versioned site with working search + version switcher; per-optional-service
  runbook (deploy/scale/backup/recovery); documented restore from backup; ≥1 non-English locale
  builds + selectable; with a model key set the demo runs a scripted agent run end-to-end +
  `is_demo` banner/tour (deliberately excluded from the zero-key quickstart).
- **Depends on:** `v1/F15-selfhosting-docs` (doc set — owner of D-DOC-1/2), `3.INF` D-INF-1 +
  D-INF-3 (runbooks for D-DOC-2), `3.INF` D-INF-4 (D-DOC-3), `v1/F13-local-quickstart` + `v1/F05`
  (D-DOC-5). D-DOC-1 precedes D-DOC-4.

### 3.PM — Sprint & Project Management Depth

- **Covers:** D-PM-1 (velocity + burndown) · D-PM-2 (working-day/holiday calendar) · D-PM-3
  (capacity planning) · D-PM-4 (estimation scales + history) · D-PM-5 (portfolio velocity +
  CFD/cycle-time) · D-PM-6 (team-scoped parallel sprints) · D-PM-7 (sprint-goal vs acceptance
  criteria). Phases V2 → Post-V3.
- **Owning/originating:** `v2/F26-sprint-velocity`; originating F01, F23, F26.
- **Targets:** `apps/api/app/services/sprint/` (velocity/burndown rollups, calendar, capacity,
  estimation history, team scoping), `apps/web/app/(app)/sprints/` (burndown/velocity/portfolio/
  CFD charts), `apps/api/app/models/sprint.py` (`team_id`, estimate scale/history, goal↔criteria links).
- **Interfaces:** burndown line + velocity chart over recent sprints; ideal line skips configured
  non-working days; per-member capacity accounts for leave + flags over/under-allocation;
  configurable estimation scale + retained history; portfolio velocity aggregation + CFD +
  cycle/lead-time scatter; two teams each with an active sprint in one project + rollup (adds
  `team_id` without breaking the event log); sprint goal links to acceptance criteria with
  satisfied/unsatisfied status.
- **Depends on:** `v2/F26-sprint-velocity` (owner — sprint model), `v1/F01-project-board`,
  `v2/F23-spec-validation-dashboard` + `cross-cutting/F38` (projection), `3.SEC` D-SEC-2 (teams for
  D-PM-6), `3.UI` D-UI-4 + `v2/F23` (D-PM-7), `3.OBS` D-OBS-7 (D-PM-5). D-PM-1 precedes D-PM-2/3/5.

### 3.GEN — LLM-Backed Generation

- **Covers:** D-GEN-1 (LLM-backed `SpecGenerator`) · D-GEN-2 (LLM postmortem composer) · D-GEN-3
  (LLM tests-to-criteria verdicts). Phases V2 → Post-V3.
- **Owning/originating:** `v1/F02-spec-engine`, `v2/F17-incident-workflows`; originating F02, F17, F23.
- **Targets:** `packages/agent-runtime/skills/` (spec-analyst skill / LangGraph `SpecGenerator`,
  postmortem composer, tests-to-criteria verdict generator) implemented behind the existing
  Protocols (V1 ships Template implementations).
- **Interfaces:** LLM `SpecGenerator` behind the F02 Protocol meets/beats the template baseline on
  spec-quality golden cases; LLM `PostmortemComposer` behind the F17 interface meets/beats the
  template baseline; generated tests-to-criteria mappings feed the traceability dashboard and match
  human labels within tolerance — **the runtime wiring is F06's job; this is the Protocol swap.**
- **Depends on:** `v1/F02-spec-engine` (`SpecGenerator` Protocol), `v1/F06-single-execution-agent`
  (runtime wiring), `v1/F12-eval-harness` (golden gate), `v2/F17-incident-workflows` (composer
  interface for D-GEN-2), `v2/F23-spec-validation-dashboard` (D-GEN-3). D-GEN-1 precedes D-GEN-3.

## 4. Public interfaces / contracts

F40 introduces no new top-level API surface of its own — it implements behind existing V1/V2/V3
seams. The load-bearing cross-theme contracts:

**Backend-selection config (the dominant pattern).** Each swappable backend is chosen by config
behind its V1 Protocol with no caller change:

```python
# settings (env / workspace config)
WORKFLOW_BACKEND: Literal["postgres_fsm", "temporal"] = "postgres_fsm"   # D-WF-1
KEYWORD_BACKEND:  Literal["postgres_fts", "paradedb_bm25"] = "postgres_fts"  # D-RET-2
RERANKER:         Literal["jina_v2", "colbert", "off"] = "jina_v2"        # D-RET-3
VECTOR_STORE:     Literal["pgvector", "qdrant"] = "pgvector"             # D-RET-7
SANDBOX_PROVIDER: Literal["worktree", "docker", "k8s_job", "firecracker", "gvisor"] = "worktree"  # D-SBX-*
SECRETS_BACKEND:  Literal["postgres_vault", "vault", "external_secrets"] = "postgres_vault"  # D-SEC-3
```

**`SandboxProvider` Protocol** (introduced by D-SBX-1, the stable seam every higher rung
implements):

```python
class SandboxProvider(Protocol):
    async def acquire(self, *, run_id: UUID, image: str, limits: ResourceLimits,
                      worktree: Path) -> Sandbox: ...
    async def exec(self, sandbox: Sandbox, cmd: list[str], *, timeout_s: int) -> ExecResult: ...
    async def release(self, sandbox: Sandbox) -> None: ...   # destroys per-run isolation
```

**Shared `Condition` primitive** (lifted to `packages/contracts/conditions` by F29; consumed by
both policy D-POL-1 and automation D-AUT-10 — total, non-Turing-complete):

```python
class ConditionOp(StrEnum): eq="eq"; ne="ne"; in_="in"; gt="gt"; lt="lt"; exists="exists"; matches="matches"
class Condition(BaseModel):   field: str; op: ConditionOp; value: JSONValue
class ConditionGroup(BaseModel): all: list["Condition | ConditionGroup"] = []; any: list["Condition | ConditionGroup"] = []
# evaluate(group, context) -> bool  — provably terminating; the ceiling, no expression language.
```

**`PMAdapter` Protocol + `PMSyncEngine`** (D-INT-1, conformance-suite-validated so D-INT-2 plugs
in): `to_external(task) / from_external(issue) / sync(direction)` with field-validation reporting
unmapped fields. **`RuleEngine`** (D-AUT-1): `evaluate(event, rules, *, dry_run=False) ->
list[ActionPlan]` — deterministic, side-effect-isolated. **`WorkflowEngine`** (F07): unchanged —
Temporal is a backend. **`KeywordSearcher`/`Reranker`/`EmbeddingProvider`** (F05) and
`reciprocal_rank_fusion` (F05): unchanged. **`NotificationEvent`** (F16): email/ops subscribers
plug in. **`AuditSink` + chain verifier** (F39): D-SEC-4 adds Merkle proofs + a DB immutability
trigger behind them. **`SpecGenerator`/`PostmortemComposer`** (F02/F17): LLM implementations
behind the V1 Protocols.

Per-theme contract detail and exact signatures live in the originating numbered slice's §4; F40
holds only the cross-theme seams and the config selectors above.

## 5. Dependencies — sequencing on V1/V2 (and V3) slices

**Hard global prerequisite:** the entire V1 critical path must have shipped with its Protocol
seams intact (`F00*` foundation → `F37`/`F39` → `F04` → `F05`/`F11` → `F06` → `F07` → `F08` →
`F36`, with `F01` in parallel). Any V1 leakage of backend-specific assumptions into callers is the
single largest risk and must be fixed before the corresponding swap (see §8/§9).

**Spine items and their fan-outs** (mirrors the spec's "Cross-cutting risks & sequencing"; build
spines first):

1. **D-WF-1 (Temporal) — V2 spine.** Requires `v1/F07-feature-workflow-fsm` (Protocol + state
   vocab, Protocol-only callers) and `v1/F08` (effect dispatch). Precedes D-WF-2/6/7, D-MA-5,
   D-SEC-5, D-OBS-8, and D-INF-3's Temporal subchart. Owner: `v2/F25-temporal-integration`.
2. **D-MR-1 (multi-repo) — V2.** Requires `v2/F22-multi-repo-execution`, `v1/F06`, `v1/F08`,
   `v1/F03`, `v1/F04`. Precedes D-MR-2/3/4/5/6/7 and feeds D-UI-4 / D-OBS-7 multi-repo evidence.
3. **D-MA-1 (supervised multi-agent) — V3.** Requires `v3/F27-supervised-multi-agent`, `v1/F06`,
   `v1/F04`, `cross-cutting/F36`. Precedes D-MA-2/3/4/5/6, D-MR-4, D-EVAL-4/8, D-OBS-3. Keep the
   supervisor deterministic; defer D-MA-4's LLM routing until D-EVAL-4 can measure regressions.
4. **Sandboxing ladder — V2→V3.** D-SBX-1 (Docker, `v2/F19`) → D-SBX-2 (K8s, also needs D-INF-1) →
   D-SBX-3 (Firecracker, `v3/F34`). D-MCP-2 and D-SBX-4/5/6 ride on D-SBX-1's stable
   `SandboxProvider`.
5. **D-POL-1 (conditional policy) — V3 governance spine.** Requires `v1/F04` and
   `v3/F29-advanced-policy-engine`. Precedes D-POL-2 (`v3/F31`), D-POL-3/6, D-AUT-4, D-MCP-6,
   D-AUT-10. D-SEC-2 (`v3/F30`) precedes D-POL-6 and D-SEC-1 (`v3/F33`).
6. **D-INF-1 (Helm) — V2.** Requires `v2/F24-kubernetes-helm` consuming `v1/F14` images.
   Precedes D-SBX-2, D-INF-2/3/4/5/7/8, D-SEC-3.
7. **D-RET-1 (MCP sync-and-index) — V2.** Requires `v1/F09` + `v1/F05`, owned by `v2/F20`.
   Precedes D-RET-4/5/6 and D-MCP-6. D-RET-2 (BM25) is independent and parallelizable.
8. **D-AUT-1 (rule engine) — V2.** Requires `v2/F21-workflow-automations`, `v1/F01`, `v1/F07`.
   Precedes D-AUT-2/3/4/5/7/8/9. Authoring/sharing surfaces (D-WF-3, D-INT-6) sequence after.
9. **Cross-cutting underpinnings.** `cross-cutting/F39-audit-log` underpins D-SEC-4 and D-OBS-6;
   `v1/F12-eval-harness` underpins D-EVAL-2/3/4 and D-OBS-2; `cross-cutting/F38` underpins
   D-OBS-1 and the PM rollups (D-PM-1).

**Phase gating.** V2-phase D-items implement once their V2 owner slice ships; V3-phase D-items
gate additionally on their V3 owner; Post-V3 items gate on the V2/V3 spine items listed above and
are sequenced opportunistically once the V1–V3 critical path lands.

## 6. Acceptance criteria — by theme cluster

Each criterion is testable; numbering is `<THEME>.<n>`. These roll up the per-D-item acceptance
criteria in `SPEC-FUTURE-deferred-scope.md` (consult it for the exhaustive per-item list).

**WF.** (1) A task drives `created→…→merged` on the Temporal backend with identical observable
transitions to the FSM (`postgres_fsm | temporal` selectable per-run, no DSL change). (2) Killing
+ restarting the worker mid-run resumes with no lost/duplicated effects (exactly-once verified by
audit assertion). (3) The incident definition validates through the same loader and runs on either
backend with zero engine code change. (4) A canvas-edited definition round-trips to identical YAML
accepted by the V1 loader; an invalid graph (unknown guard/unreachable state) is field-rejected;
each save is an immutable revision with a visual diff. (5) A false branch executes the `else`
subtree; a configured delay defers the next action (verified in replay). (6) Dry-run returns the
ordered state path + would-fire guards/effects with **no real effect dispatched** (effect-sink
spy). (7) A failure at step N runs compensations for `1..N-1` in reverse to a consistent terminal
state. (8) An in-flight FSM run migrates to Temporal and resumes without state loss; background
jobs run as Temporal schedules at prior cadence; Temporal Cloud env connects with no code change.

**MA.** (1) `execution_mode=supervised` spawns subagents per pattern and joins one
`AgentRunResult`. (2) A subagent acting outside its scoped tool set is denied by the runtime gate.
(3) `SubAgentRun` rows are recorded + linkable from the parent trace. (4) With `max_parallel=K`,
≤K subagents run concurrently; the K+1th queues; the limit is read from policy and violations are
logged. (5) A custom DSL pattern executes with built-in supervisor semantics; invalid definitions
are rejected at load. (6) Adaptive routing matches/beats deterministic routing on the multi-agent
golden set with recorded rationale. (7) Distributed subagents join durably across a worker
restart. (8) An auto-resolved merge passes repo checks (still human-gated); failure falls back to
the V1 escalate path.

**MR.** (1) A two-repo task produces two PRs each gated by its own policy; the trace links both to
one task; a merge-gate failure on either blocks completion. (2) Retrieval returns one fused ranked
list across repos; a criterion satisfied in two repos shows aggregated (not duplicate) evidence.
(3) A GitHub+GitLab task opens a PR and an MR, each policy-gated. (4) Per-repo implementer
subagents + one cross-repo reviewer. (5) An incident retrieves context from >1 repo and proposes
cross-repo PRs. (6) A later-repo merge failure auto-reverts already-merged PRs; the all-or-none
outcome is audited; a multi-repo release promotes all participating repos' artifacts together or
none. (7) Real code dependencies order correctly without manual `depends_on`; a failure retries
only the failed repo, untouched siblings.

**RET.** (1) A configured MCP source is polled on its SLA and its resources appear as searchable
indexed chunks with `mcp://` provenance; re-sync updates changed + removes deleted (no orphans).
(2) BM25 backend is config-selectable, passes the retrieval contract tests, and recall ≥ FTS
baseline on the golden set. (3) ColBERT rerank is selectable behind the reranker interface and
improves the primary RAGAS metric vs Jina (or is gated off); recency-decay weight is configurable
and deterministic. (4) An upstream update re-indexes only the affected resource within the latency
budget. (5) A PDF is parsed/chunked/retrievable; an image is OCR'd + indexed. (6) A two-source
document yields one deduped result with both provenances; an embedding-model change re-indexes and
returns valid results at the new dim. (7) Qdrant is config-selectable and passes the retrieval
contract tests with no caller change. (8) Each criterion shows the ranked chunk(s) that informed
it, linkable to `source_uri`.

**MCP.** (1) A write tool call is denied unless `allow_write=true` AND an admin approval is
recorded; every mutation is audit-logged with payload hash. (2) A stdio MCP server is launched,
health-checked, queried, cleanly terminated, and runs under sandbox/resource limits. (3) An
advertised prompt is retrievable + usable in a run with provenance. (4) An elicitation request
surfaces to the approver and the supplied value continues the call. (5) A connection over its rate
is throttled with a typed error, not a hard run failure. (6) A document the caller is not entitled
to upstream is excluded from retrieval results at query time.

**SBX.** (1) Agent commands run in a per-run container destroyed at run end; the container cannot
reach the host Docker socket (proxy-mediated only); resource breach terminates cleanly. (2) Each
task runs in its own Pod with non-root security context + default-deny NetworkPolicy. (3) A run
executes inside a Firecracker microVM, passes the sandbox contract tests, and host syscalls from
the guest are mediated/blocked. (4) A new language image registered via env+allowlist is selectable
by skill profile. (5) Warm-pool runs show lower cold-start latency than disposable, isolation
preserved between tenants. (6) File tools operate inside the sandbox boundary with no host FS
access outside the mounted worktree.

**POL.** (1) "if branch=`main` require reviewer Y" changes the decision by branch; evaluation is
total + non-Turing-complete (proven by contract tests); conditional composes with flat policy
without changing flat-only behavior. (2) A `staging→prod` promotion requires the `kind=deploy`
approval before dispatch; per-environment matrices apply the correct rule set; a canary/blue-green
rollout shifts traffic per its steps and a failed bake-time gate rolls back; a non-GitHub
`DeployProvider` and incident-driven auto-rollback work via the same primitive. (3) A task-level
restriction narrows (never widens) the repo decision; widening attempts are ignored + logged. (4)
Creating/running a task with a profile outside `allowed` returns `422` with a field error. (5) A
CODEOWNERS-path PR routes to the owner; with `min_approvals=2` the gate stays blocked until two
distinct approvals; an unresolved gate escalates per its SLA chain and an OOO approver's gates
reassign to a delegate. (6) A workspace rule applies to all member repos unless a more specific repo
rule overrides. (7) UI policy editing opens a PR against `.forge/policy.yaml`; merge required to
take effect. (8) A nested `AGENTS.md` overrides/merges per documented precedence; a PolicyProfile
seeds a `.forge/policy.yaml` PR. (9) The bounded auto-remediation profile permits only the listed
actions; everything else still requires approval. (10) A diff that swallows errors fails an
automated check, not just reviewer flagging.

**AUT.** (1) A rule fires its actions on a matching trigger+conditions; evaluation is deterministic
+ dry-run-testable. (2) A cron rule fires with no inbound event; a scheduled/timed deployment
dispatches at its configured time via the same trigger. (3) An "all subtasks complete"
rule fires only when every subtask is complete. (4) An external action (webhook/issue/deploy)
fires gated by policy + audit-logged. (5) A summarize-and-comment action delegates to an agent run
and posts the result; the engine stays deterministic. (6) The suggester proposes a profile with
confidence/rationale, human-overridable. (7) A breaching SLO alert creates an incident via the
standard intake with the alert as evidence. (8) On completion, incomplete tasks auto-roll; a
reached `start_date` auto-activates the sprint. (9) With org auto-merge enabled, a fully-approved
PR merges without a manual click; default off. (10) `forge_automation` uses the shared condition
primitive with no F21 test-suite regression.

**INT.** (1) A Forge task creates/updates a Jira + Linear issue and reflects status both ways;
unmapped fields are reported, not dropped. (2) Asana + Monday adapters pass the same conformance
suite as Jira/Linear. (3) A GitLab repo connects/mirrors/opens-MR/merge-gates like a GitHub repo.
(4) A Sentry/Datadog alert drives an `IncidentAlert`; a PagerDuty page reaches the current on-call;
Prometheus/Alertmanager alerts route to the configured channel. (5) Assignee/reviewer events
deliver email; an approval email has a working approve deep-link. (6) A community
connector/skill/rule/plugin is published/discovered/installed, versioned + policy-gated before
activation; a signed package's provenance verifies against the transparency log and declared
inter-package dependencies resolve. (7) An operator registers a GitHub App from the UI via the manifest
flow with no env hand-edit. (8) Comments/attachments/subtasks/dependency edges round-trip;
sprints/milestones map to Jira sprints + Linear cycles. (9) A Jira table round-trips without loss;
a missing external label/state is created from Forge; a custom field maps via UI.

**SEC.** (1) SAML/OIDC sign-in works; SCIM create/deactivate provisions/deprovisions; external
accounts auto-map via directory, not email; a workspace federates >1 IdP gated by domain-ownership
verification; WebAuthn/2FA + EdDSA/JWKS sessions are enforceable. (2) Workspace/team/project-scoped
roles (incl. workspace-defined custom roles) gate actions per the matrix; a break-glass elevation
is approval-gated, time-bounded, and audited. (3) Secrets resolve from Vault/External Secrets with
no plaintext at rest in the app DB; rotated BYOK secrets retain retrievable prior versions. (4) Any
post-hoc edit/delete of an audit/transition row is rejected by a DB trigger and detectable by the
chain verifier; audit events export to a SIEM and the archive bucket enforces WORM. (5) Each
tenant's workflows run in a dedicated Temporal namespace with no cross-tenant visibility. (6) Two
Slack teams link to distinct Forge workspaces under one Enterprise Grid org install.

**OBS.** (1) A project dashboard shows cost trend, retry rate, and p50/p95 latency over a window;
a DORA dashboard computes deploy frequency/lead-time/change-fail/MTTR; costs FX-normalize and a
budget hard cap blocks/alerts on overspend. (2) A canary slice is scored live alongside offline scores. (3) A supervised run renders a nested
DAG with each subagent's steps + diff overlay. (4) The canvas highlights the active state/edge for
a live run within the latency budget. (5) A run exports to a self-contained reproducible bundle; a
WebSocket client receives events + sends a control command. (6) A run records an immutable
skill-profile snapshot unchanged by later edits. (7) Coverage-over-time renders from periodic
snapshots; an org dashboard aggregates across projects and aggregates audit events across
workspaces. (8) An authenticated operator views
decrypted Temporal payloads; unauth access is denied. (9) The dashboard computes MTTR/MTTA/
remediation-accept-rate over a window.

**EVAL.** (1) A TypeScript task runs lint/test/coverage via the pluggable runner and reports
coverage. (2) Two configs compare side-by-side with per-metric deltas + significance in the UI.
(3) A submitted run is scored against the public suite and ranked reproducibly; signed submission
provenance verifies before ranking, rank-over-time renders from snapshots, and second-instance
submissions aggregate into the federated leaderboard. (4) A supervised run is graded end-to-end
against a multi-agent golden case. (5) The harness reports embedding
answer-similarity + the full RAGAS set. (6) A sanitized production run is promoted to a
deterministic golden case and re-runs identically. (7) Experiments track over time with
history/tags + >2-arm comparison. (8) A security-role subagent runs SAST + dep-audit and reports
findings into the trace.

**UI.** (1) Two users editing one description see each other's cursors and merge without lost
writes. (2) Dragging a milestone reschedules dependents per the dependency graph + shows the
cascade. (3) An org-shared filter is visible/usable in other workspaces per RBAC. (4) The
dashboard renders the requirement→test→evidence matrix with coverage + gaps per spec. (5) The
approval view lists informing knowledge sources, each linkable to `source_uri`. (6) A user mutes
an event type + chooses DM-vs-channel; routing honors it. (7) A gap is annotated + converted to an
assigned task without leaving the dashboard. (8) An approval opens a Slack modal with full context;
a threaded reply drives a run step + is recorded.

**INF.** (1) `helm install` brings up a working stack; CI verifies a classic Ingress deploy + a
kind/minikube profile runs locally. (2) A pod failure reschedules to another node with no data
loss; the HPA scales the API on load. (3) The observability/Temporal profile deploys working
dashboards + a Temporal worker target. (4) Terraform provisions managed datastores; the chart
connects via external endpoints. (5) The chart publishes as a signed OCI artifact deployable via
Argo/Flux. (6) The installer stands up single-node Forge on a fresh VM in one command. (7)
`HTTPRoute` is CI-verified as a default routing option; a multi-cluster topology is documented +
deployable. (8) The reranker schedules onto GPU nodes via selectors/tolerations. (9) A caller over
quota gets a typed 429 + metered event. (10) Roadmap progress reads from a cache kept within the
staleness budget — built only if a measured `seed_demo_board` regression justifies it.

**DOC.** (1) Docs build to a versioned site with working search + version switcher. (2) Each
optional service has a runbook covering deploy/scale/backup/recovery. (3) A documented restore
recovers a managed-datastore deployment from backup. (4) ≥1 non-English locale builds + is
selectable. (5) With a model key set, the demo runs a scripted agent run end-to-end; the demo
workspace shows an `is_demo` banner + optional tour.

**PM.** (1) A sprint shows a burndown line; the project shows a velocity chart over recent sprints.
(2) The ideal burndown line skips configured non-working days. (3) Per-member capacity accounts
for leave + surfaces over/under-allocation. (4) A task estimates on a configurable scale with
retained history. (5) A portfolio view aggregates velocity across projects; CFD + cycle/lead-time
charts render. (6) Two teams each have an active sprint in one project; a rollup aggregates both.
(7) A sprint goal links to its acceptance criteria + shows satisfied/unsatisfied status.

**GEN.** (1) The LLM `SpecGenerator` produces specs that pass spec-quality golden cases at/above
the template baseline. (2) The LLM postmortem composer scores at/above the template baseline on
the golden set. (3) Generated tests-to-criteria mappings feed the traceability dashboard + match
human labels within tolerance.

## 7. Test plan (TDD) — by theme cluster

Write tests first per the backend-tdd discipline (≥80% coverage). Each theme owns its test root
under the originating package/app; shared fakes (deterministic, no-network) are reused.

- **WF.** `packages/workflow-engine/tests/`: `test_temporal_backend_parity` (same
  `workflow_transition` rows as FSM); `test_worker_kill_resumes_exactly_once` (effect-sink spy
  asserts no dup/loss); `test_incident_def_loads_on_both_backends`; `test_canvas_yaml_roundtrip` +
  `test_invalid_graph_field_rejected`; `test_branch_else_and_delay_in_replay`;
  `test_dry_run_no_effects_dispatched`; `test_saga_compensates_in_reverse`;
  `test_fsm_to_temporal_migration_resumes`. Fixtures: `FakeEffectSink`, in-memory Temporal test env.
- **MA.** `packages/agent-runtime/tests/multi_agent/`: `test_supervised_spawns_and_joins`;
  `test_subagent_scoped_tool_denied`; `test_subagent_run_linked_to_parent`;
  `test_max_parallel_queues_kplus1`; `test_custom_pattern_executes_like_builtin` +
  `test_invalid_pattern_rejected_at_load`; `test_distributed_join_survives_restart`;
  `test_adaptive_routing_vs_deterministic_on_golden`; `test_merge_conflict_autoresolve_or_fallback`.
  Fixtures: `FakeSupervisor`, deterministic subagent stubs.
- **MR.** `packages/agent-runtime/tests/multi_repo/` + `apps/api/tests/multi_repo/`:
  `test_two_repos_two_prs_each_policy_gated`; `test_trace_links_both_prs_one_task`;
  `test_merge_gate_failure_blocks_completion`; `test_cross_repo_retrieval_single_fused_list`;
  `test_evidence_aggregated_not_duplicated`; `test_github_plus_gitlab_pr_and_mr`;
  `test_atomic_rollback_reverts_merged_on_later_failure`; `test_per_repo_divergent_retry`.
- **RET.** `packages/knowledge-core/tests/`: `test_mcp_sync_indexes_updates_and_removes`
  (no orphans, `mcp://` provenance); `test_bm25_passes_contract_and_recall_ge_fts`;
  `test_colbert_selectable_and_recency_decay_deterministic`; `test_subscription_reindex_only_affected`;
  `test_pdf_and_image_indexed`; `test_cross_source_dedup_single_result_both_provenances` +
  `test_embedding_dim_migration_reindex`; `test_qdrant_passes_retrieval_contract`;
  `test_provenance_links_criterion_to_chunk`. Reuse F05 `FakeEmbeddingProvider`/`FakeReranker`.
- **MCP.** `apps/api/tests/mcp/`: `test_write_denied_without_allow_write_and_approval` +
  `test_mutation_audit_logged_with_payload_hash`; `test_stdio_server_lifecycle_under_limits`;
  `test_prompt_retrievable_and_usable`; `test_elicitation_surfaces_and_resumes`;
  `test_rate_limit_typed_error_not_run_failure`; `test_per_resource_acl_excludes_unentitled`.
- **SBX.** `packages/agent-runtime/tests/sandbox/`: `test_per_run_container_destroyed`;
  `test_no_host_docker_socket_reachable`; `test_resource_breach_terminates_cleanly`;
  `test_k8s_pod_nonroot_default_deny`; `test_firecracker_passes_contract_and_blocks_syscalls`;
  `test_curated_image_selectable_by_profile`; `test_warm_pool_lower_coldstart_isolated`;
  `test_file_tools_confined_to_worktree`. `SandboxProvider` contract suite runs against every backend.
- **POL.** `packages/repo-policy/tests/`: `test_branch_conditional_changes_decision`;
  `test_evaluation_total_non_turing_complete` (no input loops/diverges);
  `test_conditional_composes_without_changing_flat`; `test_task_restriction_narrows_not_widens`;
  `test_disallowed_skill_profile_422`; `test_codeowners_routing_and_min_approvals_2`;
  `test_workspace_rule_overridden_by_repo_rule`; `test_ui_policy_edit_opens_pr`;
  `test_nested_agents_md_precedence` + `test_policyprofile_seeds_pr`;
  `test_bounded_remediation_profile_scope`; `test_forbidden_shortcuts_static_gate_fails`.
- **AUT.** `packages/forge-automation/tests/`: `test_rule_fires_on_match_deterministic` +
  `test_dry_run_isolated`; `test_cron_trigger_fires_without_event`;
  `test_all_subtasks_complete_aggregate`; `test_external_action_policy_gated_audited`;
  `test_llm_action_delegates_engine_stays_deterministic`; `test_profile_suggestion_overridable`;
  `test_slo_breach_creates_incident`; `test_sprint_carryover_and_autostart`;
  `test_auto_merge_optin_default_off`; `test_consumes_shared_condition_no_f21_regression`.
- **INT.** `packages/pm-adapters/tests/` (shared conformance suite run per adapter) +
  `apps/api/tests/integrations/`: `test_jira_linear_two_way_sync_reports_unmapped`;
  `test_asana_monday_pass_conformance`; `test_gitlab_connect_mirror_mr_gate`;
  `test_ops_alert_drives_incident_and_pages_oncall`; `test_email_event_and_approval_deeplink`;
  `test_marketplace_publish_install_versioned_gated`; `test_app_manifest_onboarding`;
  `test_deep_pm_sync_roundtrip`; `test_adf_roundtrip_and_field_mapper`.
- **SEC.** `apps/api/tests/security/`: `test_saml_signin_and_scim_provision_deprovision` +
  `test_directory_automap`; `test_rbac_scoped_roles_matrix`;
  `test_secrets_from_vault_no_plaintext_at_rest`; `test_audit_immutability_trigger_and_chain_verify`;
  `test_per_tenant_temporal_namespace_isolation`; `test_enterprise_grid_two_teams`.
- **OBS.** `apps/api/tests/observability/` + `apps/web` component tests:
  `test_project_dashboard_cost_retry_latency`; `test_canary_scored_live`;
  `test_supervised_nested_dag_diff_overlay`; `test_live_canvas_overlay`;
  `test_trace_export_bundle_reproducible` + `test_ws_receives_and_controls`;
  `test_skill_profile_snapshot_immutable`; `test_coverage_over_time_and_org_rollup`;
  `test_codec_server_auth_required`; `test_incident_mttr_mtta_accept_rate`.
- **EVAL.** `packages/eval-harness/tests/` + `apps/web` eval UI tests:
  `test_typescript_runner_reports_coverage`; `test_ab_ui_per_metric_deltas`;
  `test_leaderboard_scoring_reproducible`; `test_supervised_run_graded_on_golden`;
  `test_expanded_metrics_ragas_full`; `test_promote_sanitized_run_to_golden`;
  `test_experiment_tracking_multi_arm`; `test_sast_depaudit_findings_in_trace`.
- **UI.** `apps/web/**/tests/`: `test_presence_multicursor_no_lost_write`;
  `test_roadmap_drag_cascades_dependents`; `test_org_shared_filter_rbac`;
  `test_traceability_matrix_coverage_gaps`; `test_provenance_panel_linkable_sources`;
  `test_per_user_notification_prefs_honored`; `test_inline_gap_annotate_assign`;
  `test_slack_modal_and_threaded_run_step`.
- **INF.** `deploy/tests/` + CI: `test_helm_install_brings_up_stack` + `test_kind_profile_local`;
  `test_pod_failure_reschedules_hpa_scales`; `test_observability_temporal_profile`;
  `test_terraform_managed_datastores_external_endpoints`; `test_signed_oci_chart_argo_flux`;
  `test_one_line_vm_installer`; `test_httproute_ci_default` + `test_multicluster_topology`;
  `test_gpu_reranker_scheduling`; `test_rate_limit_429_metered`;
  `test_milestone_progress_cache_staleness` (conditional, gated on regression).
- **DOC.** `docs/tests/` + portal build CI: `test_versioned_site_search_switcher`;
  `test_operator_runbook_completeness`; `test_managed_restore_procedure`;
  `test_non_english_locale_builds`; `test_guided_demo_scripted_run`.
- **PM.** `apps/api/tests/sprint/` + `apps/web`: `test_burndown_and_velocity_charts`;
  `test_ideal_line_skips_nonworking_days`; `test_capacity_accounts_for_leave`;
  `test_estimation_scale_and_history`; `test_portfolio_velocity_cfd_cycle_time`;
  `test_two_team_parallel_sprints_rollup`; `test_sprint_goal_links_criteria_status`.
- **GEN.** `packages/agent-runtime/tests/skills/`: `test_llm_specgen_meets_template_baseline`;
  `test_llm_postmortem_meets_baseline`; `test_llm_tests_to_criteria_match_human_within_tolerance`.
  All gated through `v1/F12-eval-harness` golden cases; skipped if the harness is absent.

Cross-theme contract suites (`SandboxProvider`, `KeywordSearcher`, `Reranker`, vector-store,
`PMAdapter`, `WorkflowEngine`, `RuleEngine`, shared `Condition` evaluator) run against every
backend/implementation to enforce the "swap is non-breaking" invariant.

## 8. Security & policy considerations

The deferred backlog concentrates the platform's highest-blast-radius work; each high-risk item
ships behind a **default-off flag** with its own contract + abuse tests (per the spec's risk #10):

- **Policy correctness is a security boundary (D-POL-1/D-POL-2).** Evaluation MUST stay total and
  non-Turing-complete — the expression-language temptation is a permanent non-goal. Conditional
  rules must never *widen* a flat decision; task-level overlays narrow only. Deployment gates
  (`kind=deploy`) must block dispatch until approval.
- **Mutation safety (D-MCP-1).** Write MCP calls require `allow_write=true` AND a recorded admin
  approval; every mutation is audit-logged with payload hash. No bidirectional/write-back sync
  without the gate.
- **Kernel isolation (D-SBX-3) + container escape (D-SBX-1/2).** No host Docker socket
  reachability (proxy-mediated); non-root + default-deny NetworkPolicy on K8s; microVM syscall
  mediation verified. Resource breach terminates the run cleanly.
- **Identity + secrets (D-SEC-1/2/3).** SSO/SCIM and RBAC are auth-boundary changes — full role
  matrix tests + deprovisioning verification. Secrets backends must leave **no plaintext at rest
  in the app DB**.
- **Data integrity (D-MR-6, D-SEC-4).** Atomic cross-repo merge must be all-or-none with audited
  rollback; audit/transition immutability is enforced by a DB trigger *and* a chain verifier
  (defense in depth beyond repository-layer enforcement).
- **Tenant isolation (D-SEC-5, D-RET-1, D-MCP-6).** Per-tenant Temporal namespaces; indexed MCP
  chunks scoped by `allowed_namespaces`; per-resource ACLs exclude unentitled docs at query time;
  all reads filter by `workspace_id`.
- **Determinism mandates.** The supervisor (D-MA-1) and the rule engine (D-AUT-1) stay
  deterministic/policy-driven; LLM-class behavior (D-MA-4, D-AUT-5) is delegated to graded agent
  runs, never embedded in the router/evaluator.
- **Permanent non-goal reaffirmed.** Incident auto-remediation without human approval is never
  built; D-POL-9's bounded relaxed-posture profile is the only sanctioned relaxation, and even it
  permits only explicitly enumerated actions.
- **Egress + abuse bounds.** External-side-effect actions (D-AUT-4), email (D-INT-5), and ops
  integrations (D-INT-4) are policy-gated + audited; platform rate limiting (D-INF-9) backs
  per-connection quotas (D-MCP-5).

## 9. Effort estimate & risk

**Overall: XL** — this is a multi-quarter program, not a single sprint; the realistic unit of work
is one theme mini-slice at a time, scheduled behind its spine. Per-theme bands (using the INDEX
S/M/L convention, roughly summed across each theme's D-items):

| Theme | Effort | Dominant risk |
|---|---|---|
| WF | L–XL | FSM-semantic leakage into callers blocks the Temporal swap (High) |
| MA | L | Keeping the supervisor deterministic; correct scoped-tool gating (Med-High) |
| MR | L | Per-repo policy composition + atomic-merge data integrity (Med-High) |
| RET | L | Embedding-dim lock-in; BM25/ColBERT/Qdrant parity behind seams (Med) |
| MCP | M–L | Write-call mutation safety; stdio subprocess sandboxing (Med-High) |
| SBX | L | Container escape + kernel isolation correctness (High) |
| POL | L | Policy totality = security boundary (High) |
| AUT | M–L | Determinism + side-effect isolation; condition consolidation (Med) |
| INT | L–XL | Adapter fidelity + marketplace supply-chain/policy gating (Med-High) |
| SEC | L | SSO/SCIM/RBAC auth boundary; no-plaintext-at-rest (High) |
| OBS | M–L | Live streaming + codec-server auth (Med) |
| EVAL | M–L | Reproducible scoring; multi-agent grading stability (Med) |
| UI | L | CRDT collaborative editing correctness (Med) |
| INF | L–XL | HA/data-loss-free reschedule; chart-vs-build ownership boundary (Med-High) |
| DOC | M | Mostly content; portal/i18n plumbing (Low-Med) |
| PM | M | Schema additions without breaking the event log (Med) |
| GEN | M | LLM output must meet template baseline on golden gate (Med) |

**Cross-cutting risks:** (1) **Protocol leakage** — any V1 caller assuming FSM/pgvector/Docker
specifics blocks the corresponding swap; audit callers for Protocol-only usage before each spine
item. (2) **Spine ordering** — building a fan-out before its spine (e.g. D-MR-6 before D-MR-1,
D-MA-3 before D-MA-1) wastes rework. (3) **Default-off discipline** — every High/Med-High item
ships flagged off with abuse tests so partial rollout never degrades the V1 baseline. (4) **Scope
creep into non-goals** — the expression-language and auto-remediation temptations recur; reject
them at review.

## 10. Key files / paths

Roll-up of the primary targets per theme (full detail in each numbered slice's §10):

- **WF:** `packages/workflow-engine/{temporal/,saga/}`, `apps/worker/temporal_worker.py`,
  `apps/api/app/api/v1/workflow.py`, `apps/web/app/(app)/workflow/editor/`, `deploy/helm/temporal/`.
- **MA:** `packages/agent-runtime/multi_agent/{supervisor.py,subagent.py,patterns.py}`,
  `apps/api/app/models/agent.py` (`SubAgentRun`).
- **MR:** `packages/agent-runtime/multi_repo/`, `apps/api/app/models/task.py` (`repo_targets`),
  `apps/api/app/services/traceability.py`, `packages/workflow-engine/saga/cross_repo_merge.py`.
- **RET:** `packages/knowledge-core/src/knowledge_core/search/{bm25_paradedb.py,colbert_reranker.py,qdrant.py}`,
  `packages/knowledge-core/src/knowledge_core/sync/mcp_indexer.py`,
  `packages/knowledge-core/src/knowledge_core/chunking/binary.py`, `apps/worker/tasks/knowledge.py`.
- **MCP:** `apps/api/app/services/mcp_gateway/{write.py,stdio.py,prompts.py,elicitation.py,ratelimit.py}`.
- **SBX:** `packages/agent-runtime/sandbox/{provider.py,docker.py,k8s_job.py,firecracker.py,gvisor.py,warm_pool.py}`,
  `deploy/socket-proxy/`, `deploy/helm/templates/{podsecurity,networkpolicy}.yaml`.
- **POL:** `packages/contracts/conditions/`, `packages/repo-policy/{conditional.py,env_matrix.py,inheritance.py,nested_merge.py}`,
  `apps/api/app/api/v1/policy.py`, `apps/api/app/services/verification/static_gate.py`.
- **AUT:** `packages/forge-automation/{engine.py,triggers.py,aggregates.py,actions/}`,
  `apps/api/app/models/automation.py`, `apps/worker/schedules.py`.
- **INT:** `packages/pm-adapters/{protocol.py,sync_engine.py,jira.py,linear.py,asana.py,monday.py,conformance.py}`,
  `packages/providers/gitlab/`, `apps/api/app/services/notifications/email.py`,
  `apps/api/app/services/integrations/{datadog,sentry,pagerduty,grafana}.py`,
  `apps/api/app/services/marketplace/`.
- **SEC:** `apps/api/app/services/auth/{saml.py,scim.py,rbac.py}`,
  `apps/api/app/services/secrets/{vault.py,external_secrets.py}`,
  `apps/api/app/services/audit/merkle.py`, `apps/api/alembic/versions/*_audit_immutability_trigger.py`.
- **OBS:** `deploy/grafana/`, `apps/web/app/(app)/analytics/`, `apps/web/components/trace/`,
  `apps/api/app/api/v1/trace.py` (export + WS), `traceability_rollup_history` migration,
  `apps/api/app/services/temporal/codec_server.py`.
- **EVAL:** `packages/verification/runners/`, `packages/eval-harness/`, `apps/web/app/(app)/eval/`,
  `apps/api/app/api/v1/eval.py`.
- **UI:** `apps/web/app/(app)/{board,traceability,sprints,settings/notifications}/`,
  `apps/web/components/approval/KnowledgeProvenancePanel.tsx`, Slack interaction path in `apps/api`.
- **INF:** `deploy/helm/`, `deploy/terraform/`, `deploy/scripts/install.sh`, `deploy/argocd/`,
  edge rate-limit middleware in `apps/api`.
- **DOC:** `docs/` portal config, `docs/operators/`, `docs/install/`, demo runner in `apps/web`.
- **PM:** `apps/api/app/services/sprint/`, `apps/api/app/models/sprint.py`, `apps/web/app/(app)/sprints/`.
- **GEN:** `packages/agent-runtime/skills/{spec_generator.py,postmortem_composer.py,tests_to_criteria.py}`.

## 11. Research references

- Consolidated backlog source of truth: [`SPEC-FUTURE-deferred-scope.md`](./SPEC-FUTURE-deferred-scope.md)
  (125 `D-*` requirements, themes overview, cross-cutting sequencing, out-of-scope).
- Intent + architecture: [`docs/FORGE_SPEC.md`](../../FORGE_SPEC.md) — Phased Roadmap (Phase 1/2/3),
  Technology Stack rows (Workflow engine V1 FSM → V2 Temporal; keyword search Postgres FTS → BM25;
  sandbox Docker → Firecracker; background jobs Celery → Temporal activities), Core Data Model.
- [`docs/forge-research-report.md`](../../forge-research-report.md) — Hybrid Retrieval (RRF k=60,
  weighted hybrid, pgvector < 1M vectors → external store), Supervised multi-agent (deterministic
  policy-driven supervisor mandate), Temporal vs LangGraph durable-execution split.
- Temporal vs LangGraph (durable workflow vs agent state machine, June 2026):
  https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines
- Temporal self-hosting: https://docs.temporal.io/self-hosted-guide · LangGraph:
  https://langchain-ai.github.io/langgraph/
- ParadeDB `pg_search` (true BM25 in Postgres): https://github.com/paradedb/paradedb · ColBERT v2:
  https://github.com/stanford-futuredata/ColBERT · Qdrant: https://github.com/qdrant/qdrant
- Numbered owning slices (per-item §1–§11 detail): `v2/F17`–`v2/F26`, `v3/F27`–`v3/F35`,
  `v3/F30`/`F33`, `cross-cutting/F36`–`F39`, and `v1/F05`/`F09`/`F10`/`F12` as referenced per theme in §3.

## 12. Out of scope / future

Inherited verbatim from `SPEC-FUTURE-deferred-scope.md` "Out of scope for THIS follow-up":

**A. Cross-slice ownership handoffs (already owned by a numbered slice).** Not re-specified here;
tracked in the owning slice's §1–§11. Representative: LLM `SpecGenerator` runtime wiring → **F06**
(the Protocol swap is D-GEN-1); top-level FSM/retry/escalation → **F07**; PEV→PR→approval +
authoritative `run_checks` + request-changes re-run → **F08**/**F06**/**F07**; central audit
table/writer/chain-verifier/viewer → **F39**; approval-gate primitive + UI → **F36** (interrupt/
`resume` → **F06**); the V1 hybrid retrieval pipeline + spec/plan/validation indexing boost →
**F05**; MCP gateway/connector layer + per-call audit + tool-call policy → **F09**; GitHub App
mirror sync / PR-CI-review events / push creds / `render_pr_body` → **F03**; run-trace viewer +
`agent_steps` read/write model + redaction/truncation/MinIO overflow → **F10**/**F06**;
skill-profile authoring/registry + `instructions_profile` resolution → **F11**/**F06**;
materializing `SPEC-NN-*` artifacts into the repo tree → **F06**; health endpoints + `forge-cli`
+ `CREATE EXTENSION vector` + single-node compose → **F00 substrate**/**F14**/**F37**;
self-hosting guide set + `verify-restore.sh`/`verify-upgrade.sh` + dev compose/Make → **F15**/**F13**;
GitHub OAuth sign-in + RFC 8707 token binding + encrypted Postgres vault → **F37**; Slack slash
commands/button approvals/identity link + demo App wiring → **F16**/**F03**.

**B. Deliberate, permanent non-goals (will NOT be built).**
- Expression language beyond declarative `ConditionGroup` — policy/automation evaluation stays
  total + non-Turing-complete (F29). `ConditionGroup` is the ceiling.
- Editing bundled YAML definitions in place — bundled workflow/policy definitions stay read-only;
  customization is always fork-into-workspace; benchmark/golden suites stay file-authored in git,
  not via an in-UI editor (F28/F35).
- Authoring new guards/effects without a code change + redeploy — the visual editor composes
  registered names only and injects no behavior (F28).
- Watchtower-style automatic image updates — disabled in favor of deliberate, backed-up,
  digest-pinned upgrades (F14); marketplace package updates are likewise always admin-reviewed (F32).
- Incident auto-remediation without human approval — permanently requires human approval; D-POL-9's
  bounded relaxed-posture profile is the only sanctioned relaxation (F17).
- Auto-approval of approval gates without a human decision — every gate needs a human; D-POL-5's
  SLA escalation only re-routes to another human (F36).
- Password / email-magic-link authentication — human login is OAuth / enterprise SSO only (F37);
  WS-Federation / LDAP / Active Directory direct bind is not planned (integrate via SAML/OIDC+SCIM, F33).
- Video walkthroughs / screencast assets — an explicit docs non-goal (F15).

**C. Beyond-spec exploratory ideas.** None. Every requirement in this slice traces to a harvested
§12 item with a cited originating `F-id`; nothing is invented beyond the slices' stated deferrals.
