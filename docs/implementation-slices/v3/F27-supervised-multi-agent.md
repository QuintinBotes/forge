# F27 — Supervised Multi-Agent Mode (Supervisor)

> Phase: v3 · Spec module(s): Multi-Agent Coordinator (`packages/multi-agent-coordinator/forge_coordinator`), Multi-Agent Orchestration (Supervisor pattern, Subagent Role Definitions, Multi-Agent Rules, Pattern Selection Guide), Tech stack "Multi-agent layer = LangGraph Supervisor pattern", Core Data Model `WorkflowRun → AgentRun → SubAgentRun[]`, Repo Policy `subagent_rules`, Task Schema `subagent_policy` / `execution_mode`, Workflow DSL `modes` · Status target: **Done** = a task with `execution_mode = supervised_multi_agent` runs through a **deterministic Supervisor** that (1) selects a coordination pattern from explicit policy/task config (never LLM judgement), (2) spawns context-isolated, role-scoped specialist subagents — each a reused F06 `ExecutionAgent` run in its own git worktree with a tool set scoped to its role × task policy × skill profile — bounded by `subagent_rules` (`allow_subagents`, `allowed_roles`, `max_parallel`), (3) merges code-producing outputs onto an integration branch (conflicts → human interrupt), (4) merges all outputs into **structured artifacts, not free-form chat**, (5) validates the merged result against the **approved spec** (not subagent agreement) and returns an `AgentRunResult` byte-compatible with the F08 verify→PR→approval flow, (6) persists one `sub_agent_run` row per subagent with nested traces, and (7) preserves every human-in-the-loop gate. Single-agent stays the default; this whole slice is opt-in. Suite green under ruff + mypy + pytest with ≥80% coverage on `forge_coordinator`.

---

## 1. Intent — what & why

