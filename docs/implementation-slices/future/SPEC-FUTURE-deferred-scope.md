# Forge — Deferred Scope (Follow-up Spec)

> **Purpose.** This is the single, consolidated backlog spec for everything the 39
> V1/V2/V3 implementation slices explicitly pushed out of their own shippable scope.
> Each slice ends with a `## 12. Out of scope / future` section; this document harvests
> all of those deferrals, de-duplicates the items that several slices defer in common,
> and re-states each as a concrete, buildable requirement with acceptance criteria so the
> backlog can be planned and estimated rather than re-discovered slice-by-slice.
>
> **How this was generated.** The §12 ("Out of scope / future") block of every slice
> under [`docs/implementation-slices/`](../INDEX.md) (`v1/F01–F16`, `v2/F17–F26`,
> `v3/F27–F35`, `cross-cutting/F36–F39`) was extracted into a flat item list keyed by the
> originating feature id. Items were then grouped into coherent themes, near-duplicates
> were merged (e.g. *MCP sync-and-index*, *container/Firecracker sandboxing*, *multi-repo
> execution*, *Temporal migration*, *advanced reranking* are each deferred by 3–8 slices),
> and every merged requirement records **all** originating `F-id`s. Sources of truth for
> intent and architecture are [`docs/FORGE_SPEC.md`](../../FORGE_SPEC.md) (esp. the
> *Phased Roadmap*) and [`docs/forge-research-report.md`](../../forge-research-report.md).
>
> **Relation to the V1 build and the INDEX.** This spec does **not** restate work that is
> already owned by a numbered slice — when slice A says "X is owned by slice B", X belongs
> to B's §1–§11, not here (see [§ Out of scope for THIS follow-up](#out-of-scope-for-this-follow-up)).
> What remains here is genuine *new* capability that no current slice fully owns, plus
> explicit *future enhancements* layered on top of a shipped slice. The phased roadmap in
> [`FORGE_SPEC.md`](../../FORGE_SPEC.md) and [`INDEX.md`](../INDEX.md) terminates at V3
> (F35); items below are tagged **V2** / **V3** where they fall inside that roadmap and
> **Post-V3** where they are beyond the current 39-slice horizon (a true follow-on backlog
> to be sequenced opportunistically once the V1–V3 critical path lands).

**Phase legend.** `V2` = Phase 2 (Depth) · `V3` = Phase 3 (Scale) · `Post-V3` = beyond the
current roadmap horizon (follow-on backlog). Requirement ids are `D-<THEME>-<n>`.

---

## Themes overview

| Theme | Code | #Items | Target phase span | Primary owning slice(s) |
|---|---|---:|---|---|
| Workflow Engine V2 & Durable Execution | `WF` | 7 | V2 → Post-V3 | F25, F28 |
| Multi-Agent Orchestration | `MA` | 6 | V3 → Post-V3 | F27 |
| Multi-Repo Execution | `MR` | 7 | V2 → Post-V3 | F22 |
| Advanced Retrieval & Knowledge | `RET` | 8 | V2 → Post-V3 | F20, F05 |
| MCP Gateway & Protocol Features | `MCP` | 6 | V2 → Post-V3 | F09 |
| Sandboxing & Isolation | `SBX` | 6 | V2 → Post-V3 | F19, F34 |
| Advanced Policy & Governance | `POL` | 10 | V2 → Post-V3 | F29, F31 |
| Automations & Rule Engine | `AUT` | 10 | V2 → Post-V3 | F21 |
| Integrations & Marketplace | `INT` | 9 | V2 → Post-V3 | F18, F32 |
| Enterprise Security, SSO & Secrets | `SEC` | 6 | V2 → Post-V3 | F33, F30 |
| Observability & Audit (deep) | `OBS` | 9 | V2 → Post-V3 | F38, F10 |
| Evaluation & Benchmarking | `EVAL` | 8 | V2 → Post-V3 | F12, F35 |
| Advanced UI & Collaboration | `UI` | 8 | V2 → Post-V3 | F23, F01 |
| Deployment, Scaling & Infrastructure | `INF` | 10 | V2 → Post-V3 | F24 |
| Docs Platform & Demo Experience | `DOC` | 5 | V2 → Post-V3 | F15, F13 |
| Sprint & Project Management Depth | `PM` | 7 | V2 → Post-V3 | F26 |
| LLM-Backed Generation | `GEN` | 3 | V2 → Post-V3 | F02, F17 |
| **Total** | | **125** | | |

---

## Deferred requirements by theme

### WF — Workflow Engine V2 & Durable Execution

Forge V1 runs the top-level task lifecycle on a Postgres-backed FSM behind a
`WorkflowEngine` Protocol (F07). The research report's recommended end state is a hybrid:
Temporal for durable top-level orchestration, LangGraph for agent routing. This theme
carries the Temporal swap and the workflow-authoring surface that rides on top of the
stable state vocabulary.

- **D-WF-1 — Temporal-backed durable workflow engine.**
  Replace the V1 durable FSM with Temporal behind the existing `WorkflowEngine` Protocol,
  preserving the state vocabulary, guard semantics, and transition table; route *new* runs
  to the Temporal backend while keeping the DSL/evaluator unchanged.
  - *Originating:* F06, F07, F12, F17, F18, F25 (owner), F28.
  - *Target phase:* V2.
  - *Dependencies:* F07 `WorkflowEngine` Protocol + state vocab; F08 effect dispatch.
  - *Acceptance criteria:*
    1. A task can be driven `created → … → merged` end-to-end on the Temporal backend with
       identical observable transitions to the FSM backend (same `workflow_transition` rows).
    2. Killing and restarting the worker mid-run resumes the workflow with no lost or
       duplicated effects (exactly-once effect dispatch verified by an audit assertion).
    3. Backend is selectable per-run via config (`postgres_fsm` | `temporal`) with no DSL change.