Forge ships single-agent execution as the default and **supervised multi-agent as an optional, Phase-3, policy-controlled mode** (Product Vision; Core Design Principle #1: *"Default single-agent. Multi-agent is supervised, opt-in, and policy-controlled — never the default."*). F27 builds the **Multi-Agent Coordinator** named in the Product Scope table: *"Launches context-isolated specialist subagents when policy permits; merges outputs."*

The non-negotiable architectural stance, taken straight from the spec and the research report, is that the **Supervisor is deterministic**:

> Multi-Agent Rules: *"Supervisor makes routing decisions via explicit policy, not LLM judgement."*
> Research report (Multi-Agent Orchestration): *"Forge's multi-agent coordinator should be a deterministic supervisor, not a prompted reasoning agent — it should make routing, assignment, and context-passing decisions based on explicit policy, not LLM judgement."*

So the Supervisor is a LangGraph `StateGraph` whose nodes/edges contain **zero LLM calls** — pattern selection, subagent dispatch order, the maker-checker re-loop, merge, and finalize are all explicit Python predicates over typed state. The *subagents* are where the LLM work happens: each subagent is a **reused F06 `ExecutionAgent`** (we do not re-implement an agent loop) given a role-scoped tool registry, a role-scoped objective, and an isolated worktree + checkpoint thread.

This slice exists because, without it:
- `execution_mode = supervised_multi_agent` (Task Schema, Workflow DSL `modes.optional`) has no runtime — F06 hardcodes `single_agent` (see F06 §12).
- `WorkflowRun → AgentRun → SubAgentRun[]` (Core Data Model) has no producer of `SubAgentRun`.
- `subagent_rules` (F04 repo policy: `allow_subagents`, `allowed_roles`, `max_parallel`) and the F04 `spawn_subagent` `Decision` have an evaluator but **no enforcer** — F04 §12 explicitly defers `max_parallel` concurrency enforcement and role spawning to *"the multi-agent coordinator (V3)."*

What F27 borrows (research report): the **Supervisor / hierarchical control** pattern with explicit routing criteria (cite:112, cite:117), and the discipline that production teams **over-architect** multi-agent systems — *"the simplest pattern that fits the problem should be used"* (cite:114). F27 therefore makes Orchestrator-Worker (i.e. effectively single-agent) the degenerate default even *within* multi-agent mode, and only escalates to richer patterns when policy/task config asks for them.

Non-negotiables this slice enforces (Build Prompt constraints):
- The agent (here, the Supervisor or any subagent) **never self-assigns permissions or expands its own scope** — every subagent's tool set is frozen at spawn from `role_tools ∩ task.allowed_actions ∩ skill.allowed_actions`, and `spawn_subagent` itself is policy-evaluated before any subagent starts.
- **Spec-gated implementation** (Build Prompt #4): the supervised run only fires once F07 reaches `executing` (spec approved for feature-class work via F02/F07); F27 adds **no** path that bypasses spec gating, and F02's `assert_implementation_allowed` still guards each subagent's writes.
- **Human approval still gates all risky actions and merges** (Multi-Agent Rule; Build Prompt #5); merge conflicts and low aggregate confidence pause via `interrupt`. The coordinator only produces an integration branch — the merge to a base branch is still gated by the human PR approval owned by `cross-cutting/F36-human-approval-system` / `v1/F08-plan-execute-verify-pr-approval`.
- **Final acceptance validates against the approved spec, not subagent agreement** (Multi-Agent Rule).
- Subagents are **context-isolated** (Multi-Agent Rule: *"Each subagent receives scoped context only — isolated from other subagents' state"*).
- **Hybrid retrieval only** (Build Prompt #3): every subagent's `initial_context`/`context_refs` and the researcher role's `search_knowledge`/`read_knowledge` route through F05's hybrid pipeline (semantic + keyword + RRF + Jina rerank); F27 never fabricates or bypasses retrieval.
- **BYOK** (Core Principle #5): each subagent resolves its model key from the `cross-cutting/F37-auth-secrets-byok` per-workspace vault via `ModelConfig.api_key_ref` at call time; the raw key never enters supervision state, checkpoints, `sub_agent_run`, or logs.
- **MCP read-only default + immutable audit log** (Build Prompt #8, #9): `query_mcp` (researcher only) flows through F09's read-only gateway; every spawn, merge, reviewer verdict, conflict, and policy decision is written append-only to the `cross-cutting/F39-audit-log` audit log.

## 2. User-facing behavior / journeys

F27 has no rich UI of its own beyond a "supervision" extension to the run trace (the trace viewer is F10; approvals are F08). Its primary "caller" is the workflow FSM (F07), which fires `start_agent_run` for a `WorkflowRun` whose `execution_mode == supervised_multi_agent`.

1. **Opting in (human, on the board).** A task author sets `execution_mode = supervised_multi_agent` on a task whose repo policy has `subagent_rules.allow_subagents: true` and a non-empty `allowed_roles`/`max_parallel`. If the repo policy forbids subagents, the board surfaces a validation warning ("this repo does not permit subagents") sourced from F04 — the task can still be created but the run will escalate (see Journey E).

2. **Maker-Checker happy path (machine-initiated).** FSM reaches `task_ready → executing` and dispatches `start_agent_run`. The coordinator's `Supervisor.run` selects pattern `maker_checker` (because `allowed_roles ⊇ {implementer, reviewer}` and the task requests review), spawns an **implementer** subagent in worktree `forge/TASK-123/sa-implementer-1`, then a **reviewer** subagent that reads the implementer's branch read-only and emits a structured review verdict. Reviewer approves → coordinator merges the implementer branch onto integration branch `forge/TASK-123`, validates acceptance criteria against the merged tree, returns `AgentRunResult{status=succeeded, confidence, branch_name=forge/TASK-123, ...}`. The FSM advances to `verifying` exactly as it would for a single agent — F08 is unchanged.

3. **Sequential specialist pipeline.** For a `kind=feature` task configured for the full pipeline (`allowed_roles ⊇ {researcher, planner, implementer, tester, reviewer}`), the Supervisor runs them in dependency order: researcher produces a `research_brief` artifact → planner consumes it (not the researcher's raw steps) and produces a `plan` artifact → implementer + tester write code/tests on the integration branch → reviewer verdict gates the merge. Each handoff passes a **normalized artifact**, never free-form chat.

4. **Fan-out / fan-in (parallel independent subtasks).** When the plan decomposes into N independent implementer assignments and `max_parallel ≥ 2`, the Supervisor dispatches up to `max_parallel` subagents concurrently, each on its own branch off the integration HEAD, then performs a fan-in merge. A clean merge → continue; a conflict → `interrupt(needs_human_input)` with a `MergeConflict[]` report. No silent overwrite.

5. **Policy refusal / conflict (no fallback to scope expansion).** Task requests `supervised_multi_agent` but `subagent_rules.allow_subagents == false` (or `max_parallel == 0`, or a required role not in `allowed_roles`). The coordinator does **not** silently downgrade or widen scope: it records a `policy_conflict` and finalizes `status=awaiting_input` with reason `subagents_not_permitted` → FSM routes to `needs_human_input` → an admin can either relax policy (F04) or re-run the task as `single_agent`. (A workspace setting `multi_agent.fallback_to_single_agent` may be enabled to instead emit the `policy_conflict` event so F07 re-dispatches single-agent — off by default.)

6. **HITL interrupt mid-supervision.** A subagent itself interrupts (low confidence / restricted action requiring approval — F06 behavior). The coordinator surfaces the child interrupt as a coordinator-level `interrupt`, persists the supervision checkpoint, sets the parent `agent_runs.status = awaiting_input`, and waits. `Supervisor.resume(parent_id, HumanResumeInput(...))` resumes from the exact supervision super-step, which in turn resumes the specific child subagent from *its* checkpoint — no completed subagent re-runs.

7. **Maker-Checker reject loop (bounded).** Reviewer returns `verdict=changes_requested` with structured findings. If `review_loop_budget` remains, the Supervisor re-dispatches the implementer with the reviewer findings injected as a scoped artifact (the implementer never sees the reviewer's raw trace). On budget exhaustion it finalizes `awaiting_input` with the reviewer findings attached.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

The base `AgentRun`/`SubAgentRun`/`WorkflowRun` entities exist in the Core Data Model. F06 created `agent_runs` + `agent_steps`; F07 owns `workflow_run` (with `execution_mode`). This slice owns one Alembic migration `packages/db/forge_db/migrations/versions/xxxx_f27_multi_agent.py` that:

**Extends `agent_runs`** (the *parent* row of a supervised run is an ordinary `agent_runs` row with `execution_mode = supervised_multi_agent`; add coordinator-level columns):

| Column | Type | Notes |
|---|---|---|
| `pattern` | `text` null | `CoordinationPattern` value chosen by the selector (queryable; null for single-agent runs) |
| `is_supervisor` | `boolean` not null default `false` | `true` for the parent coordinator run; lets F10/queries split coordinator vs subagent rows |
| `supervision` | `jsonb` not null default `{}` | resolved `SupervisionPlan` + `MergeResult` summary (redacted); large blobs offloaded to MinIO ref |

(`execution_mode` already exists on `agent_runs.inputs`/the run via F06/F07; no new enum value needed beyond `supervised_multi_agent` which the Core Data Model already names.)

**Creates `sub_agent_run`** (the `SubAgentRun[]` of the Core Data Model — one row per spawned specialist):

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `parent_agent_run_id` | uuid FK `agent_runs.id` ON DELETE CASCADE | the supervisor run |
| `agent_run_id` | uuid FK `agent_runs.id` null | the **child** F06 `ExecutionAgent` run (its steps live in `agent_steps`); null until spawned |
| `workspace_id` | uuid FK `workspace.id` | tenant scoping |
| `assignment_id` | `text` not null | plan-local id (`sa-implementer-1`); unique `(parent_agent_run_id, assignment_id)` |
| `role` | enum `sub_agent_role` (`planner, researcher, implementer, tester, reviewer, security`) | reuses F04 `SubAgentRole` |
| `pattern` | `text` not null | the pattern this assignment belongs to |
| `ordinal` | `int` not null | execution order / fan-out index |
| `depends_on` | `jsonb` not null default `[]` | list of `assignment_id` (DAG edges) |
| `status` | enum `sub_agent_status` (`pending, running, succeeded, failed, blocked, skipped, awaiting_input`) | |
| `optional` | `boolean` not null default `false` | `true` ⇒ failure does not fail the parent (e.g. advisory `security`) |
| `objective` | `jsonb` not null | the scoped `AgentObjective` (redacted; key fields only — full obj offloaded to MinIO ref) |
| `artifact` | `jsonb` not null default `{}` | the **structured** role output (`SubAgentArtifact`; redacted) |
| `confidence` | `float` null | child run confidence |
| `branch_name` | `text` null | per-subagent branch |
| `merged` | `boolean` not null default `false` | whether this subagent's code change was merged onto integration |
| `token_usage` | `jsonb` not null default `{}` | `{prompt, completion, total, cost_usd}` |
| `error` | `jsonb` null | `{type, message}` (redacted) |
| `started_at` / `finished_at` | `timestamptz` null | |
| `created_at` | `timestamptz` default now | |

Indexes: `ix_sub_agent_run_parent (parent_agent_run_id, ordinal)`, unique `uq_sub_agent_run_assignment (parent_agent_run_id, assignment_id)`, `ix_sub_agent_run_workspace (workspace_id)`, `ix_sub_agent_run_child (agent_run_id)`.

**LangGraph checkpoint tables** — reuse F06's `langgraph` Postgres schema and `PostgresSaver`. The supervisor checkpoints under `thread_id = "sup:" + str(parent_agent_run_id)`; each subagent checkpoints under F06's `thread_id = str(child_agent_run_id)`. No new checkpoint migration (F06's `ensure_checkpoint_schema()` already runs in `make migrate`).

`agent_steps` is reused unchanged for the supervisor's own decisions: the coordinator writes `decision` steps (pattern selected, assignment dispatched, merge result, verdict) to `agent_steps` under the **parent** `agent_run_id`, with `node ∈ {select_pattern, dispatch, collect, merge, validate, finalize}`. F10's `parent_step_id` / `agent_run_ids[]` forward-compat fields (F10 §"Sub-agent trace nesting") are populated so the trace viewer can nest child traces under the dispatch step.

### 3.2 Backend (FastAPI routes + services/packages)

The single-agent routes from F06 (`GET /api/v1/agent-runs/{id}`, `POST .../resume`) are reused. This slice **extends the read response** and adds two read endpoints. All routes require auth (`agent-runner`/`member`+); workspace-scoped.

| Method | Path | Body / Query | Returns | Purpose |
|---|---|---|---|---|
| `GET` | `/api/v1/agent-runs/{id}` (extend) | — | `AgentRunRead` + `sub_agent_runs[]` summary + `pattern` when `is_supervisor` | unchanged for single-agent; adds supervision summary |
| `GET` | `/api/v1/agent-runs/{id}/subagents` | — | `list[SubAgentRunRead]` (role, status, branch, artifact, child run id) | drill-in for F10 nesting |
| `GET` | `/api/v1/agent-runs/{id}/supervision` | — | `SupervisionView` (`SupervisionPlan` + `MergeResult` + per-assignment status) | one call for the supervision panel |

`POST /api/v1/agent-runs/{id}/resume` (F06) is reused; the service branches on `is_supervisor` to call `Supervisor.resume` vs `ExecutionAgent.resume`.

Internal service wiring (consumed by F07's `start_agent_run` effect, *not* HTTP) — a **small extension to F06's `apps/api/forge_api/services/agent_runs.py`**:
- `create_and_enqueue(task_id, workflow_run_id, *, db)` (F06) branches on the run's `execution_mode`:
  - `single_agent` → enqueue Celery `agent.run` (F06, unchanged).
  - `supervised_multi_agent` → insert the **parent** `agent_runs` row with `is_supervisor=true, execution_mode=supervised_multi_agent`, then enqueue Celery `coordinator.run`.
  This is the only F06 code touched; the FSM (`start_agent_run` effect) is unchanged (it already dispatches by name and the agent runtime reads `execution_mode`).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

Package: **`packages/multi-agent-coordinator/forge_coordinator`** (module `forge_coordinator`). Implements the **same frozen `AgentRuntime` Protocol** as F06's `ExecutionAgent` so F07/F08 are agnostic to mode.

Celery tasks in `apps/worker/forge_worker/tasks/coordinator.py`:
```python
@celery_app.task(bind=True, name="coordinator.run", acks_late=True, max_retries=0)
def run_supervisor_task(self, parent_agent_run_id: str) -> dict: ...

@celery_app.task(bind=True, name="coordinator.resume", acks_late=True, max_retries=0)
def resume_supervisor_task(self, parent_agent_run_id: str) -> dict: ...
```
- `max_retries=0`, `acks_late=True`, status-guard idempotency (no-op if `status not in {pending, awaiting_input}`) — same rationale as F06; nested side effects (worktrees, branches, child runs) must not double-execute. Re-delivery replays from the supervision checkpoint.
- Runs on the dedicated `agent` queue (heavy: spawns N child agent runs). `visibility_timeout` ≥ `max_parallel × per-subagent ceiling`.
- Each child subagent's *own* execution is run **in-process** inside the supervisor (via `await child_agent.run(scoped_objective)`), not enqueued as a separate Celery task — this keeps one supervision checkpoint authoritative and avoids a distributed join. Parallel fan-out uses `asyncio.gather` bounded by an `asyncio.Semaphore(max_parallel)`. (A future revision may enqueue subagents as independent Celery tasks; out of scope — see §12.)

LangGraph supervisor graph (`forge_coordinator/graph.py`): `build_supervisor_graph(deps) -> CompiledStateGraph`. **No node calls an LLM.**

```
START → select_pattern → policy_gate → dispatch
dispatch  ─(run ready assignments, ≤ max_parallel, deps satisfied)→ collect
collect   ─(router_after_collect)→ dispatch        # more ready / handoff
                                 | merge            # all code-producing done
                                 | dispatch(reset)  # maker-checker reject + budget remains
merge     ─(router_after_merge)→ validate          # clean
                                | INTERRUPT(needs_human_input)   # conflicts
validate  → finalize
finalize  ─(router_after_finalize)→ END
                                  | INTERRUPT(needs_human_input) # low conf / required-role fail / policy override
```

Node responsibilities (all deterministic):
- **select_pattern** — `PatternSelector.select(objective, policy, subagent_rules, task_subagent_policy)` → `SupervisionPlan`. Pure table-driven mapping (see §4). Emits a `decision` step (`node=select_pattern`).
- **policy_gate** — evaluate `spawn_subagent` once per distinct role via F04 `PolicyEvaluator` + check `subagent_rules.allow_subagents`, `role ∈ allowed_roles`, and `max_parallel = min(policy.max_parallel, task.subagent_policy.max_parallel) ≥ 1`. On failure → record `policy_conflict` and route to finalize→interrupt (Journey E). Drops `optional` assignments whose role is disallowed; **blocks** if a required assignment's role is disallowed.
- **dispatch** — for each ready assignment (deps satisfied, not yet run), build a scoped `AgentObjective` (role tools ∩ task ∩ skill; isolated `initial_context` = predecessor artifacts only; per-subagent `RepoTarget` branch off integration HEAD), insert a `sub_agent_run` row (`status=running`), and run the child `ExecutionAgent`. Concurrency capped by `Semaphore(max_parallel)`. Emits one `decision` step per dispatch with `agent_run_ids[]` for nesting.
- **collect** — fold each `SubAgentResult` into supervision state; persist `sub_agent_run` (`status`, `artifact`, `confidence`, `branch_name`). A child `awaiting_input` short-circuits to coordinator interrupt.
- **merge** — `BranchMerger.merge(integration_branch, code_producing_results)` — sequential (pipeline) or fan-in merge of code-producing subagent branches onto the integration branch; returns `MergeResult` (with `conflicts[]`). Read-only roles (reviewer/researcher/security/planner) produce no branch to merge.
- **validate** — map each acceptance criterion → satisfied + evidence **against the merged integration tree** (not against subagent self-reports), gated by the reviewer verdict if a reviewer ran. Computes aggregate confidence = `min(required-subagent confidences)` clamped by reviewer verdict (rejected ⇒ ≤ threshold).
- **finalize** — assemble an `AgentRunResult` (branch_name = integration branch, changed_files/diff_stat from the merged tree, acceptance_criteria, verifications left to F08, token_usage = Σ subagents + supervisor). Interrupt if aggregate confidence < threshold, a required AC unsatisfied, a required subagent failed, or a `policy_conflict` was recorded.

State persistence: the supervisor checkpoints after each super-step (`PostgresSaver`, thread `sup:<id>`); every node writes `agent_steps` (parent run) via `deps.step_sink`; each subagent independently persists its `agent_steps` + checkpoint via the reused F06 runtime.

### 3.4 Frontend / UI (Next.js routes/components)

Mostly an **extension of F10's run trace viewer** (F10 §"Sub-agent trace nesting" is forward-compat for exactly this). F27 adds:
- `apps/web/components/runs/SupervisionPanel.tsx` — renders the `SupervisionView`: a DAG/timeline of subagents (role badge, status, confidence, branch), the chosen pattern, and the `MergeResult` (merged branches, conflicts). Clicking a subagent expands its nested trace (reuses F10 `RunTraceTimeline` keyed on the child `agent_run_id`).
- `apps/web/components/runs/SubAgentTraceGroup.tsx` — collapsible group nesting child steps under the parent `dispatch` step via `parent_step_id`.
- Data hooks (TanStack Query): `useSubAgents(runId)`, `useSupervision(runId)`.
- The F08 Review page's "Risk flags" panel gains a multi-agent note when `is_supervisor` (e.g. "reviewer subagent requested changes", "fan-in merge had conflicts resolved by <user>").

No new top-level route. If F10 is not yet extended, F27 ships the panel behind the existing run-detail route and degrades gracefully (the read endpoints work standalone).

### 3.5 Infra / deploy (compose, helm, caddy)

Reuses F06's worker image and volumes (`git`, `WORKTREE_ROOT`, `REPO_CACHE_ROOT`, `forge-worktrees`/`forge-repo-cache` named volumes, `agent` Celery queue). F27 adds env on the `worker` service (`deploy/docker-compose.yml` / `docker-compose.dev.yml`):
- `MULTI_AGENT_ENABLED=true|false` (default `false` — feature flag; supervised runs refuse with `multi_agent_disabled` when off, even if a task requests it).
- `MULTI_AGENT_MAX_PARALLEL_CAP=4` (hard upper bound on `max_parallel` regardless of policy, to cap cost/resource).
- `MULTI_AGENT_SUBAGENT_TIMEOUT_SECONDS=3600`, `MULTI_AGENT_REVIEW_LOOP_BUDGET=1`, `MULTI_AGENT_FALLBACK_TO_SINGLE_AGENT=false`.
- Worktree disk: N parallel subagents = N worktrees; document sizing in `docs/self-hosting/` (each subagent worktree ≈ repo size). No new service. Helm is V2/V3 elsewhere; no Caddy change.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

> Reused **as-is** from sibling slices (not redefined here): `AgentObjective`, `AgentRunResult`, `AcceptanceCriterion`, `AcceptanceCriterionResult`, `RepoTarget`, `PolicySnapshot`, `KnowledgeScope`, `RetrievedChunk`, `ModelConfig`, `TokenUsage`, `Step`, `HumanResumeInput`, `AgentRuntime` (F06 `forge_contracts.agent`); `SubAgentRole`, `SubagentRules`, `ToolCall`, `Decision`, `PolicyEvaluator` (F04 `forge_contracts.policy` / `forge_policy`); `SkillDirectives`, `to_directives`, `skill_permits_action` (F11 `forge_skills`); `ExecutionMode` (F07). New DTOs below are frozen in `packages/contracts/forge_contracts/coordinator.py`; internal types live in `forge_coordinator`.

**Frozen — implemented here, same Protocol as F06 so F07/F08 are mode-agnostic:**
```python
class AgentRuntime(Protocol):                       # identical to F06
    async def run(self, objective: AgentObjective) -> AgentRunResult: ...
    async def resume(self, agent_run_id: UUID, human_input: HumanResumeInput) -> AgentRunResult: ...
```

**Enums & role tool map (contracts):**
```python
from enum import StrEnum

class CoordinationPattern(StrEnum):
    ORCHESTRATOR_WORKER  = "orchestrator_worker"     # degenerate: one implementer (≈ single agent)
    SEQUENTIAL_PIPELINE  = "sequential_pipeline"     # research→plan→implement→test→review
    FAN_OUT_FAN_IN       = "fan_out_fan_in"          # parallel independent implementers
    MAKER_CHECKER        = "maker_checker"           # implementer + reviewer loop
    DYNAMIC_HANDOFF      = "dynamic_handoff"         # supervisor re-routes by explicit rules

# Spec "Subagent Role Definitions" → "Scoped Tools Only" (action vocabulary; F11 KNOWN_ACTIONS superset)
ROLE_TOOLS: dict[SubAgentRole, frozenset[str]] = {
    SubAgentRole.PLANNER:     frozenset({"read_repo", "read_spec", "read_knowledge", "write_spec"}),
    SubAgentRole.RESEARCHER:  frozenset({"read_repo", "search_knowledge", "query_mcp"}),
    SubAgentRole.IMPLEMENTER: frozenset({"read_repo", "write_code", "run_tests", "open_pr"}),
    SubAgentRole.TESTER:      frozenset({"read_repo", "write_tests", "run_tests"}),
    SubAgentRole.REVIEWER:    frozenset({"read_repo", "read_spec", "write_review_comment"}),
    SubAgentRole.SECURITY:    frozenset({"read_repo", "run_sast", "audit_dependencies"}),
}
CODE_PRODUCING_ROLES = frozenset({SubAgentRole.IMPLEMENTER, SubAgentRole.TESTER})
```

**Task-level subagent policy (contracts; mirrors Task Schema `subagent_policy`):**
```python
class SubagentPolicy(BaseModel):
    allowed: bool = False
    max_parallel: int = 0
```

**Plan / assignment / result DTOs (contracts):**
```python
class SubAgentAssignment(BaseModel):
    id: str                                  # "sa-implementer-1"
    role: SubAgentRole
    objective: str                           # role-scoped objective text
    acceptance_criteria: list[AcceptanceCriterion] = []   # subset relevant to the role
    allowed_actions: list[str]               # RESOLVED = role_tools ∩ task ∩ skill (never widened)
    context_refs: list[RetrievedChunk] = []  # ISOLATED scoped context for this role only
    depends_on: list[str] = []               # assignment ids (DAG edges)
    optional: bool = False                   # failure does not fail the parent run

class SupervisionPlan(BaseModel):
    pattern: CoordinationPattern
    assignments: list[SubAgentAssignment]
    max_parallel: int                        # resolved & capped
    review_loop_budget: int = 1
    merge_strategy: Literal["sequential_integration", "fan_in_merge", "read_only"]

class SubAgentArtifact(BaseModel):           # STRUCTURED output, never free-form chat (Multi-Agent Rule)
    kind: Literal["code_change", "review", "test_suite", "spec_draft",
                  "research_brief", "security_report"]
    summary: str
    # role-typed payload (validated per kind):
    review_verdict: Literal["approved", "changes_requested"] | None = None
    findings: list[str] = []                 # reviewer/security findings (structured)
    branch_name: str | None = None           # code_change / test_suite
    changed_files: list[str] = []
    report_ref: str | None = None            # MinIO ref (e.g. SARIF for security)

class SubAgentResult(BaseModel):
    assignment_id: str
    role: SubAgentRole
    agent_run_id: UUID                        # child F06 run id
    status: Literal["succeeded", "failed", "blocked", "skipped", "awaiting_input"]
    confidence: float
    artifact: SubAgentArtifact
    token_usage: TokenUsage

class MergeConflict(BaseModel):
    assignment_id: str
    path: str
    detail: str

class MergeResult(BaseModel):
    integration_branch: str
    head_sha: str | None
    merged_assignments: list[str]
    conflicts: list[MergeConflict] = []
    changed_files: list[str] = []
    diff_stat: dict[str, int] = {}

class SupervisionView(BaseModel):            # the read DTO for GET .../supervision
    parent_agent_run_id: UUID
    pattern: CoordinationPattern
    plan: SupervisionPlan
    subagents: list[SubAgentResult]
    merge: MergeResult | None
    aggregate_confidence: float | None
    policy_conflict: str | None
```

**Pattern selector (deterministic — no LLM):**
```python
class PatternSelector(Protocol):
    def select(self, *, objective: AgentObjective, policy: PolicySnapshot,
               subagent_rules: SubagentRules, task_subagent_policy: SubagentPolicy,
               directives: SkillDirectives) -> SupervisionPlan: ...

class DefaultPatternSelector:
    """Pure, table-driven (FORGE_SPEC 'Pattern Selection Guide'). NO LLM call.
       Selection precedence (first match wins):
         1. explicit task.subagent_policy / objective hint  -> that pattern verbatim
            (this is the ONLY path to DYNAMIC_HANDOFF in V3 — spec's "Complex multi-domain
             task" row; routing is still by explicit deterministic rules, never LLM judgement,
             see §12)
         2. directives.review_required and IMPLEMENTER+REVIEWER allowed -> MAKER_CHECKER
         3. kind == feature and full role set allowed        -> SEQUENTIAL_PIPELINE
         4. decomposable into >1 independent unit and max_parallel>=2 -> FAN_OUT_FAN_IN
         5. otherwise                                        -> ORCHESTRATOR_WORKER (single implementer)
       Assignments are filtered to subagent_rules.allowed_roles; allowed_actions resolved as
       role_tools ∩ task.allowed_actions ∩ (skill allowlist if non-empty)."""
```

**Coordinator (the Supervisor):**
```python
@dataclass
class CoordinatorDeps:
    agent_factory: Callable[[], AgentRuntime]          # builds a fresh F06 ExecutionAgent per subagent
    pattern_selector: PatternSelector
    workspace_manager: "SubAgentWorkspaceManager"      # per-subagent worktrees + integration branch
    merger: "BranchMerger"
    policy_evaluator: PolicyEvaluator                  # F04: spawn_subagent Decision
    checkpointer: "BaseCheckpointSaver"                # reuse F06 PostgresSaver
    step_sink: "StepSink"                              # reuse F06: Step -> agent_steps (parent run)
    sub_agent_sink: "SubAgentRunSink"                  # persists sub_agent_run rows
    object_store: "ObjectStore"                        # MinIO offload (objectives, artifacts, reports)
    db_session_factory: Callable[[], "AsyncSession"]
    settings: "CoordinatorSettings"

class Supervisor(AgentRuntime):
    def __init__(self, deps: CoordinatorDeps) -> None: ...
    async def run(self, objective: AgentObjective) -> AgentRunResult: ...
    async def resume(self, agent_run_id: UUID, human_input: HumanResumeInput) -> AgentRunResult: ...
```

**Internal helpers (`forge_coordinator`):**
```python
class SubAgentWorkspaceManager:
    """Creates per-subagent worktrees off the integration branch; reuses F06 WorktreeSandbox."""
    def __init__(self, sandbox: "WorktreeSandbox", settings: "CoordinatorSettings") -> None: ...
    async def ensure_integration_branch(self, repo: str, base_branch: str,
                                        integration_branch: str) -> str: ...   # -> base sha
    async def create_subagent_branch(self, repo: str, integration_branch: str,
                                     assignment_id: str) -> "WorktreeHandle": ...
    async def cleanup(self, handles: list["WorktreeHandle"], *, keep_branches: bool = True) -> None: ...

class BranchMerger:
    """Deterministic git merge of code-producing subagent branches onto integration.
       NEVER auto-resolves a conflict; returns conflicts for human interrupt."""
    async def merge(self, *, repo: str, integration_branch: str,
                    results: list[SubAgentResult], strategy: str) -> MergeResult: ...

class SubAgentRunSink(Protocol):
    async def create(self, row: "SubAgentRunCreate") -> UUID: ...
    async def update(self, sub_agent_run_id: UUID, **fields: Any) -> None: ...
    async def list_for_parent(self, parent_agent_run_id: UUID) -> list[SubAgentResult]: ...

class CoordinatorSettings(BaseModel):
    enabled: bool = False
    max_parallel_cap: int = 4
    subagent_timeout_seconds: int = 3600
    review_loop_budget: int = 1
    fallback_to_single_agent: bool = False
    confidence_threshold: float = 0.72
```

**Subagent objective scoping (the isolation contract):** for assignment `A`, the coordinator builds
`AgentObjective(..., objective=A.objective, acceptance_criteria=A.acceptance_criteria,
repo_target=<integration-derived per-subagent branch>, policy=PolicySnapshot(..., allowed_actions=A.allowed_actions,
restricted_actions=task.restricted_actions), initial_context=A.context_refs, knowledge_scope=<role-filtered>,
agents_md=<task agents_md>, skill_profile=<role-appropriate>, model=<task model>)` and runs it through a fresh `agent_factory()` `ExecutionAgent`. The child sees **only** `A.context_refs` (predecessor artifacts normalized into chunks) — never another subagent's `AgentState`, steps, or checkpoint.

## 5. Dependencies — features/slices that must exist first

| Ref | Why F27 needs it | Hard/Soft |
|---|---|---|
| `cross-cutting/F37-auth-secrets-byok` (`auth-sdk`/`forge_auth`) **+ Phase-0 monorepo baseline** | encrypted per-workspace **BYOK** vault + resolver for `ModelConfig.api_key_ref` (each subagent's model key, resolved at call time), `Principal`/`require_role(...)` RBAC on the new routes, the canonical `SecretRedactor` (reused by F06's `redaction.py`); **plus** the baseline `packages/contracts` `forge_contracts`, `packages/db` `forge_db` base session + `WorkspaceScopedModel` (host of `agent_runs`/`sub_agent_run`), and MinIO `ObjectStore` (objective / artifact / SARIF offload) that the monorepo provides | **Hard** |
| `v1/F06-single-execution-agent` (`agent-runtime`/`forge_agent`) | each subagent **is** a reused `ExecutionAgent` (`run`/`resume`); reuses `WorktreeSandbox`, `ToolRegistry.scoped`, `StepSink`, `PostgresSaver`/`ensure_checkpoint_schema`, `AgentObjective`/`AgentRunResult` contracts, the `agent` Celery queue, and the `create_and_enqueue` branch point | **Hard** |
| `v1/F04-repo-policy` (`policy-sdk`/`forge_policy`) | `SubagentRules` (`allow_subagents`/`allowed_roles`/`max_parallel`), `SubAgentRole`, `spawn_subagent` `Decision` (the policy gate F04 §12 explicitly defers to V3 coordinator), `ToolCall`/`Decision` | **Hard** |
| `v1/F11-skill-profiles` (`skill-sdk`/`forge_skills`) | `SkillDirectives` (`review_required` drives MAKER_CHECKER selection; `allowed_actions`/`forbidden_actions` compose into the resolved per-role tool set via `skill_permits_action`), `security-review` tools (`sast`/`dependency_audit`/`secrets_scan`) for the `security` role | **Hard** |
| `v1/F05-hybrid-knowledge-retrieval` (`knowledge-core`/`forge_knowledge`) | the researcher role's `search_knowledge`/`read_knowledge` backend = the spec-mandated **hybrid** pgvector+BM25+RRF+Jina-rerank pipeline (non-negotiable "retrieval is always hybrid"); `RetrievedChunk`/`KnowledgeScope` contracts; predecessor artifacts normalize into `RetrievedChunk`s for the scoped (non-chat) handoff | **Soft** — stub `KnowledgeStore.search` returns `[]` (matches F06) |
| `v1/F07-feature-workflow-fsm` (`workflow-engine`/`forge_workflow`) | `WorkflowRun.execution_mode`, the `start_agent_run` effect that triggers a run, `agent_completed`/`agent_low_confidence`/`policy_conflict` events the coordinator emits | **Hard (integration peer)** — F27 must run standalone (no FSM) for its own tests |
| `v1/F08-plan-execute-verify-pr-approval` | consumes the coordinator's `AgentRunResult` for verify→PR→approval; F08's `run_checks` re-verifies the **merged integration branch** independently (it does not trust subagent self-checks — F08 §3.3), so the coordinator returns `verifications=[]` and leaves the authoritative gate to F08; "human approval gates merge" enforced there | **Hard (integration peer)** — F27 returns the same contract; no F08 change required |
| `cross-cutting/F36-human-approval-system` (`approval-sdk`/`forge_approval`) | the canonical approval-gate primitive that **human-gates merges and risky actions** (non-negotiable); coordinator interrupts (merge conflict, low aggregate confidence, reviewer-reject-past-budget, restricted-action approval) surface via F07 `needs_human_input` and F08's `pr` gate, which resolve through this service — the integration branch never merges to base without it | **Hard (non-negotiable peer)** — F27 emits the gating signals; the gate lives in F36/F08 |
| `cross-cutting/F39-audit-log` (`forge_contracts.audit` + `forge_db` audit writer) | the **immutable, append-only audit log** (non-negotiable "every agent action, tool call, MCP call, and approval — immutable, queryable"); every subagent spawn, branch merge, reviewer verdict, conflict, and policy decision is audited with the child `agent_run_id`, secret-redacted before persistence | **Hard (non-negotiable)** |
| `v1/F10-run-trace-viewer` | subagent trace nesting UI (`parent_step_id`/`agent_run_ids[]` forward-compat already in F10) | **Soft** — read endpoints work standalone |
| `v1/F02-spec-engine` (`spec-engine`/`forge_spec`) | `read_spec`/`write_spec` backends for `planner`; acceptance criteria for validation | **Soft** — stub for non-feature kinds |
| `v1/F09-mcp-gateway-v1` (`mcp-sdk`/`forge_mcp`) | `query_mcp` backend for `researcher` (read-only gateway; `search_knowledge` is served by F05) | **Soft** — stub returns `[]` |
| `v1/F03-github-app` (`integration-sdk`/`forge_integrations`) | `open_pr` backend (implementer) + push creds; integration-branch base mirror | **Soft for tests** (local fixture repo), hard for real runs |
| Security tooling (`run_sast`/`audit_dependencies` tools, SARIF) | the `security` role's tools | **Soft** — `security` is `optional` in V3; stub/skip if absent |

External libs: `langgraph`, `langgraph-checkpoint-postgres`, `langchain-core`, `pydantic v2`, `sqlalchemy[asyncio]`, `celery` (all already pinned by F06). No new top-level dependency.

## 6. Acceptance criteria (numbered, testable)

1. **Mode routing.** A `WorkflowRun` with `execution_mode == supervised_multi_agent` causes `create_and_enqueue` to insert a parent `agent_runs` row with `is_supervisor == true` and enqueue `coordinator.run` (not `agent.run`); a `single_agent` run still enqueues `agent.run` (F06 unchanged).
2. **Deterministic pattern selection.** `DefaultPatternSelector.select` is pure and deterministic: identical inputs → identical `SupervisionPlan`; with `directives.review_required == true` and `allowed_roles ⊇ {implementer, reviewer}` it returns `pattern == MAKER_CHECKER`. **No LLM/model client is constructed in `select`** (assert the supervisor graph nodes never call `model_factory`).
3. **Maker-checker happy path.** Given scripted implementer + reviewer subagents (`agent_factory` returns a `ScriptedExecutionAgent`), `Supervisor.run` spawns exactly two `sub_agent_run` rows (`role=implementer` then `role=reviewer`), the reviewer reads (does not write) code, the implementer branch is merged onto the integration branch, and the result is `AgentRunResult{status="succeeded", branch_name == integration_branch}` with `diff_stat.files >= 1`.
4. **Role tool scoping (no self-expansion).** For an `implementer` assignment, the resolved `allowed_actions` equals `ROLE_TOOLS[implementer] ∩ task.allowed_actions ∩ skill_allowlist`; `query_mcp` is **absent** from an implementer's objective even when the task allows it (implementer cannot query MCP — Multi-Agent Rule), and `write_code` is **absent** from a `researcher`'s objective.
5. **Policy gate — subagents disallowed.** With `subagent_rules.allow_subagents == false`, `Supervisor.run` spawns **zero** subagents, records a `policy_conflict` step, and finalizes `status == "awaiting_input"` with `needs_human_reason == "subagents_not_permitted"`; the parent `agent_runs.status` is persisted accordingly.
6. **Policy gate — role not allowed.** A plan requiring `role=security` while `allowed_roles` excludes it: if the `security` assignment is `optional` it is `skipped` (recorded), the run still completes; if a **required** role is disallowed the run blocks (`awaiting_input`, reason names the role).
7. **`max_parallel` enforcement.** With `max_parallel = min(policy=2, task=3) = 2` and 3 ready fan-out implementer assignments, no more than 2 subagent runs are ever concurrently `running` (assert via a semaphore-instrumented `agent_factory` recording max concurrency); `MULTI_AGENT_MAX_PARALLEL_CAP` further clamps the effective value.
8. **Fan-in clean merge.** Two parallel implementers touching disjoint files → `BranchMerger.merge` returns `MergeResult{conflicts == []}`, both `sub_agent_run.merged == true`, and the merged tree contains both files.
9. **Fan-in conflict → human.** Two implementers editing the same lines → `merge` returns non-empty `conflicts[]`; the graph emits an `interrupt`, parent `status == "awaiting_input"`, a checkpoint exists for `thread_id == "sup:"+id`, and **no** partial/auto-resolved merge is committed.
10. **Context isolation.** A subagent's `AgentObjective.initial_context` contains only its assignment's `context_refs`; it never contains another subagent's steps or `AgentState` (assert the implementer's context equals the planner's *artifact* chunk, not the planner's raw trace).
11. **Structured handoff (not chat).** The planner→implementer handoff passes a `SubAgentArtifact{kind="spec_draft"|"research_brief"}` normalized into `RetrievedChunk`s; the raw child `agent_steps` of the predecessor are **not** forwarded (assert by content).
12. **Maker-checker reject loop (bounded).** Reviewer returns `review_verdict == "changes_requested"`; with `review_loop_budget == 1` the implementer is re-dispatched once with the reviewer `findings` injected as a scoped artifact, then on a second rejection the run finalizes `awaiting_input` with the findings attached (assert exactly 2 implementer `sub_agent_run` rows).
13. **Final validation against spec, not agreement.** Even when all subagents self-report success, `validate` marks an acceptance criterion `satisfied=false` if the **merged tree** lacks the evidence; the aggregate confidence is clamped ≤ threshold when a reviewer rejected, forcing finalize→interrupt (Multi-Agent Rule "Final acceptance validates against approved spec, not subagent agreement").
14. **F08 contract compatibility.** `Supervisor.run` returns an `AgentRunResult` with the same fields F08 consumes (`status`, `confidence`, `acceptance_criteria`, `branch_name`, `changed_files`, `diff_stat`, `head_commit_sha`, `token_usage`); F08's verify→PR flow drives the merged branch with **no F08 code change** (assert via an F08 fake consuming the result).
15. **Persistence & nesting.** Each subagent produces exactly one `sub_agent_run` row linked to its child `agent_run_id`; the supervisor's own `decision` steps are written to `agent_steps` under the parent run with `agent_run_ids[]` populated so F10 can nest; `GET /agent-runs/{id}/subagents` and `/supervision` return them.
16. **HITL interrupt/resume across nesting.** A child subagent interrupts (low confidence) → coordinator interrupts. `Supervisor.resume(parent_id, HumanResumeInput(decision="approve"))` resumes the supervision checkpoint, which resumes only the interrupted child; **already-completed subagents are not re-run** (assert their `sub_agent_run.started_at`/child step counts are unchanged).
17. **Token & cost aggregation.** `AgentRunResult.token_usage.total == Σ subagent token totals + supervisor overhead`; persisted on the parent `agent_runs.token_usage`.
18. **Feature flag & default.** With `MULTI_AGENT_ENABLED == false`, a supervised run finalizes `awaiting_input` with reason `multi_agent_disabled` and spawns no subagent; single-agent remains the platform default (a task with no `execution_mode` set never enters the coordinator).
19. **Idempotency.** Invoking `run_supervisor_task` twice for one parent id starts only one supervision (second observes `status != pending` and no-ops); a re-delivered message replays from the supervision checkpoint, not from scratch (no duplicate `sub_agent_run` rows).
20. **Secret redaction.** No model API key or configured secret pattern appears in `sub_agent_run.objective/artifact/error`, in `agent_runs.supervision`, in the returned `AgentRunResult`, or in logs.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; implement to green. Layout under `packages/multi-agent-coordinator/tests/`. F27 is built under `backend-tdd` (≥80% coverage on `forge_coordinator`, ruff + mypy green).

**Key fixtures (`conftest.py`):**
- `ScriptedExecutionAgent` — an `AgentRuntime` fake whose `run(objective)` returns a canned `AgentRunResult` keyed by the objective's role hint (writes a file to the given worktree branch for code-producing roles; returns a structured `SubAgentArtifact` for read-only roles). Records `objective.policy.allowed_actions` and `initial_context` for scoping/isolation assertions. Mirrors F06's `ScriptedModelClient` philosophy — no network/LLM.
- `agent_factory_recording` — returns `ScriptedExecutionAgent`s and records max concurrency via a shared counter (AC 7).
- `tmp_git_repo` — temp repo with `app/`, `tests/`, `AGENTS.md`, `.forge/policy.yaml` (reuse F06 fixture) serving as the `REPO_CACHE_ROOT` mirror; supports integration-branch + per-subagent worktrees.
- `policy_fixtures` — `SubagentRules` variants: `allow_all` (`allow_subagents=true, allowed_roles=[planner,researcher,implementer,tester,reviewer,security], max_parallel=2`), `deny_subagents`, `reviewer_only`.
- `pg_container` — Postgres (testcontainers) for `sub_agent_run` persistence + supervisor checkpointer.
- `stub_spec` / `stub_mcp` / `stub_github` / `stub_object_store` — minimal Protocol stubs.
- `redacting_log_capture` — for AC 20.

**Unit tests:**
- `test_pattern_selector.py` — table-driven over the 5 precedence rules; determinism (same input → same plan); MAKER_CHECKER on `review_required`; an explicit `objective`/`task.subagent_policy` hint selects the requested pattern verbatim (incl. `DYNAMIC_HANDOFF`, which is unreachable by the heuristic rules 2–5); assignments filtered to `allowed_roles`; resolved `allowed_actions` = intersection (AC 2, 4, 6). Property test: selector never produces an assignment whose `allowed_actions ⊄ ROLE_TOOLS[role]` (no widening); and `select` constructs **no** model client / `model_factory` (AC 2 determinism).
- `test_role_scoping.py` — `ROLE_TOOLS` intersections: implementer lacks `query_mcp`; researcher lacks `write_code`; reviewer is read-only + `write_review_comment` (AC 4).
- `test_policy_gate.py` — `spawn_subagent` denied → zero subagents + `policy_conflict` (AC 5); role-not-allowed optional→skip, required→block (AC 6); `max_parallel` resolution & cap (AC 7 resolution part).
- `test_branch_merger.py` — disjoint files → clean merge (AC 8); same-line edits → `conflicts[]`, nothing committed (AC 9); read-only roles produce no merge.
- `test_workspace_manager.py` — integration branch created off base; per-subagent worktrees distinct; cleanup keeps branches.
- `test_routing.py` — `router_after_collect/merge/finalize` over crafted supervision state (more-ready→dispatch; reviewer-reject+budget→dispatch(reset); `DYNAMIC_HANDOFF` deterministic re-route by explicit rule→dispatch(next role); conflicts→interrupt; low-conf→interrupt) — every branch is a pure predicate over typed state, no LLM (AC 9, 12, 13, 16).
- `test_validate.py` — AC marked unsatisfied despite subagent self-report when merged tree lacks evidence; confidence clamp on reviewer reject (AC 13).
- `test_result_mapping.py` — returned `AgentRunResult` has all F08-required fields; token aggregation (AC 14, 17).
- `test_redaction.py` — secrets stripped from `sub_agent_run`/`supervision`/result/logs (AC 20).

**Integration tests (graph end-to-end, scripted subagents + Postgres):**
- `test_maker_checker_happy.py` — two subagents, merge, succeed (AC 3, 15).
- `test_sequential_pipeline.py` — researcher→planner→implementer→tester→reviewer; structured handoff; isolation (AC 10, 11).
- `test_fan_out_clean.py` / `test_fan_out_conflict.py` — parallel merge clean vs conflict→interrupt (AC 7, 8, 9).
- `test_policy_disallowed.py` — `deny_subagents` → `awaiting_input` (AC 5).
- `test_reject_loop.py` — bounded re-implement (AC 12).
- `test_interrupt_resume.py` — child low-confidence interrupt; resume continues only the child; completed subagents untouched (AC 16, 19 replay).
- `test_worker_idempotency.py` — double `run_supervisor_task` → one supervision (AC 19).
- `test_feature_flag_off.py` — `enabled=false` → `multi_agent_disabled` (AC 18).
- `test_f08_consumes_result.py` — feed the returned `AgentRunResult` to an F08 fake verify→PR flow; no F08 change (AC 14).

**API tests (`apps/api/tests/`):** `GET /agent-runs/{id}` includes `sub_agent_runs[]` + `pattern`; `/subagents` and `/supervision` return correct shapes; workspace isolation (cross-workspace id → 404); resume routes to `Supervisor.resume` when `is_supervisor` (AC 15, 16).

## 8. Security & policy considerations

- **Opt-in, policy-gated, never default.** The coordinator runs only when the task explicitly sets `execution_mode = supervised_multi_agent` **and** `MULTI_AGENT_ENABLED` **and** the repo policy `subagent_rules.allow_subagents` permit it (Core Principle #1). A missing/false config never silently enables multi-agent.
- **`spawn_subagent` is policy-evaluated before any subagent runs.** The `policy_gate` node calls F04 `PolicyEvaluator.evaluate(ToolCall(name="spawn_subagent", args={"role": role}))` per distinct role and enforces `allowed_roles` + `max_parallel` (the enforcement F04 §12 deferred to V3). The Supervisor cannot grant a role the policy forbids.
- **No scope self-expansion.** Each subagent's tool set is frozen at spawn as `ROLE_TOOLS[role] ∩ task.allowed_actions ∩ skill_allowlist` and passed to F06's already-tested `ToolRegistry.scoped`; the subagent cannot widen it. `restricted_actions` (e.g. `deploy_prod`, `push_to_main`) hard-deny inside each subagent exactly as in single-agent (F06). Subagents inherit F06's per-tool policy check on every call.
- **Context isolation = blast-radius control.** Each subagent gets its own worktree, its own `agent_run_id`, its own checkpoint thread, and only its assignment's scoped `context_refs` — never another subagent's `AgentState`/steps. Cross-subagent influence flows only through reviewed **structured artifacts** (Multi-Agent Rules).
- **Human gates preserved.** Merge conflicts, low aggregate confidence, required-role failure, reviewer rejection past budget, and any `requires_approval` deny all route to `interrupt`/`awaiting_input` → F08 approval / F07 `needs_human_input`. There is no path to merge to a base branch without F08's human PR approval (the coordinator only produces an integration branch; F08 still gates the PR).
- **Validation against the spec.** `validate` checks acceptance criteria against the merged tree, not subagent self-agreement, so collusive or over-confident subagents cannot fast-track a merge.
- **BYOK.** Each subagent is a fresh F06 `ExecutionAgent` whose model key is resolved from the `cross-cutting/F37-auth-secrets-byok` per-workspace vault via `ModelConfig.api_key_ref` at call time (held in memory only); the raw key is never written to `agent_runs.inputs`/`supervision`, `sub_agent_run.objective`, the supervision checkpoint, or logs (Core Principle #5; no vendor lock-in).
- **Audit & redaction.** Every coordinator decision (`select_pattern`, `dispatch`, `collect`, `merge`, `validate`, `finalize`) and every subagent spawn/verdict/merge/conflict is written append-only — to the supervisor's `agent_steps` (immutable per F10/Security) and through the canonical `cross-cutting/F39-audit-log` writer — with the child `agent_run_id`. `sub_agent_run` rows are immutable audit records. Secrets are redacted with the single canonical `SecretRedactor` (`cross-cutting/F37-auth-secrets-byok`, reused via F06's `redaction.py`) before persistence in objectives, artifacts, errors, `supervision`, and logs; large artifacts (e.g. SARIF) go to MinIO with the same redaction.
- **Cost/resource bounds.** `max_parallel` (policy) ∩ `MULTI_AGENT_MAX_PARALLEL_CAP` (deploy) bounds concurrency; `subagent_timeout_seconds` and `review_loop_budget` bound runtime and retries to prevent multi-agent token/CI storms.
- **MCP read-only for researcher.** `query_mcp` flows through F09's read-only gateway (MCP Security Rules); implementer/tester never get `query_mcp`.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** The supervisor graph + deterministic pattern selector + policy gate + per-subagent worktree/branch management + git merge with conflict detection + structured-artifact handoff + nested checkpoint/resume + `sub_agent_run` persistence + result aggregation each compose non-trivially, and the slice must reuse F06 cleanly N times. Rough split: graph + nodes + routing (M), pattern selector + policy gate + role scoping (M), workspace manager + branch merger (M), persistence + API + aggregation (S/M), interrupt/resume across nesting + idempotent worker (M).

Key risks:
- **Git merge of subagent branches** is the highest-risk surface (fan-in conflicts, partial states). *Mitigation:* `BranchMerger` never auto-resolves — conflicts always interrupt to a human; sequential_integration strategy (one branch at a time off latest HEAD) is the default to minimize conflicts; heavy `test_branch_merger` coverage.
- **Determinism temptation.** Pressure to let the supervisor "reason" with an LLM violates the spec. *Mitigation:* graph nodes have no `model_factory` access (AC 2 asserts it); pattern selection is a pure table.
- **Nested checkpoint/resume** (supervisor thread + N child threads) is subtle. *Mitigation:* one authoritative supervision checkpoint; subagents run in-process and persist their own F06 checkpoints; `test_interrupt_resume` pins "completed subagents not re-run."
- **Contract drift with F06/F08.** The coordinator must return an `AgentRunResult` F08 already consumes. *Mitigation:* implement `AgentRuntime` Protocol exactly; `test_f08_consumes_result` against an F08 fake; freeze `forge_contracts.coordinator` first.
- **Cost/latency blow-up** from parallel LLM subagents. *Mitigation:* `max_parallel` cap + timeouts + Orchestrator-Worker (single implementer) as the degenerate default even in multi-agent mode (research: don't over-architect).
- **Worktree disk pressure** with N parallel subagents. *Mitigation:* deploy cap + cleanup(keep_branches) + documented sizing.

## 10. Key files / paths (exact)

```
packages/multi-agent-coordinator/forge_coordinator/
├── __init__.py
├── supervisor.py          # Supervisor(AgentRuntime): run(), resume()
├── graph.py               # build_supervisor_graph(deps) -> CompiledStateGraph
├── state.py               # SupervisionState (TypedDict) + reducers
├── nodes.py               # select_pattern, policy_gate, dispatch, collect, merge, validate, finalize
├── routing.py             # router_after_collect/merge/finalize
├── selector.py            # DefaultPatternSelector, ROLE_TOOLS, CODE_PRODUCING_ROLES
├── policy_gate.py         # spawn_subagent evaluation + max_parallel/allowed_roles enforcement
├── workspace.py           # SubAgentWorkspaceManager (reuses F06 WorktreeSandbox)
├── merger.py              # BranchMerger (git merge + conflict detection, no auto-resolve)
├── objectives.py          # build scoped AgentObjective per assignment (isolation)
├── artifacts.py           # SubAgentArtifact build/normalize -> RetrievedChunk handoff
├── aggregate.py           # acceptance-criteria validation + confidence aggregation -> AgentRunResult
├── persistence.py         # SubAgentRunSink (sub_agent_run rows), parent run status updates
└── settings.py            # CoordinatorSettings (env: MULTI_AGENT_*)
packages/multi-agent-coordinator/tests/        # unit + integration (see §7)
packages/multi-agent-coordinator/pyproject.toml

apps/worker/forge_worker/tasks/coordinator.py  # run_supervisor_task, resume_supervisor_task
apps/api/forge_api/routers/agent.py            # extend GET; add /subagents, /supervision (resume reused)
apps/api/forge_api/services/agent_runs.py      # create_and_enqueue: branch on execution_mode (small extension to F06)
packages/db/forge_db/models/sub_agent_run.py
packages/db/forge_db/migrations/versions/xxxx_f27_multi_agent.py  # agent_runs cols + sub_agent_run

# Consumed contracts (frozen, shaped for this slice):
packages/contracts/forge_contracts/coordinator.py  # CoordinationPattern, ROLE_TOOLS, SupervisionPlan,
                                                    # SubAgentAssignment, SubAgentArtifact, SubAgentResult,
                                                    # MergeResult, SupervisionView, SubagentPolicy, PatternSelector

# Frontend (extends F10):
apps/web/components/runs/SupervisionPanel.tsx
apps/web/components/runs/SubAgentTraceGroup.tsx
apps/web/lib/hooks/useSupervision.ts

# Deploy:
deploy/docker-compose.yml / docker-compose.dev.yml  # worker: MULTI_AGENT_* env
```

## 11. Research references (relevant links from the spec/research report)

- **Multi-agent production patterns (Orchestrator-Worker default; Maker-Checker; Fan-out/Fan-in; Sequential; Dynamic handoff; "don't over-architect"):** https://beam.ai/agentic-insights/multi-agent-orchestration-patterns-production (research report cite:114) — basis for `CoordinationPattern` + the Pattern Selection Guide.
- **Pattern selection guide:** https://www.kore.ai/blog/choosing-the-right-orchestration-pattern-for-multi-agent-systems (cite:117) — explicit decision criteria for the deterministic supervisor.
- **Supervisor / hierarchical control with explicit routing criteria (not prompted reasoning):** research report §"Multi-Agent Orchestration" (cite:112, cite:117) — the deterministic-supervisor mandate.
- **LangGraph as the framework for both single- and multi-agent within one graph model:** https://langchain-ai.github.io/langgraph/ · https://github.com/langchain-ai/langgraph (research report cite:131) — Tech-stack "Multi-agent layer = LangGraph Supervisor pattern."
- **Best multi-agent frameworks 2026 / LangChain frameworks:** https://gurusup.com/blog/best-multi-agent-frameworks-2026 · https://www.langchain.com/resources/ai-agent-frameworks.
- **Open SWE (isolated sandbox per task, structured task context, curated tools):** https://github.com/langchain-ai/open-swe (cite:35) — the per-subagent worktree + scoped-context-not-chat handoff pattern.
- **Symphony (task-as-control-plane; agents in dedicated workspaces; orchestrator monitors):** https://openai.com/index/open-source-codex-orchestration-symphony/ (cite:126).
- **Spec sections:** Multi-Agent Orchestration (Pattern Selection Guide, Subagent Role Definitions, Multi-Agent Rules); Product Scope (Multi-Agent Coordinator); Technology Stack (Multi-agent layer); Core Data Model (`WorkflowRun → AgentRun → SubAgentRun[]`); Repo Policy System (`subagent_rules`); Task Schema (`execution_mode`, `subagent_policy`); Workflow Engine → Workflow DSL (`modes.optional`); Human Approval System; Security table; Phase 3 roadmap ("Supervised multi-agent mode (Supervisor pattern, opt-in)").

## 12. Out of scope / future

- **LLM-driven / adaptive-planning supervisor** — the V3 supervisor routes by explicit policy and a deterministic plan only ("Supervisor makes routing decisions via explicit policy, not LLM judgement"). LLM-reasoned routing and runtime plan *discovery* (Adaptive Planning pattern) are future.
- **Heuristic auto-selection of `DYNAMIC_HANDOFF`** — the `DefaultPatternSelector` reaches `DYNAMIC_HANDOFF` (spec's "Complex multi-domain task" row) only via an explicit task/objective hint (precedence rule 1); its re-routing edges are still deterministic explicit rules over typed state. Inferring "complex multi-domain" automatically (without an LLM) is a future selector refinement.
- **Subagents as independent Celery tasks / cross-host distribution** — V3 runs subagents in-process under one supervision checkpoint (bounded by `max_parallel`). A distributed subagent fan-out (one Celery task per subagent + durable join, or Temporal child workflows in V2/V3 workflow engine) is future.
- **AI-assisted merge-conflict resolution** — `BranchMerger` detects conflicts and escalates to a human; automated resolution is out of scope.
- **Cross-repo multi-agent** — V3 multi-agent targets a single `repo_target`; combining with multi-repo execution (V2 `F22`) is future.
- **Workflow visual editor / authoring custom multi-agent patterns** as DSL — V3 ships the five built-in patterns + a deterministic selector; a `multi_agent` DSL block and visual editor (Phase 3 "Workflow visual editor") are separate slices.
- **Container / Firecracker isolation per subagent** — V3 uses git worktrees (same as F06); stronger per-subagent isolation is the Phase-3 "Firecracker / gVisor sandbox" slice.
- **Full SAST/dependency-audit tool implementations** for the `security` role — F27 wires the `security` role and its `optional` artifact (SARIF ref); the actual `run_sast`/`audit_dependencies` tool backends are owned by the security-tooling slice (the `security` role is skipped/stubbed if absent).
- **F10 deep multi-agent trace UX** (timeline DAG visualization, per-subagent diff overlays) — F27 ships the data + a basic `SupervisionPanel`; richer visualization is an F10 follow-up.
- **Eval-set coverage for multi-agent** — golden multi-agent tasks in F12's harness are a fast-follow once the five patterns are stable.
```