- **D-WF-2 — Incident workflow definition on the shared engine.**
  Add the `alert_received → … → postmortem_created` incident workflow as a separate
  definition loaded by the same DSL parser, runnable on either backend (FSM or Temporal)
  (subsumes F01's deferred board-level "incident workflow states").
  - *Originating:* F01, F07, F17, F25.
  - *Target phase:* V2.
  - *Dependencies:* D-WF-1 (for Temporal variant); F17 incident intake; F07 DSL parser.
  - *Acceptance criteria:*
    1. The incident definition validates through the same loader/validator as the feature
       workflow with zero engine code changes.
    2. An `IncidentAlert` drives the incident workflow to `postmortem_created` with all
       transitions recorded.

- **D-WF-3 — Workflow visual editor.**
  A canvas to author, diff, validate, and version `WorkflowDefinition`s (compose registered
  guard/effect names only; inject no behavior), exporting the canonical YAML interchange format.
  - *Originating:* F07, F21, F27, F28 (owner).
  - *Target phase:* V3.
  - *Dependencies:* `GET /workflow/definitions/{name}` graph data (F07/F28); registry of guards/effects.
  - *Acceptance criteria:*
    1. A definition edited on the canvas round-trips to identical YAML accepted by the V1 loader.
    2. Saving an invalid graph (unknown guard, unreachable state) is rejected with a field-level error.
    3. Each save produces a new immutable revision with a visual diff against the prior revision.

- **D-WF-4 — Branching / multi-step action trees in workflows & rules.**
  Replace flat ordered action lists with `if/else` action trees plus delays-between-actions,
  authored in the visual editor.
  - *Originating:* F21, F28.
  - *Target phase:* V3.
  - *Dependencies:* D-WF-3; D-AUT-1 (rule engine).
  - *Acceptance criteria:*
    1. A branch condition evaluating false skips its subtree and executes the `else` branch.
    2. A configured delay defers the next action by the specified duration (verified in a replay).

- **D-WF-5 — Workflow dry-run simulation / what-if preview.**
  Feed sample events through a draft definition to preview the resulting path without executing effects.
  - *Originating:* F28.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-WF-3; deterministic guard evaluation.
  - *Acceptance criteria:*
    1. Simulating a sample event sequence returns the ordered state path and the guards/effects that *would* fire.
    2. No real effect (PR, notification, agent run) is dispatched during simulation (asserted via effect sink spy).

- **D-WF-6 — Runbook saga with step-level compensation.**
  Compensating actions and partial rollback for sequential runbook execution, beyond V1's
  fail-and-re-propose flow.
  - *Originating:* F17.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-WF-1 (Temporal saga primitives preferred); F17 runbook executor.
  - *Acceptance criteria:*
    1. A failure at step *N* triggers compensations for steps `1..N-1` in reverse order.
    2. Compensation completion leaves the system in a recorded, consistent terminal state.

- **D-WF-7 — Operational Temporal maturity.**
  Live cut-over of already-running `postgres_fsm` workflows to Temporal, full Celery→Temporal
  migration of background work (indexers, syncers — incl. F20's MCP-index poll moving from
  Celery Beat to a durable Temporal cron, indexer body unchanged), and config-only Temporal Cloud
  connection (TLS + API key).
  - *Originating:* F20, F25.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-WF-1.
  - *Acceptance criteria:*
    1. An in-flight FSM run can be migrated to Temporal and resumed without losing state.
    2. Background jobs run as Temporal schedules/workflows with the prior cadence and no Celery beat dependency.
    3. Setting Temporal Cloud env (endpoint + API key + TLS) connects with no code change.

### MA — Multi-Agent Orchestration

V1 hardcodes `execution_mode = single_agent` (F06). This theme is the supervised
multi-agent surface and its enhancements; the research report is explicit that the
supervisor must be a deterministic, policy-driven router, not a prompted reasoning agent.

- **D-MA-1 — Supervised multi-agent mode (Supervisor + bounded subagents).**
  Deterministic supervisor, subagent spawning, `SubAgentRun`, and scoped specialist tools
  (e.g. implementer/reviewer/security roles), gated by `subagent_policy`.
  - *Originating:* F06, F08, F22, F25, F27 (owner).
  - *Target phase:* V3.
  - *Dependencies:* F06 agent loop; F04 policy (`allow_subagents`, role); F36 approval gates.
  - *Acceptance criteria:*
    1. A task with `execution_mode=supervised` spawns subagents per the selected pattern and
       joins their results into one `AgentRunResult`.
    2. A subagent attempting an action outside its scoped tool set is denied by the runtime gate.
    3. `SubAgentRun` rows are recorded and linkable from the parent run trace.

- **D-MA-2 — `max_parallel` subagent concurrency enforcement.**
  Enforce subagent concurrency limits in the coordinator (conditional rules may gate
  `spawn_subagent` by role/mode but not by instance count).
  - *Originating:* F04, F29.
  - *Target phase:* V3.
  - *Dependencies:* D-MA-1.
  - *Acceptance criteria:*
    1. With `max_parallel=K`, at most K subagents run concurrently; the K+1th queues until a slot frees.
    2. The limit is read from policy and a violation attempt is logged, not silently dropped.

- **D-MA-3 — Custom multi-agent pattern DSL beyond the five built-ins.**
  A `multi_agent` DSL block (and editor support) for authoring custom coordination patterns
  beyond the shipped Orchestrator-Worker / Sequential / Fan-out-in / Debate / Dynamic-handoff set.
  - *Originating:* F27 (+F28 visual editor).
  - *Target phase:* V3.
  - *Dependencies:* D-MA-1; D-WF-3.
  - *Acceptance criteria:*
    1. A custom pattern defined in the DSL executes with the same supervisor semantics as a built-in.
    2. Invalid pattern definitions are rejected at load time with a clear error.

- **D-MA-4 — LLM-reasoned adaptive planning & heuristic pattern selection.**
  Replace deterministic policy/plan routing with LLM-reasoned routing + runtime plan
  discovery (Adaptive Planning), and let the selector infer multi-domain tasks for
  `DYNAMIC_HANDOFF` without an explicit task hint.
  - *Originating:* F27.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-MA-1; D-EVAL-4 (to measure regressions vs deterministic routing).
  - *Acceptance criteria:*
    1. On the multi-agent golden set, adaptive routing matches or beats deterministic routing on the primary score.
    2. Auto-selected patterns are recorded with the selection rationale for audit.

- **D-MA-5 — Distributed subagent fan-out.**
  Run subagents as independent Celery tasks or Temporal child workflows across hosts with a
  durable join, beyond V1's in-process execution.
  - *Originating:* F27.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-MA-1; D-WF-1 (durable join).
  - *Acceptance criteria:*
    1. Subagents execute on separate workers and the supervisor joins all results durably across a worker restart.

- **D-MA-6 — AI-assisted merge-conflict resolution.**
  Automatically resolve branch-merge conflicts instead of detecting and escalating to a human.
  - *Originating:* F27.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-MA-1; D-MR-1 (cross-repo context where applicable).
  - *Acceptance criteria:*
    1. A conflicting merge produces an agent-proposed resolution that passes repo checks, still gated by human approval.
    2. If auto-resolution fails, the run falls back to the V1 escalate-to-human path.

### MR — Multi-Repo Execution

V1 targets a single `repo_target` and produces one PR per run. This theme makes a single
task span multiple `RepositoryConnection`s — the most-deferred capability in the harvest
(F02, F03, F05, F06, F08, F22, F23, F25, F27).

- **D-MR-1 — Single task across multiple repos.**
  Execute one task against multiple repos in a single run (`repo_targets` with >1 entry),
  opening and tracking one PR per repo.
  - *Originating:* F02, F03, F05, F06, F08, F22 (owner), F25.
  - *Target phase:* V2.
  - *Dependencies:* F06 agent loop; F08 PR flow; F03 mirrors; F04 per-repo policy.
  - *Acceptance criteria:*
    1. A task with two repo targets produces two PRs, each gated by its own repo policy.
    2. The run trace links both PRs to the single originating task.
    3. A merge gate failure on either repo blocks task completion.

- **D-MR-2 — Cross-repo retrieval merge & per-criterion evidence aggregation.**
  Merge hybrid-retrieval results across the task's repos, and aggregate validation evidence
  for a single acceptance criterion across repos via `EvidencePort`.
  - *Originating:* F05, F23.
  - *Target phase:* V2.
  - *Dependencies:* D-MR-1; F05 retrieval; F23 traceability projection.
  - *Acceptance criteria:*
    1. Retrieval for a multi-repo task returns a single fused ranked list spanning all target repos.
    2. A criterion satisfied by evidence in two repos shows aggregated evidence, not duplicate gaps.

- **D-MR-3 — Mixed-provider multi-repo (GitLab).**
  Support GitLab or mixed-provider repos within one task, gated on the GitLab adapter (see D-INT-3).
  - *Originating:* F22.
  - *Target phase:* V2.
  - *Dependencies:* D-MR-1; D-INT-3.
  - *Acceptance criteria:*
    1. A task spanning a GitHub repo and a GitLab repo opens a PR and an MR respectively, each policy-gated.

- **D-MR-4 — Cross-repo multi-agent fan-out.**
  Combine multi-repo execution with supervised multi-agent: per-repo implementer subagents
  plus a cross-repo reviewer, gated by `subagent_policy`.
  - *Originating:* F22, F27.
  - *Target phase:* V3.
  - *Dependencies:* D-MR-1; D-MA-1.
  - *Acceptance criteria:*
    1. Each target repo is handled by its own implementer subagent; a single reviewer subagent reviews all diffs.

- **D-MR-5 — Multi-service / cross-repo incident context.**
  Join incident context across multiple repos rather than F17's single primary `repo_id`.
  - *Originating:* F17.
  - *Target phase:* V2.
  - *Dependencies:* D-MR-1; F17 incident workflow.
  - *Acceptance criteria:*
    1. An incident can reference and retrieve context from >1 repo and propose remediation PRs across them.

- **D-MR-6 — Atomic cross-repo merge, coordinated promotion + automated rollback.**
  A true atomicity guarantee via merge-queue integration or a saga with automated cross-repo
  revert, replacing V2's pre-gate-plus-ordered-merge and human-escalation-on-partial-failure;
  plus coordinated promotion of an artifact spanning multiple repos as one atomic release
  (builds on D-POL-2's deploy gate).
  - *Originating:* F22, F31.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-MR-1; D-WF-6 (saga/compensation); D-POL-2 (promotion).
  - *Acceptance criteria:*
    1. If a later repo merge fails, already-merged PRs are automatically reverted, leaving all repos unmerged.
    2. The atomic outcome (all-merged or none-merged) is recorded in the audit log.
    3. A multi-repo release promotes all participating repos' artifacts together or none, as one atomic promotion.

- **D-MR-7 — Cross-repo dependency inference & per-repo divergent retry.**
  Auto-infer merge-ordering dependencies from code (beyond explicit `depends_on`), and retry
  only the failed repo while preserving passed repos' diffs.
  - *Originating:* F22.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-MR-1.
  - *Acceptance criteria:*
    1. Declared-free repos with a real code dependency are ordered correctly without manual `depends_on`.
    2. A failure in one repo retries only that repo; other repos' diffs are untouched.

### RET — Advanced Retrieval & Knowledge

V1 ships hybrid retrieval (pgvector + native Postgres FTS + RRF + Jina rerank, F05) plus
read-only MCP query-through (F09). This theme upgrades the keyword backend, reranking,
freshness, and source breadth.

- **D-RET-1 — MCP sync-and-index mode.**
  Periodically pull MCP resources (and telemetry) into the local pgvector/BM25 index instead
  of only querying live at runtime. *The single most-deferred retrieval item.*
  - *Originating:* F03, F05, F09, F17, F20 (owner).
  - *Target phase:* V2.
  - *Dependencies:* F09 gateway; F05 index; F04 `allowed_namespaces`.
  - *Acceptance criteria:*
    1. A configured MCP source is polled on its SLA interval and its resources appear as indexed, searchable chunks.
    2. Re-sync updates changed resources and removes deleted ones (no orphan chunks).
    3. Indexed MCP chunks carry `mcp://` `source_uri` provenance.

- **D-RET-2 — True BM25 keyword backend (ParadeDB `pg_search`).**
  Swap native Postgres FTS (`ts_rank_cd`) for real BM25 behind the `KeywordSearcher`
  interface; downstream slices inherit it with no change.
  - *Originating:* F05, F20.
  - *Target phase:* V2.
  - *Dependencies:* F05 `KeywordSearcher` interface.
  - *Acceptance criteria:*
    1. The BM25 backend is selectable via config and passes the existing retrieval contract tests.
    2. On the golden eval set, BM25 keyword recall is ≥ the FTS baseline.

- **D-RET-3 — ColBERT reranking + tunable recency-decay RRF leg.**
  Add token-level late-interaction reranking and a tunable recency-decay leg to RRF, tuned
  against the golden eval set.
  - *Originating:* F05.
  - *Target phase:* Post-V3.
  - *Dependencies:* F05 reranker interface; F12 golden eval set.
  - *Acceptance criteria:*
    1. ColBERT reranking is selectable behind the reranker interface and improves the primary RAGAS metric vs Jina on the golden set, or is gated off.
    2. The recency-decay weight is configurable and changes ranking deterministically for a fixed query.

- **D-RET-4 — Subscription-driven incremental re-index.**
  Use MCP `resources/subscribe` + `notifications/resources/updated` push for lower-lag
  re-indexing instead of SLA polling, once the gateway holds durable subscriptions.
  - *Originating:* F20.
  - *Target phase:* V3.
  - *Dependencies:* D-RET-1; durable MCP subscriptions in the gateway.
  - *Acceptance criteria:*
    1. An upstream resource update triggers re-index of only the affected resource within the configured latency budget.

- **D-RET-5 — Binary / non-text resource indexing.**
  Extract and index PDFs, images, and office docs (OCR/parsing) beyond text-only ingestion.
  - *Originating:* F20.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-RET-1.
  - *Acceptance criteria:*
    1. A PDF resource is parsed to text, chunked, and retrievable; an image is OCR'd and indexed.

- **D-RET-6 — Cross-source dedup + embedding-model migration tooling.**
  Deduplicate the same document reachable via both repo and MCP, and provide tooling to
  change `EMBEDDING_DIM` and re-index.
  - *Originating:* F20.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-RET-1.
  - *Acceptance criteria:*
    1. A document reachable via two sources yields a single deduped result with both provenances.
    2. Changing the embedding model re-indexes the corpus and search returns valid results at the new dimension.

- **D-RET-7 — Dedicated external vector store (e.g. Qdrant).**
  Move embeddings to a dedicated vector DB once the corpus exceeds roughly 1M vectors,
  behind the existing vector-store interface.
  - *Originating:* F05.
  - *Target phase:* Post-V3.
  - *Dependencies:* F05 vector-store interface.
  - *Acceptance criteria:*
    1. The external store is selectable via config and passes the retrieval contract tests with no caller change.

- **D-RET-8 — Knowledge-provenance traceability links.**
  Trace which retrieved knowledge chunks informed each spec acceptance criterion.
  - *Originating:* F23.
  - *Target phase:* Post-V3.
  - *Dependencies:* F05 retrieval; F23 traceability; D-UI-5 (surfacing).
  - *Acceptance criteria:*
    1. Each criterion shows the ranked chunk(s) that contributed to its satisfaction, linkable to `source_uri`.

### MCP — MCP Gateway & Protocol Features

V1 is read-only HTTP query-through with capability negotiation (F09). This theme completes
the MCP surface: writes, transports, and protocol features.

- **D-MCP-1 — Mutating (write) MCP tool calls.**
  Allow `allow_write=true` to actually execute mutations behind an admin gate +
  policy-override approval, enabling (gated) bidirectional/write-back sync.
  - *Originating:* F09, F20.
  - *Target phase:* V2.
  - *Dependencies:* F09 gateway; F36 approval; D-POL-1 (policy gate).
  - *Acceptance criteria:*
    1. A write tool call is denied unless `allow_write=true` AND an admin approval is recorded.
    2. Every executed mutation is audit-logged with payload hash.

- **D-MCP-2 — stdio transport for subprocess MCP servers.**
  Run subprocess MCP servers in the gateway with process sandboxing and lifecycle controls.
  - *Originating:* F09.
  - *Target phase:* V2.
  - *Dependencies:* F09 gateway; D-SBX-1 (process sandboxing).
  - *Acceptance criteria:*
    1. A stdio MCP server is launched, health-checked, queried, and cleanly terminated by the gateway.
    2. The subprocess runs under the configured sandbox/resource limits.

- **D-MCP-3 — Consume server-advertised prompts capability.**
  Actually use MCP `prompts` rather than only negotiating/reporting the capability.
  - *Originating:* F09.
  - *Target phase:* V2.
  - *Dependencies:* F09 gateway.
  - *Acceptance criteria:*
    1. A server-advertised prompt is retrievable and usable in an agent run, with provenance recorded.

- **D-MCP-4 — Server-side elicitation support.**
  Support interactive server elicitation instead of returning it as denied/unsupported.
  - *Originating:* F09.
  - *Target phase:* Post-V3.
  - *Dependencies:* F09 gateway; F36 human-in-the-loop interrupt.
  - *Acceptance criteria:*
    1. An elicitation request surfaces to the approver and the supplied value is returned to the server to continue the call.

- **D-MCP-5 — Per-connection rate limiting & quotas.**
  Add per-connection rate limits and quotas beyond timeouts and size caps (via the
  platform-wide rate-limiting capability, D-INF-9).
  - *Originating:* F09.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INF-9.
  - *Acceptance criteria:*
    1. A connection exceeding its configured rate is throttled with a typed error, not a hard failure of the run.

- **D-MCP-6 — Per-resource ACL propagation.**
  Mirror fine-grained upstream per-document permissions into retrieval-time filtering, beyond
  scoping by `allowed_namespaces`.
  - *Originating:* F20.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-RET-1; D-POL-1.
  - *Acceptance criteria:*
    1. A document the caller is not entitled to upstream is excluded from retrieval results at query time.

### SBX — Sandboxing & Isolation

V1 isolates agent work with git worktrees off the mirror. This theme is the
defense-in-depth isolation ladder (the second-most-deferred cluster: F03, F06, F08, F19,
F22, F25, F27, F34).

- **D-SBX-1 — Container (Docker) per-task sandbox isolation.**
  Docker-based per-task isolation behind a `SandboxProvider` Protocol, replacing bare
  worktrees for check/command execution; locked-down container model + socket-proxy.
  - *Originating:* F03, F06, F08, F19 (owner), F22, F25.
  - *Target phase:* V2.
  - *Dependencies:* F06 `SandboxCommandRunner`; F14 compose runtime.
  - *Acceptance criteria:*
    1. Agent commands execute inside a per-run container; the container is destroyed at run end.
    2. The container cannot reach the host Docker socket directly (proxy-mediated only).
    3. Resource limits (CPU/mem/time) are enforced and a breach terminates the run cleanly.

- **D-SBX-2 — Kubernetes per-task Job/Pod isolation.**
  Per-task `Job`/`Pod` with restricted `PodSecurityContext` and `NetworkPolicy`, replacing
  Docker-out-of-Docker on the K8s path (the K8s per-task launch path also deferred by F34).
  - *Originating:* F19, F34.
  - *Target phase:* V2.
  - *Dependencies:* D-INF-1 (Helm chart); D-SBX-1.
  - *Acceptance criteria:*
    1. Each task runs in its own Pod with a non-root security context and a default-deny NetworkPolicy.

- **D-SBX-3 — Firecracker / gVisor microVM isolation.**
  A `FirecrackerSandboxProvider` (and gVisor option) plugging into `SandboxProvider` for true
  microVM isolation per task and per subagent.
  - *Originating:* F06, F19, F25, F27, F34 (owner).
  - *Target phase:* V3.
  - *Dependencies:* D-SBX-1; D-INF-1.
  - *Acceptance criteria:*
    1. A run executes inside a Firecracker microVM and passes the existing sandbox contract tests.
    2. Kernel-level isolation is verified (host syscalls from the guest are mediated/blocked).

- **D-SBX-4 — Per-language curated sandbox images.**
  Additional curated images beyond python/node/go via an allowlist and new
  `FORGE_SANDBOX_IMAGE_*` env vars (incl. F34's per-runtime guest kernels / image curation).
  - *Originating:* F19, F34.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-SBX-1.
  - *Acceptance criteria:*
    1. A new language image registered via env+allowlist is selectable by skill profile and used for its runs.

- **D-SBX-5 — Warm sandbox snapshot/restore pool.**
  Snapshot/restore reuse of warm containers across runs as a perf optimization over
  disposable per-run containers (incl. F34's warm microVM pools / snapshot-restore).
  - *Originating:* F19, F34.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-SBX-1.
  - *Acceptance criteria:*
    1. Warm-pool runs show measurably lower cold-start latency than disposable runs, with isolation guarantees preserved between tenants.

- **D-SBX-6 — Runtime-level worker hardening + in-container file/git tools.**
  Sysbox/gVisor runtime hardening of the worker/daemon, and routing `read_repo`/`write_code`
  (and eventually git) through the container.
  - *Originating:* F19.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-SBX-1.
  - *Acceptance criteria:*
    1. File tools operate inside the sandbox boundary with no host filesystem access outside the mounted worktree.

### POL — Advanced Policy & Governance

V1 ships a flat, context-free declarative policy (F04) read-only in the UI. This theme is
the conditional engine and the governance surfaces layered on it.

- **D-POL-1 — Advanced conditional policy engine.**
  Rule expressions and time/branch/environment-conditional rules and per-environment
  matrices, replacing V1's flat declarative rules; `ConditionGroup` is the (deliberate) ceiling.
  - *Originating:* F04, F28, F29 (owner).
  - *Target phase:* V3.
  - *Dependencies:* F04 evaluator/loader/snapshot/router.
  - *Acceptance criteria:*
    1. A rule "if branch=`main` require reviewer Y" changes the decision based on the active branch.
    2. Evaluation is total and non-Turing-complete (no expression can loop/diverge); proven by the contract tests.
    3. The conditional decision composes with the existing flat policy without changing flat-only behavior.

- **D-POL-2 — Deployment gates, environment promotion & rollout strategies.**
  Promotion-workflow states, an environment registry, and approval routing atop the rule
  primitive, including the `kind=deploy` approval gate; progressive rollout strategies
  (canary / blue-green / percentage traffic-shifting with bake-time gates) behind a
  `DeployProvider` Protocol with non-GitHub providers (GitLab CI, Argo CD, Spinnaker,
  Kubernetes-native); and incident-driven auto-rollback wired into the F17 incident workflow.
  - *Originating:* F03, F08, F16, F29, F31 (owner).
  - *Target phase:* V3.
  - *Dependencies:* D-POL-1; F36 approval primitive; F08 PR flow; F17 incident workflow (auto-rollback).
  - *Acceptance criteria:*
    1. A promotion from `staging`→`prod` requires the configured `kind=deploy` approval before dispatch.
    2. Per-environment policy matrices apply the correct rule set per target environment.
    3. A canary/blue-green rollout shifts traffic per the configured steps and a failed bake-time gate halts and rolls back.
    4. A non-GitHub `DeployProvider` (e.g. GitLab CI or Argo CD) drives a gated deploy through the same primitive, and an incident can trigger an automatic rollback.

- **D-POL-3 — Task-level allowed/restricted action composition.**
  Overlay task-schema `allowed_actions`/`restricted_actions` onto the composed repo decision
  at the runtime tool gate.
  - *Originating:* F04, F06, F29.
  - *Target phase:* V3.
  - *Dependencies:* D-POL-1; F06 runtime tool gate.
  - *Acceptance criteria:*
    1. A task-level restriction narrows (never widens) the repo decision; an attempt to widen is ignored and logged.

- **D-POL-4 — Hard enforcement of `skill_profiles.allowed`.**
  A hard `422` when a task picks a disallowed skill profile, enforced in board/spec
  validation and at runtime (V1 only exposes `registry.exists()`/directive resolution).
  - *Originating:* F01, F02, F11.
  - *Target phase:* V2.
  - *Dependencies:* F11 registry; F01/F02 validation.
  - *Acceptance criteria:*
    1. Creating/running a task with a profile outside `allowed` returns `422` with a field error.

- **D-POL-5 — Advanced approval routing (multi-reviewer, escalation & delegation).**
  Support `min_approvals > 1` with quorum aggregation and CODEOWNERS-based reviewer routing
  (V1 supports `min_approvals = 1` only), plus time-based SLA escalation chains for unresolved
  approval gates and delegation / out-of-office reassignment of approvers (escalation routes to
  another human; it never auto-approves — see permanent non-goals).
  - *Originating:* F08, F36.
  - *Target phase:* V2.
  - *Dependencies:* F08 PR gate; F36 approval.
  - *Acceptance criteria:*
    1. A PR touching a CODEOWNERS path routes to the owning reviewer(s).
    2. With `min_approvals=2`, the gate stays blocked until two distinct approvals are recorded.
    3. An approval left unresolved past its SLA escalates to the next approver per the configured chain; an out-of-office approver's pending gates reassign to their delegate.

- **D-POL-6 — Cross-repo / workspace-level conditional policy inheritance.**
  Workspace-wide policy inheritance and multi-team RBAC-conditional rules.
  - *Originating:* F29.
  - *Target phase:* V3.
  - *Dependencies:* D-POL-1; D-SEC-2 (multi-team RBAC).
  - *Acceptance criteria:*
    1. A workspace-level rule applies to all member repos unless overridden by a more specific repo rule.

- **D-POL-7 — In-UI authoring/commit of policy & rules.**
  PR-to-policy editing and committing of `.forge/policy.yaml` and conditional rules from the
  UI (V1 UI is read/validate/manage-templates only).
  - *Originating:* F04, F29.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-POL-1; F03 PR backend.
  - *Acceptance criteria:*
    1. Editing policy in the UI opens a PR against `.forge/policy.yaml`; merge is required to take effect.

- **D-POL-8 — Nested AGENTS.md merging + PolicyProfile bootstrap.**
  Directory-scoped precedence merging beyond root and `.forge/AGENTS.md` (V1 loads the first
  hit only), and generating a starter `.forge/policy.yaml` PR from a `PolicyProfile` template.
  - *Originating:* F04.
  - *Target phase:* Post-V3.
  - *Dependencies:* F04 loader.
  - *Acceptance criteria:*
    1. A nested `AGENTS.md` deeper in the tree overrides/merges with the root per documented precedence.
    2. Selecting a `PolicyProfile` opens a PR seeding `.forge/policy.yaml`.

- **D-POL-9 — Bounded auto-remediation skill profile.**
  A separately-named skill profile with a relaxed posture allowing *bounded* auto-remediation
  (note: incident response *without* human approval remains a permanent non-goal — see out-of-scope).
  - *Originating:* F17.
  - *Target phase:* Post-V3.
  - *Dependencies:* F11 profiles; D-POL-1.
  - *Acceptance criteria:*
    1. The profile permits only the explicitly bounded remediation actions; everything else still requires approval.

- **D-POL-10 — Static-analysis enforcement of `forbidden_shortcuts`.**
  A lint-rule / static-analysis machine gate for `no_error_handling` (and similar), beyond
  V1's reviewer-checklist surfacing.
  - *Originating:* F11.
  - *Target phase:* Post-V3.
  - *Dependencies:* F08 verification service.
  - *Acceptance criteria:*
    1. A diff that swallows errors fails an automated check rather than only being flagged for the reviewer.

### AUT — Automations & Rule Engine

V1 persists the data automations will act on but ships no evaluator. This theme is the
board-level rule engine and its action breadth (deferred by F01, F03, F07, F11, F17, F26).

- **D-AUT-1 — Saved workflow automation rule engine.**
  A WHEN/IF/THEN evaluator for board-level automations (e.g. "when status=merged, close
  linked spec task"), kept deterministic and separate from the FSM.
  - *Originating:* F01, F07, F21 (owner).
  - *Target phase:* V2.
  - *Dependencies:* F01 board entities; F07 transition events.
  - *Acceptance criteria:*
    1. A rule fires its actions when its trigger+conditions match a board event.
    2. Rule evaluation is deterministic and side-effect-isolated (testable via a dry-run mode).

- **D-AUT-2 — Scheduled / time-based triggers.**
  A `SCHEDULED` trigger type (cron, recurring, relative-to-date), since V1/V2 automations are
  event-driven only; also backs scheduled/timed deployments (F31, e.g. "deploy at 02:00",
  "promote 24h after staging") and scheduled/continuous re-benchmarking (F35).
  - *Originating:* F21, F31, F35.
  - *Target phase:* V3.
  - *Dependencies:* D-AUT-1; Celery Beat (or D-WF-7 Temporal schedules).
  - *Acceptance criteria:*
    1. A cron-scheduled rule fires at the configured time without an inbound event.
    2. A scheduled deployment dispatches at its configured time via the same trigger.

- **D-AUT-3 — Cross-entity aggregate conditions.**
  Rollup conditions across multiple entities (e.g. all subtasks done, 3+ tasks blocked) beyond
  single-entity snapshot evaluation.
  - *Originating:* F21.
  - *Target phase:* V3.
  - *Dependencies:* D-AUT-1.
  - *Acceptance criteria:*
    1. A rule conditioned on "all subtasks complete" fires only when every subtask is complete.

- **D-AUT-4 — External-side-effect automation actions.**
  Webhook calls, external issue creation, and deploy triggers as rule actions, once V2 PM
  adapters, integration egress, and per-action policy exist.
  - *Originating:* F21.
  - *Target phase:* V3.
  - *Dependencies:* D-AUT-1; D-INT-1; D-POL-1 (per-action policy).
  - *Acceptance criteria:*
    1. A rule action fires an outbound webhook / creates an external issue, gated by policy and audit-logged.

- **D-AUT-5 — LLM-assisted automation actions.**
  Agent-class actions (e.g. summarize-and-comment) routed through the spec/workflow path,
  keeping the rule engine itself deterministic.
  - *Originating:* F21.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-AUT-1; F06 agent runtime.
  - *Acceptance criteria:*
    1. An "summarize-and-comment" action delegates to an agent run and posts the result, with the rule engine remaining deterministic.

- **D-AUT-6 — LLM-driven skill-profile suggestion.**
  Let a model suggest which skill profile fits a task (V1 selection is explicit/human/board-driven).
  - *Originating:* F11.
  - *Target phase:* V2.
  - *Dependencies:* F11 registry; F12 eval (to validate suggestions).
  - *Acceptance criteria:*
    1. The suggester proposes a profile with a confidence/rationale; selection remains overridable by a human.

- **D-AUT-7 — SLA/SLO breach auto-declaration of incidents.**
  Auto-declare incidents from SLO burn-rate alerts, feeding the same `IncidentAlert` intake.
  - *Originating:* F17.
  - *Target phase:* V2.
  - *Dependencies:* D-AUT-1; F17 incident intake; ops integrations (D-INT-4).
  - *Acceptance criteria:*
    1. A breaching SLO alert creates an incident via the standard intake with the alert as evidence.

- **D-AUT-8 — Sprint-event automations.**
  Rule-driven sprint lifecycle: auto-roll carryover on completion, auto-start on `start_date`.
  - *Originating:* F26.
  - *Target phase:* V2.
  - *Dependencies:* D-AUT-1; F26 sprint model.
  - *Acceptance criteria:*
    1. On sprint completion, incomplete tasks auto-roll to the next sprint per the rule.
    2. A sprint with a reached `start_date` auto-activates.

- **D-AUT-9 — Auto-merge as default posture (opt-in org policy).**
  Allow orgs to opt into Forge auto-merging (V1 defaults to human-merge-on-GitHub with
  `merge_pr` approval-gated and off).
  - *Originating:* F03.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-AUT-1; F36 approval; D-POL-1.
  - *Acceptance criteria:*
    1. With auto-merge enabled by org policy, a fully-approved PR merges without manual click; default remains off.

- **D-AUT-10 — Consolidate rule conditions onto the shared primitive.**
  Refactor `forge_automation` to consume `forge_contracts.conditions` (the `Condition` /
  `ConditionGroup` / `ConditionOp` shape lifted by F29) and delete the duplicate.
  - *Originating:* F21, F29.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-POL-1.
  - *Acceptance criteria:*
    1. `forge_automation` uses the shared condition primitive with no regression in the F21 test suite.

### INT — Integrations & Marketplace

V1 ships GitHub App + Slack notifications. This theme is every other external system and the
community marketplace (deferred across F01, F02, F03, F09, F13, F16, F17, F18, F21, F22, F28, F32).

- **D-INT-1 — External PM adapters: Jira & Linear.**
  `PMAdapter` Protocol + `PMSyncEngine` two-way sync mapping `ForgeTask` to Jira and Linear.
  - *Originating:* F01, F02, F03, F16, F18 (owner).
  - *Target phase:* V2.
  - *Dependencies:* F01 serializable `ForgeTask`; F03 GitHub App patterns; F16 notification contract.
  - *Acceptance criteria:*
    1. A Forge task creates/updates a corresponding Jira issue and Linear issue and reflects status changes both ways.
    2. The mapping is field-validated; unmapped fields are reported, not silently dropped.

- **D-INT-2 — Additional PM adapters: Asana & Monday.com.**
  New adapters on the same `PMAdapter` Protocol / `PMSyncEngine`.
  - *Originating:* F18.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INT-1.
  - *Acceptance criteria:*
    1. Asana and Monday adapters pass the same adapter conformance suite as Jira/Linear.

- **D-INT-3 — GitLab provider support.**
  GitLab repo + MR/CI/review events as an alternative to the GitHub App.
  - *Originating:* F03, F18, F22.
  - *Target phase:* V2.
  - *Dependencies:* F03 provider abstraction.
  - *Acceptance criteria:*
    1. A GitLab repo can be connected, mirrored, and have an MR opened and merge-gated like a GitHub repo.

- **D-INT-4 — Ops/observability integrations.**
  Datadog, Sentry, PagerDuty, and Grafana integrations (including PagerDuty on-call schedules
  and escalation policies, which F17 consumes but does not own), plus Prometheus/Alertmanager
  alert routing (F38 ships dashboards + recording rules but not alert routing).
  - *Originating:* F03, F16, F17, F18, F38.
  - *Target phase:* V2.
  - *Dependencies:* F16 notification contract; F17 incident intake.
  - *Acceptance criteria:*
    1. A Sentry/Datadog alert can drive an `IncidentAlert`; a PagerDuty page is sent to the current on-call.

- **D-INT-5 — Email notifications, approvals & digests.**
  An email integration subscribing to the shared `NotificationEvent` contract, including
  approval emails and digests (V1 ships Slack only).
  - *Originating:* F03, F16.
  - *Target phase:* V2.
  - *Dependencies:* F16 `NotificationEvent`.
  - *Acceptance criteria:*
    1. Assignee/reviewer events deliver email; an approval email contains a working approve/deep-link.

- **D-INT-6 — Integration marketplace.**
  Community-contributed MCP connectors, skill profiles, PM-adapter packages, and shareable
  rule/workflow/policy templates (beyond shipped example templates and in-repo
  `examples/automations/`); plus plugin-as-code installable custom tools (behind sandboxed
  execution + a hardened trust model), inter-package dependency graphs, a cross-workspace shared
  registry cache, opt-in privacy-gated ratings/reviews/install telemetry, and Sigstore keyless
  (OIDC) signing with a transparency log for package provenance (distinct from D-INF-5's Helm
  chart signing).
  - *Originating:* F09, F11, F21, F28, F32 (owner).
  - *Target phase:* V3.
  - *Dependencies:* F09 connectors; F11 skill SDK; F18 adapters; D-POL-1; D-WF-3 (template interchange); D-SBX-1 (plugin-as-code sandboxing).
  - *Acceptance criteria:*
    1. A community connector/skill/rule template can be published, discovered, and installed into a workspace.
    2. Installed artifacts are versioned and policy-gated before activation.
    3. A signed package's provenance verifies against the transparency log before activation, and a package declaring a dependency on another resolves it via the dependency graph.

- **D-INT-7 — Self-service GitHub App Manifest onboarding.**
  Self-service GitHub App registration via the App Manifest flow (V1 assumes a pre-registered,
  env-configured App).
  - *Originating:* F03.
  - *Target phase:* Post-V3.
  - *Dependencies:* F03 App integration.
  - *Acceptance criteria:*
    1. An operator can register a new GitHub App from the UI via the manifest flow without hand-editing env.

- **D-INT-8 — Deeper PM sync.**
  Threaded comments, attachments, subtask hierarchy, blocks/blocked-by dependency graph, and
  sprint/cycle/epic mapping (V1 adapters sync core task fields at issue grain).
  - *Originating:* F18.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INT-1.
  - *Acceptance criteria:*
    1. Comments, attachments, subtasks, and dependency edges round-trip between Forge and the external board.
    2. Forge sprints/milestones map to Jira sprints and Linear cycles/projects.

- **D-INT-9 — PM fidelity & two-way schema.**
  Full ADF rich-text round-trip (tables/panels/media), two-way label/state creation
  (auto-create missing external states/labels), and a point-and-click custom-field mapper UI.
  - *Originating:* F18.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INT-1; D-UI cross-ref.
  - *Acceptance criteria:*
    1. A Jira description with a table round-trips without loss.
    2. A missing external label/state is created from Forge; a custom field can be mapped via UI.

### SEC — Enterprise Security, SSO & Secrets

V1 ships the Auth & Secrets service (BYOK, OAuth login, API keys, RBAC — F37) and an
immutable audit log (F39). This theme is the enterprise hardening tier.

- **D-SEC-1 — Enterprise SSO (SAML + OIDC) + SCIM provisioning & platform auth hardening.**
  SAML and generic-OIDC enterprise SSO plus SCIM-based directory provisioning, including managed
  Forge-user→external-account auto-mapping (V1/F18 maps assignees by email match), SAML Single
  Logout fan-out across sessions, multiple IdPs per workspace, automated domain-ownership
  verification (DNS TXT / email challenge), team-scoped SCIM group→team mapping, SAML
  attribute-based fine-grained authz (delegated to D-POL-1), extended SCIM features
  (bulk/ETags/`/Me`/enterprise-user schema), and platform auth hardening (WebAuthn / hardware-key
  2FA on privileged flows; EdDSA + JWKS asymmetric session tokens with key rotation).
  - *Originating:* F18, F33 (owner), F37.
  - *Target phase:* V3.
  - *Dependencies:* F37 auth; D-SEC-2 (RBAC); D-POL-1 (attribute-based authz).
  - *Acceptance criteria:*
    1. A user can sign in via a SAML IdP; SCIM create/deactivate provisions/deprovisions the Forge account.
    2. External accounts auto-map to Forge users via the directory, not just email heuristics.
    3. A user can sign in via a generic OIDC enterprise connector through the same `external_identity` model.
    4. A workspace can federate more than one IdP, and domain-ownership verification (DNS TXT / email) gates federation before sign-in is allowed.
    5. WebAuthn / hardware-key 2FA is enforceable on privileged flows, and sessions issued as EdDSA tokens verify against the published JWKS.

- **D-SEC-2 — Multi-team workspace controls & full RBAC.**
  Multi-team workspace controls and a full scoped RBAC hierarchy, including workspace-defined
  custom roles with bespoke permission sets, an organization tier above workspace (multiple
  workspaces under one org with org-level admins), a dedicated `auditor` role with scoped
  self-audit reads, time-bounded "break-glass" role elevation gated by an approval-to-grant
  workflow, and migration of all remaining legacy `require_role` call sites onto the scoped gate.
  - *Originating:* F29, F30 (owner), F37, F39.
  - *Target phase:* V3.
  - *Dependencies:* F37 base RBAC; F01 workspace model; F36 approval (break-glass grant).
  - *Acceptance criteria:*
    1. Roles scoped at workspace/team/project levels gate actions per the role matrix.
    2. A workspace-defined custom role with a bespoke permission set gates actions exactly as its grants specify.
    3. A break-glass elevation grants a time-bounded role only after an approval is recorded, auto-expires at `expires_at`, and both the grant and its expiry are audit-logged.

- **D-SEC-3 — External secrets backend.**
  HashiCorp Vault secrets management and external-secrets-operator / sealed-secrets / Vault CSI
  integration (V1 uses the encrypted Postgres vault with an env-resident KEK; the Helm chart
  supports `existingSecret` as the integration point only); plus BYOK secret versioning /
  previous-value history (V1 stores the current ciphertext only).
  - *Originating:* F14, F24, F37.
  - *Target phase:* V2.
  - *Dependencies:* F37 vault interface; D-INF-1 (Helm).
  - *Acceptance criteria:*
    1. Secrets resolve from Vault/External Secrets with no plaintext at rest in the app database.
    2. A rotated BYOK secret retains prior versions, each retrievable for rollback/audit.

- **D-SEC-4 — Tamper-evident hash chaining, audit export & immutability hardening.**
  Merkle/append-only step proofs beyond the unique-seq, plus a database-level immutability
  trigger hardening `workflow_transition`/audit append-only enforcement (beyond repository-layer-only),
  external SIEM / syslog / OpenTelemetry export of audit events, WORM/object-lock enforcement on
  the archive bucket, and table partitioning / tiered (cold) storage for very large deployments.
  - *Originating:* F07, F10, F39.
  - *Target phase:* V2.
  - *Dependencies:* F39 audit log.
  - *Acceptance criteria:*
    1. Any post-hoc edit/delete of an audit or transition row is rejected by a DB trigger and detectable by the chain verifier.
    2. Audit events export to an external SIEM via syslog/OTLP, and the archive bucket enforces WORM/object-lock so archived events cannot be overwritten before retention expiry.

- **D-SEC-5 — Per-tenant Temporal namespace isolation.**
  Replace the single `forge` namespace + `workspace_id` scoping with namespace-per-tenant
  isolation as multi-tenant hardening.
  - *Originating:* F25.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-WF-1.
  - *Acceptance criteria:*
    1. Each tenant's workflows execute in a dedicated Temporal namespace with no cross-tenant visibility.

- **D-SEC-6 — Multiple Slack workspaces / Enterprise Grid.**
  Multi-workspace and Enterprise Grid org-install support (V1 assumes one Slack team per Forge workspace).
  - *Originating:* F16.
  - *Target phase:* Post-V3.
  - *Dependencies:* F16 Slack app.
  - *Acceptance criteria:*
    1. Two Slack teams can be linked to distinct Forge workspaces under one Enterprise Grid org install.

### OBS — Observability & Audit (deep)

V1 ships the run-trace viewer (F10), cost/observability primitives (F38), and the audit log
(F39). This theme is the cross-run analytics and deep trace surfaces.

- **D-OBS-1 — Cross-run project analytics, DORA, budgets & deep telemetry.**
  Aggregate cost trends, retry-rate charts, and p50/p95 latency across runs (Grafana/Prometheus),
  plus deployment DORA metrics (deployment frequency, lead time, change-fail rate, MTTR),
  multi-currency / FX cost normalization with budget enforcement (hard caps + overspend alerts),
  per-tenant / per-workspace Grafana dashboards, frontend RUM + distributed profiling, and
  Temporal-native metrics + durable-workflow tracing once the durable engine lands.
  - *Originating:* F10, F31, F38 (owner).
  - *Target phase:* V2.
  - *Dependencies:* F38 metrics; F10 read model; D-WF-1 (Temporal-native metrics); F31 deploy events (DORA).
  - *Acceptance criteria:*
    1. A project dashboard shows cost trend, retry rate, and p50/p95 latency over a selectable window.
    2. A DORA dashboard computes deployment frequency, lead time, change-fail rate, and MTTR from deploy audit events.
    3. Costs normalize to a single currency via FX, and a configured budget hard cap blocks or alerts on overspend.

- **D-OBS-2 — Online / canary production evaluation.**
  Live/canary production scoring with real-time metric collection (beyond the offline
  golden-suite gate).
  - *Originating:* F12, F38.
  - *Target phase:* Post-V3.
  - *Dependencies:* F12 eval; F38 observability; D-EVAL-2.
  - *Acceptance criteria:*
    1. A canary slice of production runs is scored live and surfaced alongside offline scores.

- **D-OBS-3 — Deep multi-agent trace visualization.**
  Timeline DAG visualization, sub-agent trace nesting, and per-subagent diff overlays beyond
  the basic `SupervisionPanel` (`RunTrace.agent_run_ids` is already forward-compatible).
  - *Originating:* F10, F27.
  - *Target phase:* V3.
  - *Dependencies:* D-MA-1; F10 trace viewer.
  - *Acceptance criteria:*
    1. A supervised run renders a nested DAG with each subagent's steps and diff overlay.

- **D-OBS-4 — Live run overlay on the workflow canvas.**
  Real-time highlighting of a running workflow's current state/edge via F10's event stream
  (V1 canvas authoring is static).
  - *Originating:* F28, F10.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-WF-3; F10 event stream.
  - *Acceptance criteria:*
    1. The canvas highlights the active state and last edge for a live run within the stream's latency budget.

- **D-OBS-5 — Trace export bundle + WebSocket bidirectional control.**
  Download a run as a JSON/HAR-like bundle, and add a WebSocket multiplexer for bidirectional
  control (V1 streams one-way SSE).
  - *Originating:* F10.
  - *Target phase:* Post-V3.
  - *Dependencies:* F10 read model.
  - *Acceptance criteria:*
    1. A run exports to a self-contained bundle that reproduces the trace offline.
    2. A WebSocket client can both receive trace events and send a control command (e.g. pause).

- **D-OBS-6 — Per-run skill-profile version pinning.**
  Snapshot the exact skill-profile body that governed an `AgentRun` for audit (analogous to
  F04's `RepoPolicySnapshot`).
  - *Originating:* F11.
  - *Target phase:* Post-V3.
  - *Dependencies:* F11 registry; F39 audit.
  - *Acceptance criteria:*
    1. A run records an immutable snapshot of its skill profile; later edits to the profile do not alter the snapshot.

- **D-OBS-7 — Historical coverage/trend snapshots + org rollup.**
  Time-series snapshots of traceability rollups (`traceability_rollup_history`) for
  coverage-over-time and gap burndown, plus workspace cross-project/org rollup dashboards and
  org-level audit-event aggregation/analytics across workspaces.
  - *Originating:* F22, F23, F26, F39.
  - *Target phase:* Post-V3.
  - *Dependencies:* F23 projection; D-UI-4; F39 audit log (org audit aggregation).
  - *Acceptance criteria:*
    1. Coverage-over-time renders from periodic rollup snapshots.
    2. An org dashboard aggregates traceability/quality across all projects.
    3. An org dashboard aggregates audit events across all workspaces.

- **D-OBS-8 — Temporal codec server for Web UI.**
  A trusted, authenticated codec-server endpoint so operators can decrypt encrypted Temporal
  payloads in the Web UI.
  - *Originating:* F25.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-WF-1.
  - *Acceptance criteria:*
    1. An authenticated operator can view decrypted payloads in the Temporal Web UI; unauthenticated access is denied.

- **D-OBS-9 — Incident analytics dashboards.**
  MTTR/MTTA/remediation-accept-rate dashboards consuming F17's audit and timeline data.
  - *Originating:* F17.
  - *Target phase:* Post-V3.
  - *Dependencies:* F17 incident timeline; F38.
  - *Acceptance criteria:*
    1. The dashboard computes MTTR, MTTA, and remediation-accept-rate over a selectable window.

### EVAL — Evaluation & Benchmarking

V1 ships the golden test set + offline eval harness (F12). This theme broadens languages,
metrics, multi-agent grading, and public benchmarking.

- **D-EVAL-1 — Pluggable per-language verification toolchains.**
  Per-language runners (TypeScript, Go, …) and multi-language coverage parsers (V1 ships only
  the Python exit-code/`pytest --cov` golden-set toolchain).
  - *Originating:* F06, F08, F11.
  - *Target phase:* V2.
  - *Dependencies:* F08 verification service; F11 canonical step list.
  - *Acceptance criteria:*
    1. A TypeScript task runs its lint/test/coverage steps via the pluggable runner and reports coverage.

- **D-EVAL-2 — A/B evaluation harness UI.**
  A cross-encoder/embedding A/B evaluation UI consuming emitted metrics (`forge eval ab diff`
  exists in V1 as CLI + metric table).
  - *Originating:* F05, F12.
  - *Target phase:* V2.
  - *Dependencies:* F12 metrics; F05 retrieval metrics.
  - *Acceptance criteria:*
    1. Two configs can be compared side-by-side with per-metric deltas and significance in the UI.

- **D-EVAL-3 — Public benchmark suite & leaderboard.**
  A public benchmark suite and evaluation leaderboard, with materialized leaderboard snapshots
  and rank-over-time timelines, scheduled/continuous re-benchmarking and canary boards, dedicated
  multi-agent benchmark task categories (coordinator overhead, routing quality), cryptographically
  signed submission provenance (signed replay bundles), a cross-instance federated leaderboard,
  and cost/latency-gated rubric dimensions.
  - *Originating:* F11, F12, F35 (owner), F27.
  - *Target phase:* V3.
  - *Dependencies:* F12 harness; F32 marketplace; F27 (for multi-agent entries); D-AUT-2 (scheduled re-benchmarking).
  - *Acceptance criteria:*
    1. A submitted run is scored against the public suite and ranked on the leaderboard reproducibly.
    2. A submission's signed provenance verifies before it is ranked, and rank-over-time renders from materialized snapshots.
    3. A scheduled re-benchmark run republishes the board, and submissions from a second instance aggregate into the federated leaderboard.

- **D-EVAL-4 — Multi-agent golden cases & supervised-run grading.**
  Golden multi-agent tasks added to the eval harness, and grading extended to supervised
  multi-agent runs (V1 grades single-agent `agent_task` scope only).
  - *Originating:* F12, F27.
  - *Target phase:* V3.
  - *Dependencies:* D-MA-1; F12 harness.
  - *Acceptance criteria:*
    1. A supervised run is graded end-to-end against a multi-agent golden case.

- **D-EVAL-5 — Expanded metrics.**
  Embedding-based answer-similarity and the full RAGAS suite beyond the four core metrics.
  - *Originating:* F12.
  - *Target phase:* Post-V3.
  - *Dependencies:* F12 harness.
  - *Acceptance criteria:*
    1. The harness reports embedding answer-similarity plus the full RAGAS metric set on the golden suite.

- **D-EVAL-6 — Auto-generated golden cases.**
  Promote sanitized real production workflow runs into golden cases via the recorder.
  - *Originating:* F12.
  - *Target phase:* Post-V3.
  - *Dependencies:* F12 recorder.
  - *Acceptance criteria:*
    1. A sanitized production run can be promoted to a deterministic golden case and re-run identically.

- **D-EVAL-7 — Rich A/B experiment-tracking dashboards.**
  Experiment-tracking dashboards beyond the basic diff/metric table.
  - *Originating:* F12.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-EVAL-2.
  - *Acceptance criteria:*
    1. Experiments are tracked over time with history, tags, and comparison across more than two arms.

- **D-EVAL-8 — SAST & dependency-audit tool backends.**
  Implement the `run_sast` and `audit_dependencies` tool backends for the security role.
  - *Originating:* F27.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-MA-1 (security role); F08 verification.
  - *Acceptance criteria:*
    1. A security-role subagent runs SAST and dependency audit and reports findings into the run trace.

### UI — Advanced UI & Collaboration

V1 board is keyboard-first, optimistic single-writer. This theme is collaboration, the
traceability/provenance surfaces, and richer approval/notification UX.

- **D-UI-1 — Real-time presence & collaborative editing.**
  Multi-cursor presence and conflict-free collaborative editing of descriptions and workflow
  drafts (V1 uses optimistic single-writer + version conflict; F28 enforces single last-writer drafts).
  - *Originating:* F01, F28.
  - *Target phase:* Post-V3.
  - *Dependencies:* F01 entities; D-WF-3 (draft editor).
  - *Acceptance criteria:*
    1. Two users editing the same description see each other's cursors and merge without lost writes.

- **D-UI-2 — Roadmap drag-to-reschedule with dependency propagation.**
  Interactive roadmap scheduling that propagates across dependencies (V1 roadmap is
  read-mostly view + placement).
  - *Originating:* F01.
  - *Target phase:* Post-V3.
  - *Dependencies:* F01 roadmap; dependency graph.
  - *Acceptance criteria:*
    1. Dragging a milestone reschedules dependent items per the dependency graph and shows the cascade.

- **D-UI-3 — Cross-workspace saved-filter sharing & org views.**
  Share saved filters across workspaces plus org-level views (V1 is workspace-scoped only).
  - *Originating:* F01.
  - *Target phase:* Post-V3.
  - *Dependencies:* F01 filters; D-SEC-2 (org scoping).
  - *Acceptance criteria:*
    1. A saved filter shared at org level is visible/usable in other workspaces per RBAC.

- **D-UI-4 — Spec validation / requirement-traceability dashboard.**
  A full requirement-traceability visualization dashboard (V1/F02 only exposes data via
  `/traceability`; the merge gate stays in F02/F08).
  - *Originating:* F02, F23 (owner).
  - *Target phase:* V2.
  - *Dependencies:* F02 `/traceability`; F08/F12 verdicts.
  - *Acceptance criteria:*
    1. The dashboard renders the requirement→test→evidence matrix with coverage and gaps per spec.

- **D-UI-5 — Approval UI knowledge-provenance panel.**
  A `KnowledgeProvenancePanel` surfacing retrieval `source_uri`/metadata (including `mcp://`)
  in the review/approval layer.
  - *Originating:* F05, F09.
  - *Target phase:* V2.
  - *Dependencies:* F36 approval UI; F05/F09 provenance.
  - *Acceptance criteria:*
    1. The approval view lists the knowledge sources that informed the run, each linkable to its `source_uri`.

- **D-UI-6 — Per-user notification preferences.**
  Granular per-user mute and DM-vs-channel controls (V1 routes by workspace/project +
  assignee/reviewer DMs).
  - *Originating:* F16.
  - *Target phase:* V2.
  - *Dependencies:* F16 routing.
  - *Acceptance criteria:*
    1. A user can mute an event type and choose DM vs channel; routing honors it.

- **D-UI-7 — Inline traceability-gap editing/annotation.**
  Edit, annotate, or assign gaps as tasks inline (V1 deep-links to the board's create-task flow).
  - *Originating:* F23.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-UI-4.
  - *Acceptance criteria:*
    1. A gap can be annotated and converted to an assigned task without leaving the dashboard.

- **D-UI-8 — Richer Slack UX.**
  Full `views.open` modal review dialogs and two-way agent chat threads driving the run (V1
  uses message buttons, ephemeral responses, deep links, and slash commands).
  - *Originating:* F16.
  - *Target phase:* Post-V3.
  - *Dependencies:* F16 interaction path.
  - *Acceptance criteria:*
    1. An approval opens a Slack modal with full context.
    2. A threaded reply can drive a run step (e.g. answer a clarification) and is recorded.

### INF — Deployment, Scaling & Infrastructure

V1 ships Docker Compose self-hosting (F14) and local quickstart (F13). This theme is the
Kubernetes path and cloud/scale infrastructure.

- **D-INF-1 — Kubernetes Helm chart + local kind/minikube dev.**
  Production HA/scaling Helm chart and local Kubernetes development via kind/minikube.
  - *Originating:* F13, F14, F19, F24 (owner).
  - *Target phase:* V2.
  - *Dependencies:* F14 images/contracts; F37 auth.
  - *Acceptance criteria:*
    1. `helm install` brings up a working Forge stack; CI verifies a classic Ingress deploy.
    2. A kind/minikube profile runs the stack locally.

- **D-INF-2 — Multi-node, HA, auto-scaling.**
  Multi-node, high-availability, auto-scaling, and cross-host restart (outside Compose's
  capabilities; the Kubernetes path).
  - *Originating:* F14, F24.
  - *Target phase:* V2.
  - *Dependencies:* D-INF-1.
  - *Acceptance criteria:*
    1. A pod failure reschedules to another node with no data loss; HPA scales the API on load.

- **D-INF-3 — Temporal & observability stack deployment.**
  Real configuration, dashboards, and worker wiring for Temporal/Prometheus/Grafana/Loki
  (V1 ships opt-in profile placeholders), including the Helm Temporal subchart.
  - *Originating:* F14, F24, F25.
  - *Target phase:* V2.
  - *Dependencies:* D-INF-1; D-WF-1.
  - *Acceptance criteria:*
    1. Enabling the observability/Temporal profile deploys working dashboards and a Temporal worker target.

- **D-INF-4 — Cloud images, managed datastores & Terraform.**
  Cloud-provider images, managed-datastore variants (RDS+pgvector, ElastiCache, S3), and
  Terraform modules (chart consumes external endpoints).
  - *Originating:* F14, F24.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INF-1.
  - *Acceptance criteria:*
    1. Terraform provisions managed datastores and the chart connects to them via external endpoints.

- **D-INF-5 — GitOps packaging & chart signing.**
  Argo CD/Flux manifests, an OCI publish pipeline, and chart signing/provenance.
  - *Originating:* F24.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INF-1.
  - *Acceptance criteria:*
    1. The chart is published as a signed OCI artifact and deployable via Argo CD/Flux.

- **D-INF-6 — One-line remote VM installer.**
  A `curl | sh` installer (`deploy/scripts/install.sh`) for VMs (production path, not local dev).
  - *Originating:* F13.
  - *Target phase:* Post-V3.
  - *Dependencies:* F14 compose stack.
  - *Acceptance criteria:*
    1. The installer stands up a working single-node Forge on a fresh VM in one command.

- **D-INF-7 — Gateway API default + multi-cluster/service-mesh.**
  Promote Gateway API `HTTPRoute` from experimental flag to default, and support
  multi-cluster/multi-region and Istio/Linkerd service-mesh topologies.
  - *Originating:* F24.
  - *Target phase:* V3.
  - *Dependencies:* D-INF-1.
  - *Acceptance criteria:*
    1. `HTTPRoute` is CI-verified as a default routing option.
    2. A multi-cluster topology is documented and deployable.

- **D-INF-8 — GPU scheduling for the bundled reranker.**
  GPU node-selectors/tolerations and model-server tuning (chart renders CPU-only today).
  - *Originating:* F24.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INF-1.
  - *Acceptance criteria:*
    1. The reranker deployment schedules onto GPU nodes via selectors/tolerations and serves with lower latency.

- **D-INF-9 — Platform-wide rate limiting & quotas.**
  Per-connection / per-tenant rate limits and quotas as a shared platform capability
  (consumed by D-MCP-5 and the API edge), including per-model-provider rate limiting at the BYOK
  call site and rate limiting on the audit-export endpoint.
  - *Originating:* F09, F37, F39.
  - *Target phase:* Post-V3.
  - *Dependencies:* none hard (edge middleware).
  - *Acceptance criteria:*
    1. A caller exceeding its quota is throttled with a typed 429-style response and the event is metered.
    2. Per-model-provider limits throttle outbound model calls, and the audit-export endpoint is rate-limited.

- **D-INF-10 — Materialized `milestone_progress` cache.**
  A cached `milestone_progress` table + periodic recompute, introduced only if read-time
  progress computation regresses on the `seed_demo_board` fixture.
  - *Originating:* F01.
  - *Target phase:* Post-V3 (conditional).
  - *Dependencies:* F01 roadmap query path.
  - *Acceptance criteria:*
    1. Roadmap progress reads from the cache and a recompute job keeps it within the staleness budget.
    2. Built only if a measured perf regression against `seed_demo_board` justifies it.

### DOC — Docs Platform & Demo Experience

V1 ships Markdown self-hosting docs (F15) and a zero-key quickstart/demo (F13). This theme is
the docs platform upgrade and the richer guided demo.

- **D-DOC-1 — Hosted versioned docs portal.**
  A Docusaurus/MkDocs portal with per-version docs, search, and i18n-readiness, replacing
  Git-host-rendered Markdown.
  - *Originating:* F15.
  - *Target phase:* V2.
  - *Dependencies:* F15 doc set.
  - *Acceptance criteria:*
    1. Docs build to a versioned site with working search and a version switcher.

- **D-DOC-2 — Operator runbooks + production Helm guidance.**
  Dedicated operator runbooks for the optional Prometheus/Grafana/Loki/Temporal services, and
  full HA/scaling Helm guidance (following the V1 preview reference chart).
  - *Originating:* F15.
  - *Target phase:* V2.
  - *Dependencies:* D-INF-1; D-INF-3.
  - *Acceptance criteria:*
    1. Each optional service has a runbook covering deploy, scale, backup, and failure recovery.

- **D-DOC-3 — Managed-datastore backup/restore + air-gapped/multi-region guides.**
  First-class backup/restore docs for operator-provided RDS/ElastiCache/S3, plus offline-mirror
  (air-gapped) and multi-region HA install guides.
  - *Originating:* F15.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-INF-4.
  - *Acceptance criteria:*
    1. A documented restore procedure recovers a managed-datastore deployment from backup.

- **D-DOC-4 — Docs localization.**
  Translate docs beyond English (V1 is English-only).
  - *Originating:* F15.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-DOC-1 (i18n-ready portal).
  - *Acceptance criteria:*
    1. At least one non-English locale builds and is selectable in the portal.

- **D-DOC-5 — Guided demo experience.**
  A scripted live agent-run walkthrough (requires `MODEL_PROVIDER_KEY`), demo knowledge
  indexing (requires a self-hosted embedding option + BYOK), and an `is_demo`-gated banner/tour
  on the board UI — all deliberately excluded from the zero-key quickstart.
  - *Originating:* F13.
  - *Target phase:* Post-V3.
  - *Dependencies:* F13 quickstart; F05 embeddings/BYOK.
  - *Acceptance criteria:*
    1. With a model key set, the demo runs a scripted agent run end-to-end.
    2. The demo workspace shows an `is_demo` banner and an optional guided tour.

### PM — Sprint & Project Management Depth

V1 ships sprints/milestones tables + basic assignment. This theme is velocity/burndown and
deeper planning (all from F26, with one F23 link).

- **D-PM-1 — Sprint velocity & burndown dashboards.**
  Velocity charts and burndown views over the `sprint_velocity` rollup.
  - *Originating:* F01, F26 (owner).
  - *Target phase:* V2.
  - *Dependencies:* F26 sprint model; F23/F38 projection.
  - *Acceptance criteria:*
    1. A sprint shows a burndown line and the project shows a velocity chart over recent sprints.

- **D-PM-2 — Working-day/holiday calendar.**
  A configurable working-day calendar (skip weekends/holidays, per-day capacity) for the ideal
  burndown line (V1 uses inclusive calendar days).
  - *Originating:* F26.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-PM-1.
  - *Acceptance criteria:*
    1. The ideal burndown line skips configured non-working days.

- **D-PM-3 — Capacity planning from member availability.**
  Per-assignee capacity, leave, and load balancing (V1 uses a single display/forecast
  `capacity_points`).
  - *Originating:* F26.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-PM-1.
  - *Acceptance criteria:*
    1. Per-member capacity accounts for leave and surfaces over/under-allocation.

- **D-PM-4 — Estimation scales + re-estimation history.**
  Fibonacci/T-shirt estimation scales and re-estimation history (V1 uses integer
  `tasks.estimate` points).
  - *Originating:* F26.
  - *Target phase:* Post-V3.
  - *Dependencies:* F26 estimate field.
  - *Acceptance criteria:*
    1. A task can be estimated on a configurable scale and its estimate history is retained.

- **D-PM-5 — Portfolio velocity + advanced trend charts.**
  Workspace portfolio dashboard aggregating velocity across projects, plus cumulative-flow
  diagrams and cycle-time/lead-time scatter charts.
  - *Originating:* F26 (cf. F23).
  - *Target phase:* Post-V3.
  - *Dependencies:* D-PM-1; D-OBS-7.
  - *Acceptance criteria:*
    1. A portfolio view aggregates velocity across projects; CFD and cycle/lead-time charts render from time-series.

- **D-PM-6 — Team-scoped parallel active sprints.**
  Per-team concurrent active sprints + cross-team rollup (V1 has one active sprint per
  project), adding `team_id` without breaking the event log.
  - *Originating:* F26.
  - *Target phase:* Post-V3.
  - *Dependencies:* D-SEC-2 (teams); F26 sprint model.
  - *Acceptance criteria:*
    1. Two teams can each have an active sprint in the same project; a rollup aggregates both.

- **D-PM-7 — Sprint-goal tracking vs acceptance criteria.**
  Link sprint goal to spec validation / acceptance criteria.
  - *Originating:* F26 (surface owned by F23).
  - *Target phase:* Post-V3.
  - *Dependencies:* D-UI-4; F23.
  - *Acceptance criteria:*
    1. A sprint goal links to its acceptance criteria and shows satisfied/unsatisfied status.

### GEN — LLM-Backed Generation

V1 ships deterministic Template generators behind Protocols. This theme swaps in real
LLM-backed implementations as the eval harness matures.

- **D-GEN-1 — LLM-backed `SpecGenerator`.**
  The real spec-analyst skill / LangGraph `SpecGenerator` behind the V1 Protocol (V1/F02 ships
  `TemplateSpecGenerator`; the LLM implementation is owned by the F06 agent runtime).
  - *Originating:* F02 (impl owned by F06).
  - *Target phase:* V2.
  - *Dependencies:* F02 `SpecGenerator` Protocol; F06 runtime; F12 eval.
  - *Acceptance criteria:*
    1. The LLM `SpecGenerator` produces specs that pass the spec-quality golden cases at or above the template baseline.

- **D-GEN-2 — LLM-backed postmortem composer.**
  Replace `TemplatePostmortemComposer` with an LLM-backed composer behind the
  `PostmortemComposer` interface.
  - *Originating:* F17.
  - *Target phase:* Post-V3.
  - *Dependencies:* F17 composer interface; F12 eval.
  - *Acceptance criteria:*
    1. The LLM composer produces a postmortem scored at or above the template baseline on the golden set.

- **D-GEN-3 — LLM-generated tests-to-criteria verdicts.**
  Generate tests-to-criteria mappings/verdicts via the spec-analyst agent (V1/F23 consumes
  verdicts owned by F06/F02).
  - *Originating:* F23 (verdicts owned by F02/F06).
  - *Target phase:* V2.
  - *Dependencies:* D-GEN-1; F23 dashboard.
  - *Acceptance criteria:*
    1. Generated tests-to-criteria mappings feed the traceability dashboard and match human-labeled mappings on the golden set within tolerance.

---

## Cross-cutting risks & sequencing

The deferred backlog has a small number of "spine" items that unlock large fan-outs. Build
the spine first; most other items become incremental.

1. **Temporal engine (D-WF-1) is the V2 spine.** It must land before — and is a hard
   dependency of — incident-on-Temporal (D-WF-2 variant), durable runbook sagas (D-WF-6),
   live cut-over + full migration (D-WF-7), Temporal-streaming outbound PM sync (D-INT-1
   enhancement), per-tenant namespaces (D-SEC-5), the codec server (D-OBS-8), and the Helm
   Temporal subchart (D-INF-3). It depends only on the V1 `WorkflowEngine` Protocol and
   state vocabulary (F07) being honored — *risk:* any V1 leakage of FSM-specific semantics
   into callers will block the swap. Keep all callers Protocol-only.

2. **Multi-repo execution (D-MR-1) precedes** every other MR item, cross-repo retrieval/
   evidence (D-MR-2), multi-service incidents (D-MR-5), cross-repo multi-agent (D-MR-4), and
   the atomic-merge work (D-MR-6/7). It also gates multi-repo evidence aggregation in the
   traceability dashboard (D-UI-4 / D-OBS-7). *Risk:* per-repo policy composition and
   one-PR-per-repo accounting must be correct before atomicity work begins.

3. **Supervised multi-agent (D-MA-1) precedes** concurrency enforcement (D-MA-2), custom
   patterns (D-MA-3), adaptive planning (D-MA-4), distributed fan-out (D-MA-5), conflict
   resolution (D-MA-6), cross-repo multi-agent (D-MR-4), multi-agent eval (D-EVAL-4), deep
   multi-agent traces (D-OBS-3), and SAST/dep-audit role backends (D-EVAL-8). *Risk:* the
   supervisor must stay deterministic/policy-driven (research-report mandate) — defer
   D-MA-4's LLM routing until D-EVAL-4 can measure regressions.

4. **Sandboxing ladder is strictly incremental:** Docker per-task (D-SBX-1) → K8s per-task
   (D-SBX-2, also needs the Helm chart D-INF-1) → Firecracker/gVisor (D-SBX-3). stdio MCP
   transport (D-MCP-2) and curated images (D-SBX-4) ride on D-SBX-1. Do not start D-SBX-3
   before D-SBX-1's `SandboxProvider` Protocol is stable.

5. **Conditional policy (D-POL-1) is the governance spine.** It precedes deployment gates
   (D-POL-2 / F31), task-level composition (D-POL-3), cross-repo/workspace policy (D-POL-6),
   external-side-effect automation actions (D-AUT-4), per-resource MCP ACLs (D-MCP-6), and
   the F21 condition consolidation (D-AUT-10). Multi-team RBAC (D-SEC-2 / F30) precedes both
   D-POL-6 and SSO/SCIM (D-SEC-1 / F33). *Risk:* keep evaluation total/non-Turing-complete —
   the expression-language temptation is an explicit non-goal (see below).

6. **Kubernetes Helm chart (D-INF-1) precedes** the K8s isolation (D-SBX-2), GPU reranker
   (D-INF-8), Gateway API/mesh (D-INF-7), external secrets operator (D-SEC-3), and the
   observability/Temporal stack deployment (D-INF-3). It consumes F14's images/contracts;
   *risk:* don't fork image/build ownership — the chart only consumes.

7. **MCP sync-and-index (D-RET-1) precedes** subscription-driven re-index (D-RET-4), binary
   indexing (D-RET-5), cross-source dedup (D-RET-6), and per-resource ACLs (D-MCP-6). It
   depends on the V1 MCP gateway (F09) and retrieval (F05). True BM25 (D-RET-2) is
   independent and can land in parallel behind the `KeywordSearcher` interface.

8. **Automation rule engine (D-AUT-1) precedes** scheduled triggers (D-AUT-2), aggregate
   conditions (D-AUT-3), external-side-effect actions (D-AUT-4), LLM-assisted actions
   (D-AUT-5), sprint automations (D-AUT-8), SLA auto-declare (D-AUT-7), and auto-merge default
   (D-AUT-9). The visual editor (D-WF-3) and integration marketplace (D-INT-6) provide the
   authoring/sharing surfaces; sequence them after the engine and adapters.

9. **Audit log (F39, V1) underpins** hash chaining + DB immutability (D-SEC-4) and per-run
   skill-profile snapshots (D-OBS-6). The eval harness (F12, V1) underpins the A/B UI
   (D-EVAL-2), leaderboard (D-EVAL-3), multi-agent eval (D-EVAL-4), and online eval (D-OBS-2).

10. **Highest-risk items** (build with extra review): D-POL-1/D-POL-2 (policy correctness =
    security boundary), D-SEC-1/D-SEC-2 (SSO/RBAC), D-MR-6 (atomic cross-repo merge — data
    integrity), D-MCP-1 (write MCP calls — mutation safety), and D-SBX-3 (kernel isolation).
    Each should ship behind a default-off flag with its own contract/abuse tests.

---

## Out of scope for THIS follow-up

The following are **not** new backlog and are intentionally excluded from this consolidation:

### A. Cross-slice ownership handoffs (already owned by a numbered slice)

Many §12 entries are "X is owned by slice Y" statements where the capability is already fully
specified in another V1–V3 / cross-cutting slice. These are tracked in that slice's §1–§11,
not re-specified here. Representative examples (originating note → owning slice):

- LLM `SpecGenerator` wiring at agent runtime → **F06** (the *Protocol* swap is D-GEN-1; the
  runtime wiring is F06's job).
- Top-level workflow FSM / retry / escalation orchestration → **F07**; PEV → PR → approval
  wiring, authoritative `run_checks`, request-changes re-run loop → **F08**/**F06**/**F07**.
- Central audit-log table/writer/query/chain-verifier/viewer → **F39**; emitting `AuditEvent`s
  via `AuditSink` → the originating slices.
- Approval-gate primitive + approval UI → **F36**; raising interrupt / `resume` → **F06**.
- Hybrid retrieval pipeline (pgvector+BM25+RRF+rerank) → **F05**; spec/plan/validation
  artifact indexing with the 1.4× boost → **F05**.
- MCP gateway/connector layer + per-call audit fields + MCP-specific tool-call policy → **F09**.
- GitHub App mirror sync / PR-CI-review events / push creds / `render_pr_body` → **F03**.
- Run-trace viewer UI + `agent_steps` read model / step write-contract, redaction, 64KB
  truncation, MinIO overflow → **F10** (read) / **F06** (write).
- Skill-profile authoring/registry + `instructions_profile` resolution → **F11** / **F06**.
- Materializing/committing spec & `SPEC-NN-*` artifacts into the repo tree → **F06**.
- Health endpoints (`/healthz`, `/readyz`), `forge-cli`, app source/migrations/`CREATE
  EXTENSION vector`, production single-node compose stack → **F00 substrate** / **F14** / **F37**.
- Self-hosting guide set + `verify-restore.sh`/`verify-upgrade.sh`, dev compose + Make targets
  → **F15** / **F13**.
- GitHub OAuth human sign-in, RFC 8707 token binding, encrypted Postgres vault → **F37**.
- Slack slash commands / button approvals / per-user identity link, GitHub App wiring in the
  demo → **F16** / **F03**.

### B. Deliberate, permanent non-goals (will NOT be built)

- **Expression language beyond declarative `ConditionGroup`** — arbitrary boolean/arithmetic/
  regex expressions are excluded to keep policy/automation evaluation total and
  non-Turing-complete (F29). `ConditionGroup` is the ceiling.
- **Editing bundled YAML definitions in place** — bundled workflow/policy definitions stay
  read-only source artifacts; customization is always fork-into-workspace; benchmark/golden
  suites likewise stay file-authored in git (frozen via CLI), not via an in-UI editor (F28/F35).
- **Authoring new guards/effects without a code change + redeploy** — the visual editor only
  composes registered names and injects no behavior; new predicates remain code changes (F28).
- **Watchtower-style automatic image updates** — intentionally disabled in favor of
  deliberate, backed-up, digest-pinned upgrades (F14). (Marketplace package updates are likewise
  always admin-reviewed — no unattended auto-apply, F32.)
- **Incident auto-remediation without human approval** — permanently requires human approval;
  D-POL-9's bounded relaxed-posture profile is the *only* sanctioned relaxation (F17).
- **Auto-approval of approval gates without a human decision** — every gate requires a human;
  D-POL-5's SLA escalation only re-routes to another human, never auto-approves (F36).
- **Password / email-magic-link authentication** — V1 is OAuth + API key only; human login is via
  social OAuth / enterprise SSO (D-SEC-1), not local passwords or magic links (F37).
- **WS-Federation / LDAP / Active Directory direct bind** — not planned; enterprises integrate via
  SAML/OIDC + SCIM through their IdP (F33).
- **Video walkthroughs / screencast assets** — an explicit docs non-goal (F15).

### C. Beyond-spec exploratory ideas

None. Every requirement above traces to a harvested §12 item with a cited originating `F-id`;
nothing was invented beyond the slices' own stated deferrals.
