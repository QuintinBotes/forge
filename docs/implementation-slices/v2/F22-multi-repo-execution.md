# F22 — Multi-Repo Task Execution

> Phase: v2 · Spec module(s): Execution Agent Runtime (`packages/agent-runtime/forge_agent`), Orchestrator / Workflow Engine (`packages/workflow-engine/forge_workflow` — verification + PR + merge gate), Repo Policy Layer (`packages/policy-sdk/forge_policy`), GitHub App integration (`packages/integration-sdk`), Board (Task schema `repo_targets[]`) · Status target: **Done** = a single Task with `repo_targets[]` of length ≥ 2 runs one single-agent execution across N isolated git worktrees, each tool call is policy-checked against *its own* repo's policy, verification runs per-repo against per-repo policy commands, one PR is opened per changed repo with cross-PR links and per-repo spec traceability, an aggregate merge gate refuses to merge any PR until **every** required repo PR is approved + CI-green + spec-validated, and merge then executes in a topologically-ordered, dependency-respecting sequence — all audit-logged, and green under `ruff` + types + `pytest`. The existing single-repo path (`len(repo_targets) == 1`) is byte-for-byte behaviour-preserved.

---

## 1. Intent — what & why

The Task schema already declares `repo_targets[]` as a **list** (FORGE_SPEC.md §Task Schema), but V1 deliberately collapsed it to one entry: F06 froze `AgentObjective.repo_target` (singular), F08 made `pull_request` unique per `workflow_run` ("V1 assumes a single repo target → single PR per run"), and both slices list **"Multi-repo / multi-PR tasks — V2"** as out of scope. F22 is that V2 slice: it widens the execution arc — agent runtime, verification, PR builder, merge gate, and the FSM guards/actions — to handle `len(repo_targets) > 1` while leaving the single-repo path untouched.

Why this is a real feature and not a config toggle: a single unit of work frequently spans repos that must change *together* and *consistently* — e.g. a backend API contract change (`org/api`) plus the generated client in the frontend (`org/web`), or a shared schema in `org/proto` consumed by two services. The agent must reason about the change *holistically* (so the API and its client stay in sync), but Forge's non-negotiables still apply *per repo*: every write is checked against **that repo's** `.forge/policy.yaml` (different repos have different `write_rules`/`commands`/`review_rules`), verification runs **that repo's** commands, and merge is **human-gated per repo**.

Design stance, anchored to the spec's Core Design Principles:

- **Single-agent stays the default (Principle 1).** F22 is *not* fan-out to multiple agents. It is **one** `AgentRun` operating over a **multi-worktree workspace**, because cross-repo coherence (contract + client) needs shared reasoning context. Supervised multi-agent fan-out remains Phase 3. (Per the Multi-Agent Pattern Selection Guide, fan-out fits *independent* subtasks; cross-repo coupled changes are not independent.)
- **Repo-aware and policy-aware per repo (Principle 3).** The agent never gets one merged "super-policy". Each tool call carries a `repo` and is evaluated against that repo's `PolicySnapshot`. A write allowed in `org/api` but denied in `org/web` is enforced exactly that way.
- **Human approval before every merge — always (Principle 5 / Build constraint 5).** Multi-repo amplifies this: merge is **all-or-nothing-gated** (no PR merges until *all* required repo PRs are green + approved + spec-validated) and then **ordered** by an explicit dependency DAG.

What F22 must honestly confront and bound: GitHub offers **no cross-repo atomic merge**. F22 provides a strong *pre-merge gate* + *ordered merge* + *partial-merge halt-and-escalate*, and documents the residual partial-merge window as a known V2 limitation (§9, §12).

## 2. User-facing behavior / journeys

**Journey A — Happy path, coupled API + client change:**
1. An engineer has an approved spec/plan for `TASK-401` with two repo targets: `org/api` (`role: primary`) and `org/web` (`role: secondary`, `depends_on: [github.com/org/api]`), skill `backend-tdd` for api and `frontend-ui` for web. Task is `task_ready`.
2. They click **Run**. The workflow goes `task_ready → executing`. The task card shows **two** live repo lanes ("api: executing", "web: executing"), each with its own worktree.
3. The single agent reads both repos, edits the endpoint in `org/api` and the generated client in `org/web`, writes tests in both. Each write shows under the correct repo lane; an attempted write to `org/web/infra/prod/**` (denied by web's policy) is rejected and surfaced as a denied step under the web lane.
4. `executing → verifying`. Each repo lane fills its own checklist from *that repo's* policy commands: api → Lint/Type/Tests/Coverage (87% ≥ 80%); web → Lint/Type/Tests + a11y.
5. All repos pass → `verifying → pr_opened`. **Two** PRs appear — `org/api#123` and `org/web#456` — each body carrying its repo's spec-traceability subset, verification table, confidence, and a **cross-PR link block** ("Part of TASK-401 · merges with org/web#456 · merge order: api → web").
6. `pr_opened → awaiting_review`. **One `pr` approval gate per repo PR** is created (via F36, so api's `team-backend` and web's `team-frontend` reviewers each gate their own repo). The card shows an aggregate "0/2 approved" gate.
7. Backend reviewer approves api; nothing merges (gate shows "1/2 — waiting on org/web review + CI"). Frontend reviewer approves web; CI is green on both; both specs validated → aggregate gate becomes "Ready to merge (order: api → web)".
8. An authorized user clicks **Merge group**. Forge merges `org/api#123` first (it is the dependency root), then `org/web#456`. Workflow `awaiting_review → merged → closed`. Both PRs show merged; the board reflects done.

**Journey B — One repo fails verification, agent retries (whole task):**
- After `verifying`, `org/web` tests fail while `org/api` passes. The aggregate verification is "failed" (any-repo-fail ⇒ fail). Retry budget remains → `verifying → executing` after backoff; the agent re-runs with the failing-repo context. Repos that already passed are re-verified on the retry (their worktrees persist) so the final report is a consistent snapshot. On a later all-pass attempt, flow rejoins Journey A.

**Journey C — A repo target produces no diff:**
- The agent decides `org/proto` needs no change. That repo's `RepoChangeSet.has_changes=false`; **no PR** is opened for it; it is excluded from the merge gate. If another repo `depends_on` it, that dependency edge is treated as satisfied (nothing to merge). If `org/proto` was `required_for_merge: true` *and* the spec's acceptance criteria mapped to it are unsatisfied, the run escalates (`needs_human_input`) rather than silently merging a partial change.

**Journey D — Partial-merge failure (the hard edge):**
- All gates pass; merge order is `api → web`. `org/api#123` merges. `org/web#456` merge call fails (e.g. base branch moved, mergeability lost). Forge **halts immediately** (does not attempt rollback of the already-merged api PR), transitions to `needs_human_input` with `escalation_reason="partial_merge: org/api merged, org/web pending"`, records exactly which repos merged, and notifies. A human resolves (re-run web only, or revert api). No automatic cross-repo revert in V2.

**Journey E — Cross-repo dependency cycle:**
- A maintainer mis-configures `org/api depends_on org/web` and `org/web depends_on org/api`. At run start the merge-plan builder detects the cycle and refuses to start the run with a clear `CyclicRepoDependencyError`; the task stays `task_ready` with the offending edges named.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One Alembic migration `packages/db/migrations/versions/00XX_f22_multi_repo.py` (additive + index changes; reversible), chaining on the shared `packages/db` Alembic history that F06/F07/F08 extend. SQLAlchemy models live in `packages/db/forge_db/models/` (new `pr_group.py`, `agent_repo_workspace.py`; extensions to the F08 models). It **extends** F08's tables and adds three.

**Extend Task `repo_targets[]` schema (no DDL — it is JSONB on `task`, owned by F01).** Each element gains coordination fields (validated by the Pydantic `RepoTarget`, §4): `role` (`primary` | `secondary`, exactly one `primary`), `depends_on: list[str]` (repo ids), `skill_profile: str | None` (per-repo override), `required_for_merge: bool = true`. A data-backfill step sets `role=primary, depends_on=[], required_for_merge=true` on every existing single-element `repo_targets` so V1 tasks are valid under the new schema.

**Extend `verification_report` (F08 table)** — make it per-repo:
| change | detail |
|---|---|
| add `repo_id` | `text not null` (V1 backfill = the single repo target) |
| drop unique `(workflow_run_id, attempt)` | replace with `(workflow_run_id, repo_id, attempt)` |
| add index | `ix_verification_report_run_repo (workflow_run_id, repo_id)` |

**Extend `pull_request` (F08 table)** — allow N PRs per run with cross-links + ordering:
| change | detail |
|---|---|
| add `repo_id` | `text not null` (already had `repo_id`; now load-bearing for grouping) |
| drop unique `(workflow_run_id)` | replace with unique `(workflow_run_id, repo_id)` |
| add `pr_group_id` | `uuid` FK → `pr_group.id` (the run's PR set) |
| add `merge_order` | `int not null default 0` (topo rank within the group) |
| add `depends_on_repo_ids` | `jsonb not null default '[]'` (repo ids whose PRs must merge first) |
| add `has_changes` | `boolean not null default true` (false ⇒ no PR row created) |
| add `merge_state` | `text not null default 'pending'` (`pending` \| `merged` \| `skipped` \| `failed`) |

**New table `pr_group`** — one row per multi-repo run's PR set (the merge unit):
| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workflow_run_id` | `uuid` FK → `workflow_run.id` | unique (one group per run) |
| `task_id` | `uuid` FK → `task.id` | |
| `repo_count` | `int not null` | number of changed repos with PRs |
| `merge_order` | `jsonb not null` | topologically sorted list of repo ids |
| `status` | `text not null default 'open'` | `open` \| `ready` \| `merging` \| `merged` \| `partially_merged` \| `failed` |
| `merged_repo_ids` | `jsonb not null default '[]'` | set as each PR merges (partial-merge audit) |
| `created_at` / `updated_at` | `timestamptz` | |

**New table `agent_repo_workspace`** — per (`agent_run`, repo) worktree record (the multi-worktree analogue of F06's single `worktree_path` columns on `agent_runs`):
| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `agent_run_id` | `uuid` FK → `agent_runs.id` ON DELETE CASCADE | |
| `repo_id` | `text not null` | |
| `role` | `text not null` | `primary` \| `secondary` |
| `worktree_path` | `text not null` | absolute, under `WORKTREE_ROOT/<agent_run_id>/<repo_slug>` |
| `branch_name` | `text not null` | `forge/TASK-401` (consistent across repos) |
| `base_branch` | `text not null` | |
| `base_commit_sha` | `varchar(40) not null` | |
| `head_commit_sha` | `varchar(40)` null | |
| `policy_snapshot_id` | `uuid` FK → `repo_policy_snapshot.id` | which policy governed this repo (F04 audit) |
| Unique | `(agent_run_id, repo_id)` | |

**`approval_request` (no F22 schema change — owned by `cross-cutting/F36-human-approval-system`).** F36 generalizes F08's pr-only table (renames `kind`→`gate_type`, adds `subject_type`/`subject_id`, and replaces F08's `uq_pending_pr_approval` with `uq_pending_gate ON approval_request (subject_type, subject_id, gate_type) WHERE status='pending'`). F22 needs **no new column or index here**: each repo's PR is a distinct subject (`subject_type='pull_request'`, `subject_id=<pull_request.id>`), so F36's per-subject partial-unique index already yields exactly **one pending `pr` gate per repo PR per run**. Per-repo `review_rules` enforcement is likewise already F36's responsibility (see §3.2).

All security-relevant writes are recorded in the central immutable audit log (`cross-cutting/F39-audit-log`) via `deps.audit.emit(session, AuditEvent(...))`, each F22 entry carrying `repo_id` in its `detail`/`detail_ref`; FSM transitions are additionally recorded by F07's `workflow_transition`. F22 adds workflow/domain events `repo_checks_started`, `repo_check_completed`, `repo_pr_opened`, `pr_group_ready`, `merge_group_started`, `repo_merged`, `merge_group_completed`, `partial_merge_halted`.

### 3.2 Backend (FastAPI routes + services/packages)

**`packages/policy-sdk/forge_policy`** — add multi-repo loading:
- `load_policies(worktree_roots: dict[str, str]) -> dict[str, Policy]` — loads + validates each repo's `.forge/policy.yaml` (fail-closed per repo; a single bad policy fails the whole run, named by repo).
- `RepoScopedPolicyEvaluator` — wraps `PolicyEvaluator`, selects the policy by `tool_call.repo` before evaluating; raises `UnknownRepoError` if `repo` is not in scope (no implicit default).

**`packages/workflow-engine/forge_workflow`** — add `multi_repo/`:
- `multi_repo/plan.py` — `MergePlanBuilder.build(repo_targets) -> MergePlan` (validates exactly one `primary`, builds the `depends_on` DAG, runs **cycle detection** → `CyclicRepoDependencyError`, returns topological `merge_order`). Reuses the board's cycle-detection util (F01) where available.
- `multi_repo/verification.py` — `MultiRepoVerificationService.run_all(...)`: fans out F08's `VerificationService.run_checks` once **per repo** (each with that repo's `policy` + effective `skill_profile`), persists one `verification_report` per `(run, repo, attempt)`, returns `MultiRepoVerificationReport` (aggregate `all_passed = all(repos)`).
- `multi_repo/pr.py` — `MultiRepoPRBuilder.open_group(...)`: for each repo with `has_changes`, calls F08's `PRBuilderService` to open a PR (per-repo traceability subset), then writes the `pr_group` row, computes `merge_order`, injects the **cross-PR link block** into each PR body (a second `GitHubIntegration.update_pr` pass once all PR numbers are known), and creates one `pr` approval gate per PR via F36's `ApprovalService.create(gate_type="pr", subject=<pull_request>)`.
- `multi_repo/merge.py` — `MultiRepoMergeGate.evaluate(workflow_run_id) -> MultiRepoMergeGateResult` (aggregate of F08's per-PR `MergeGateEvaluator` across all required repos) and `MultiRepoMerger.merge_in_order(workflow_run_id) -> MergeGroupOutcome` (ordered merge with halt-on-failure + partial-merge recording).
- `transitions/handlers_multi_repo.py` + `transitions/guards_multi_repo.py` — register multi-repo variants of F08's action handlers/guards (see §4 dispatch rule).

**`apps/api/app/`:**
- `routers/runs_multi_repo.py`:
  - `GET /workflow-runs/{run_id}/repos` → `list[RepoExecutionSummary]` (per-repo state, verification, PR, approval, merge_state).
  - `GET /workflow-runs/{run_id}/pr-group` → `PRGroupResponse` (PRs, merge order, aggregate gate).
  - `POST /workflow-runs/{run_id}/merge-group` → `MergeGroupOutcome` (authorized merge of the whole group; idempotent).
- Approval decisions reuse F36's canonical `POST /approvals/{id}/decision` endpoint and the `pr` `GateResolutionHook` (the hook body is owned by F08, registered into F36's gate registry). For a multi-repo run that hook resolves the *per-repo* PR gate and returns its blocking reasons; the **aggregate** readiness (e.g. "1/2 approved") is computed by F22's `MultiRepoMergeGate` and surfaced via the `/repos` and `/pr-group` endpoints below — F22 adds no second decision endpoint.
- `services/merge_group_service.py` — `MergeGroupService.merge(run_id, actor)`: authorizes via F36's authorizer (member/admin, never the agent-runner; each repo's `review_rules` satisfied), evaluates the aggregate gate, drives the F07 transition `awaiting_review → merged` via `MultiRepoMerger`, returns `MergeGroupOutcome`.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

**`packages/agent-runtime/forge_agent`** — widen to multi-worktree:
- `sandbox.py` — add `MultiRepoWorkspace`: `create_all(repo_targets) -> dict[repo_id, WorktreeHandle]`, `handle(repo_id)`, `commit_all_repos(message) -> dict[repo_id, sha]`, `change_sets() -> list[RepoChangeSet]`, `cleanup_all(keep_branches=True)`. Each repo gets an isolated worktree under `WORKTREE_ROOT/<agent_run_id>/<repo_slug>` off its own bare mirror.
- `tools/base.py` — `ToolContext` gains `repo: str` and the runtime resolves `worktree_path`/`policy` per `repo`; every tool arg schema gains a required `repo: str` field (`read_repo`, `write_code`, `apply_patch`, `run_tests` all repo-scoped). The model's tool specs document the allowed `repo` enum (the task's repo ids).
- `policy_guard.py` — `MultiRepoPolicyGuard` holds `dict[repo_id, PolicySnapshot]`; `check(call)` selects by `call.repo`. Path confinement asserts the resolved path is under **that repo's** worktree only (cross-repo path escape is denied).
- `nodes.py` / `graph.py` — `load_context` creates **all** worktrees and injects each repo's `AGENTS.md` + policy summary + effective skill block, labelled by repo; `verify` is replaced by a per-repo verification loop (one `VerificationResult[]` set per repo); `finalize` maps acceptance criteria to the repo(s) whose diffs/tests satisfy them and assembles `repo_change_sets[]`.
- `objective.py` — `AgentRunInputBuilder.build` now composes `repo_targets[]`, `policies{}`, `agents_md{}`, `skill_profiles{}` (resolving per-repo overrides), and persists one `agent_repo_workspace` row per repo.

**`apps/worker/forge_worker/tasks/`:**
- `tasks/verification_multi_repo.py` — `run_repo_verification(workflow_run_id, agent_run_id, repo_id, attempt)`: verifies a single repo inside its worktree; a fan-out **chord** over all repos calls `aggregate_repo_verification(...)` on completion, which emits `checks_passed`/`checks_failed` to F07 (all-pass ⇒ passed). Repos verify in parallel (each on the `verification` queue) but the chord fans in deterministically.
- `tasks/pr_multi_repo.py` — `open_pr_group(workflow_run_id)`: wraps `MultiRepoPRBuilder.open_group`; re-enqueues `advance_workflow` (→ `awaiting_review`).
- `tasks/merge_multi_repo.py` — `merge_pr_group(workflow_run_id)`: wraps `MultiRepoMerger.merge_in_order` (ordered, halt-on-failure); on full success advances `merged → closed`; on partial failure transitions `→ needs_human_input` with the partial-merge reason.
- `start_agent_run` effect (F07/F08) dispatches **one** `agent.run` for the whole task (single AgentRun, multi-worktree) — *not* one per repo.

LangGraph: unchanged topology; the agent loop still runs as a single `StateGraph`/`CompiledStateGraph` (single-agent). Only the tool/sandbox/policy layers became repo-aware.

### 3.4 Frontend / UI (Next.js routes/components)

**`apps/web/`:**
- `app/(dashboard)/projects/[projectId]/tasks/[taskId]/review/page.tsx` (F08 page) — gains a **per-repo tab strip** when `repo_targets.length > 1`. Each repo tab reuses F08's `DiffViewer`, `VerificationPanel`, `TraceabilityMatrix`, `ConfidenceBadge`, `RiskFlags` scoped to that repo's PR; an always-visible **aggregate merge panel** (`components/review/MergeGroupPanel.tsx`) shows per-repo readiness (approved/CI/validated), the merge order DAG, and a single **Merge group** action (disabled until the aggregate gate is green).
- `components/board/RepoLanes.tsx` — on the task card, one live lane per repo target (executing/verifying/pr/approved/merged), subscribed to the F07/F10 run-event stream.
- Data layer: TanStack Query hooks `useRepoExecutionSummaries(runId)`, `usePrGroup(runId)`, mutation `useMergeGroup(runId)` (optimistic, rollback on blocking reasons). Keyboard: `1..9` jump to repo tab, `g` = merge group (confirm), reuse F08's `a/r/x` within the focused repo tab.

### 3.5 Infra / deploy (compose, helm, caddy)

- **No new service.** Reuses `worker` (agent + verification queues), `api`, `db`, `redis`, `minio`.
- **Worktree storage:** `WORKTREE_ROOT` now holds `<agent_run_id>/<repo_slug>` subtrees; N worktrees per run multiply disk usage — document sizing and ensure `cleanup_all` runs on terminal states. Named volume `forge-worktrees` (from F06) unchanged.
- **Repo mirrors:** every repo in `repo_targets[]` must already be mirrored under `REPO_CACHE_ROOT` by the GitHub App (F03); F22 only adds worktrees off them. The run fails fast (named repo) if a target repo is not connected.
- **GitHub App scope:** PRs in different repos may be under different App installations; the merge call for each repo uses **that repo's** installation token (F03). No Caddy/Helm changes (Helm is V2 elsewhere; F22 adds no chart values beyond what F06/F08 already define).

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

> Coordinated contract widening of F06/F08 frozen DTOs (mirrors how F07 widened `WorkflowEngine`). Single-repo fields are retained as computed back-compat properties so V1 callers keep working.

**Task `RepoTarget` (widened; `packages/contracts/forge_contracts/agent.py`):**
```python
class RepoTarget(BaseModel):
    repo: str                                   # github.com/org/api  (the repo_id)
    base_branch: str = "main"
    branch_prefix: str                          # forge/TASK-401
    branch_name: str                            # resolved: forge/TASK-401 (same across repos)
    worktree: bool = True
    # --- F22 additions ---
    role: Literal["primary", "secondary"] = "secondary"
    depends_on: list[str] = []                  # repo_ids that must merge BEFORE this repo
    skill_profile: str | None = None            # per-repo override; None ⇒ task.skill_profile
    required_for_merge: bool = True             # if False, an empty/absent diff does not block
```

**`AgentObjective` (widened):**
```python
class AgentObjective(BaseModel):
    agent_run_id: UUID
    workspace_id: UUID
    task_id: UUID
    workflow_run_id: UUID | None
    kind: str
    objective: str
    acceptance_criteria: list[AcceptanceCriterion]
    # --- F22: multi-repo ---
    repo_targets: list[RepoTarget]                       # length >= 1
    policies: dict[str, PolicySnapshot]                  # repo_id -> policy
    agents_md: dict[str, str]                            # repo_id -> AGENTS.md text
    skill_profiles: dict[str, SkillProfileSpec]          # repo_id -> effective skill
    initial_context: list[RetrievedChunk]
    knowledge_scope: KnowledgeScope
    model: ModelConfig
    max_iterations: int = 60                              # raised: multi-repo needs more steps
    confidence_threshold: float = 0.72

    @property
    def primary_repo_target(self) -> RepoTarget:
        return next(rt for rt in self.repo_targets if rt.role == "primary")

    @property
    def repo_target(self) -> RepoTarget:                 # V1 back-compat alias
        return self.primary_repo_target
```

**Per-repo result + widened `AgentRunResult`:**
```python
class RepoChangeSet(BaseModel):
    repo: str
    branch_name: str
    base_commit_sha: str
    head_commit_sha: str | None
    changed_files: list[str]
    diff_stat: dict[str, int]                            # {files, insertions, deletions}
    has_changes: bool

class AgentRunResult(BaseModel):
    agent_run_id: UUID
    status: Literal["succeeded", "failed", "awaiting_input", "cancelled"]
    confidence: float
    summary: str
    acceptance_criteria: list[AcceptanceCriterionResult] # each may cite repo(s) in evidence
    repo_change_sets: list[RepoChangeSet]                 # F22: one per repo target
    needs_human_reason: str | None = None
    token_usage: TokenUsage
    steps: list[Step]

    @property
    def changed_files(self) -> list[str]:                # V1 back-compat (primary repo)
        primary = next((c for c in self.repo_change_sets), None)
        return primary.changed_files if primary else []
```

**Tool context / guard (agent-runtime):**
```python
class ToolContext(BaseModel, arbitrary_types_allowed=True):
    agent_run_id: UUID
    workspace_id: UUID
    repo: str                                            # F22: required, selects worktree+policy
    worktree_path: str                                   # resolved for `repo`
    policy: PolicySnapshot                               # resolved for `repo`
    knowledge_scope: KnowledgeScope
    deps: "RuntimeDeps"

class MultiRepoPolicyGuard:
    def __init__(self, policies: dict[str, PolicySnapshot], evaluator: PolicyEvaluator) -> None: ...
    def check(self, call: ToolCall) -> Decision: ...          # selects policies[call.repo]
    def check_write_path(self, repo: str, path: str) -> Decision: ...  # confined to repo's worktree
    def check_command(self, repo: str, command: str) -> Decision: ...
    # raises UnknownRepoError if call.repo not in policies
```

**Multi-worktree workspace (agent-runtime):**
```python
class MultiRepoWorkspace:
    def __init__(self, repo_cache_root: str, worktree_root: str, agent_run_id: UUID) -> None: ...
    async def create_all(self, repo_targets: list[RepoTarget]) -> dict[str, WorktreeHandle]: ...
    def handle(self, repo_id: str) -> WorktreeHandle: ...
    async def commit_all_repos(self, message: str) -> dict[str, str]: ...   # repo_id -> sha
    async def change_sets(self) -> list[RepoChangeSet]: ...                  # has_changes computed
    async def cleanup_all(self, *, keep_branches: bool = True) -> None: ...
```

**Merge plan + cycle detection (workflow-engine):**
```python
class MergePlan(BaseModel):
    primary_repo_id: str
    merge_order: list[str]                  # topologically sorted; primary first among equals
    edges: dict[str, list[str]]             # repo_id -> depends_on repo_ids

class MergePlanBuilder:
    @staticmethod
    def build(repo_targets: list[RepoTarget]) -> MergePlan: ...
    # raises MultipleOrNoPrimaryError, CyclicRepoDependencyError(cycle=[...]),
    #        UnknownDependencyRepoError(repo, missing)
```

**Verification (per-repo + aggregate):**
```python
class MultiRepoVerificationReport(BaseModel):
    workflow_run_id: UUID
    agent_run_id: UUID
    attempt: int
    per_repo: dict[str, VerificationReport]   # repo_id -> F08 VerificationReport
    all_passed: bool                          # all(required repos passed)

class MultiRepoVerificationService:
    def __init__(self, verifier: VerificationService, ws: MultiRepoWorkspace,
                 repo: VerificationRepository) -> None: ...
    async def run_all(self, *, workflow_run_id: UUID, agent_run_id: UUID, attempt: int,
                      policies: dict[str, PolicySnapshot],
                      skill_profiles: dict[str, SkillProfileSpec]) -> MultiRepoVerificationReport: ...
```

**PR group + cross-links:**
```python
class CrossPRLink(BaseModel):
    repo_id: str
    pr_number: int | None
    url: str | None
    merge_order: int

class PRGroup(BaseModel):
    id: UUID
    workflow_run_id: UUID
    task_id: UUID
    merge_order: list[str]                   # repo_ids
    prs: list[CrossPRLink]
    status: Literal["open","ready","merging","merged","partially_merged","failed"]
    merged_repo_ids: list[str]

class MultiRepoPRBuilder:
    def __init__(self, pr_builder: PRBuilderService, github: GitHubIntegration,
                 repo: PullRequestRepository) -> None: ...
    async def open_group(self, *, workflow_run_id: UUID, agent: AgentRunResult,
                         verification: MultiRepoVerificationReport,
                         plan: MergePlan) -> PRGroup: ...
```

**Aggregate merge gate + ordered merger:**
```python
class RepoMergeStatus(BaseModel):
    repo_id: str
    has_changes: bool
    review_approved: bool
    ci_green: bool
    spec_validated: bool
    merge_order: int
    blocking_reasons: list[str]

class MultiRepoMergeGateResult(BaseModel):
    can_merge: bool                          # True iff every required, changed repo is mergeable
    repos: list[RepoMergeStatus]
    merge_order: list[str]
    blocking_reasons: list[str]              # flattened, repo-prefixed

class MergeGroupOutcome(BaseModel):
    status: Literal["merged","partially_merged","blocked","failed"]
    merged_repo_ids: list[str]
    failed_repo_id: str | None
    workflow_state: str
    gate: MultiRepoMergeGateResult

class MultiRepoMergeGate:
    def __init__(self, per_pr_gate: MergeGateEvaluator, repo: WorkflowReadRepository) -> None: ...
    async def evaluate(self, workflow_run_id: UUID) -> MultiRepoMergeGateResult: ...

class MultiRepoMerger:
    def __init__(self, gate: MultiRepoMergeGate, github: GitHubIntegration,
                 repo: PullRequestRepository) -> None: ...
    async def merge_in_order(self, workflow_run_id: UUID) -> MergeGroupOutcome: ...
    # re-checks the FULL aggregate gate immediately before the FIRST merge; merges
    # in plan.merge_order; on any failure HALTS, records merged_repo_ids, returns
    # status="partially_merged" (or "failed" if first merge fails).
```

**FSM dispatch rule (how F22 coexists with F08 in F07's registries):** the worker-side action/guard registries select the multi-repo variant when `len(task.repo_targets) > 1`, else F08's single-repo handler. State vocabulary is unchanged. New/overridden guard semantics:
| guard/action (F08 → F22 multi-repo) | semantics |
|---|---|
| `all_checks_passed` | all **required** repos' latest-attempt reports passed |
| `any_check_failed` | any repo failed (drives retry of the *whole* task) |
| `open_pr_with_spec_traceability` | `MultiRepoPRBuilder.open_group` (one PR per changed repo) |
| `merge_ready` | `MultiRepoMergeGate.evaluate().can_merge` |
| `merge_pr` (action) | `MultiRepoMerger.merge_in_order` (ordered, halt-on-failure) |

**REST (all `/api/v1`, authenticated):**
```
GET    /workflow-runs/{run_id}/repos          -> list[RepoExecutionSummary]
GET    /workflow-runs/{run_id}/pr-group        -> PRGroupResponse
POST   /workflow-runs/{run_id}/merge-group     -> MergeGroupOutcome
POST   /approvals/{approval_id}/decision        -> ApprovalResolution   # F36-owned; pr hook resolves the per-repo gate. Aggregate readiness via /repos + /pr-group above. F22 adds no new decision endpoint.
```

## 5. Dependencies — features/slices that must exist first

| Ref | Why F22 needs it | Hard/Soft |
|---|---|---|
| `v1/F06-single-execution-agent` | `ExecutionAgent`, `RepoTarget`, `AgentObjective`, `WorktreeSandbox`, tool registry/`PolicyGuard`, `ScriptedModelClient` — F22 widens all of these to multi-worktree | **Hard** |
| `v1/F08-plan-execute-verify-pr-approval` | `VerificationService`, `PRBuilderService`, `SpecTraceabilityComposer`, `MergeGateEvaluator`, the `pr` `GateResolutionHook`, `verification_report`/`pull_request` tables, FSM handlers/guards — F22 fans these out per repo | **Hard** |
| `cross-cutting/F36-human-approval-system` | canonical `approval_request`/`gate_type` schema + `uq_pending_gate` per-subject index, `ApprovalService.create/resolve`, server-side authorization (agents/viewers never approve), and per-repo `review_rules` enforcement — F22 opens one `pr` gate per repo PR through it | **Hard** |
| `cross-cutting/F39-audit-log` | central immutable `audit_log` + `AuditEvent`/`AuditSink` (`deps.audit.emit`) with secret redaction — F22 writes a `repo_id`-tagged entry for every per-repo check/PR/approval/merge and the partial-merge halt | **Hard** |
| `v1/F07-feature-workflow-fsm` | the FSM, `workflow_run`, guard/action registries, transition audit — F22 registers multi-repo guard/action variants (no new states) | **Hard** |
| `v1/F04-repo-policy` | `Policy`/`PolicySnapshot`, `PolicyEvaluator`, `load_policy`, `repo_policy_snapshot` — F22 adds `load_policies` + repo-scoped evaluation | **Hard** |
| `v1/F03-github-app` | repo mirrors under `REPO_CACHE_ROOT`, per-installation tokens, the frozen `GitHubIntegration` (`open_pr`/`update_pr`/`merge_pr`/`get_ci_status`/`get_pr`) and `render_pr_body` — needed once **per repo** | **Hard** |
| `v1/F11-skill-profiles` | `SkillProfileSpec`/`SkillProfile` resolution incl. per-repo override | **Hard** |
| `v1/F01-project-board` | Task with `repo_targets[]` (the widened schema lives on the task) + dependency cycle-detection util reused by `MergePlanBuilder` | **Hard** |
| `v1/F02-spec-engine` | per-repo acceptance-criteria mapping + `ValidationReader` for the `spec_validated` gate per repo | **Soft** (chore/bug repos may have no AC) |
| `v1/F05-hybrid-knowledge-retrieval` | multi-repo `initial_context` (knowledge_scope already lists multiple repos) | **Soft** — stubbable |

External libs: unchanged from F06/F08 (`langgraph`, `sqlalchemy[asyncio]`, `celery`, `pydantic v2`). F22 adds no new third-party dependency; cycle detection is a small graph util (or reuse F01's).

## 6. Acceptance criteria (numbered, testable)

1. **Multi-worktree creation.** Given an `AgentObjective` with two `repo_targets`, `load_context` creates two isolated worktrees (distinct paths under `WORKTREE_ROOT/<agent_run_id>/`), each on the *same* `branch_name` (`forge/TASK-401`) off its own `base_branch`, and persists one `agent_repo_workspace` row per repo with the governing `policy_snapshot_id`.
2. **Per-repo policy enforcement.** A `write_code` to a path allowed by `org/api` policy succeeds; the *same relative path* that is in `org/web`'s `write_deny` is denied when `repo="github.com/org/web"` — proving the guard selects policy by `call.repo`, not a merged policy.
3. **Cross-repo path confinement.** A `read_repo`/`write_code` with `repo="org/api"` but a path resolving into the `org/web` worktree (via `..` or absolute path) is denied and recorded as a `denied` step; the foreign file is never read/written.
4. **Unknown repo rejected.** A tool call whose `repo` is not in `repo_targets` raises `UnknownRepoError`, is recorded as a denied step, and performs no FS/IO.
5. **Per-repo verification.** `MultiRepoVerificationService.run_all` runs each repo's checks using **that repo's** `policy.commands` and effective `skill_profile`; persists one `verification_report` per `(run, repo, attempt)` with the correct `repo_id`; aggregate `all_passed` is true iff every required repo passed.
6. **Any-repo failure fails the aggregate + retries the task.** If `org/web` fails while `org/api` passes, the aggregate report is `failed`, the FSM takes `verifying → executing` (retry budget permitting), and **no** PR is opened for either repo on that attempt.
7. **One PR per changed repo with cross-links.** When all repos pass, `open_group` opens exactly one PR per repo with `has_changes`, creates a `pr_group` row, and each PR body contains (a) that repo's spec-traceability subset and (b) a cross-PR link block naming the other repo PR(s) and the merge order.
8. **No-diff repo ⇒ no PR.** A repo target whose agent output produced no diff has `RepoChangeSet.has_changes=false`, no `pull_request` row, and is excluded from the merge gate; if it is `required_for_merge=true` and its mapped acceptance criteria are unsatisfied, the run escalates to `needs_human_input` instead of proceeding.
9. **Per-repo approval gating.** One pending `pr` approval gate is created **per repo PR** via F36 (`subject_type='pull_request'`, `subject_id=<pull_request.id>`, enforced by F36's `uq_pending_gate (subject_type, subject_id, gate_type)`); approving one repo's PR does **not** make the aggregate gate `can_merge` while another repo is unapproved.
10. **Aggregate merge gate.** `MultiRepoMergeGate.evaluate().can_merge` is true **iff every required, changed repo** has `review_approved AND ci_green AND spec_validated`; otherwise `blocking_reasons` lists each unmet condition prefixed by its repo id.
11. **Ordered merge.** With `org/web depends_on org/api`, `merge_in_order` calls `GitHubIntegration.merge_pr` for `org/api` **before** `org/web`; on full success the `pr_group.status` is `merged`, both `pull_request.merge_state='merged'`, and the FSM reaches `merged → closed`.
12. **Partial-merge halt + escalate.** If the second merge fails after the first succeeded, the merger halts (no further merges, no rollback), records `merged_repo_ids=[org/api]`, sets `pr_group.status='partially_merged'`, returns `MergeGroupOutcome.status='partially_merged'`, and the FSM moves to `needs_human_input` with `escalation_reason` naming merged vs pending repos.
13. **Gate re-check before first merge.** `merge_in_order` re-evaluates the **full** aggregate gate immediately before the first merge; if any repo lost mergeability (e.g. CI flipped red) it merges **nothing** and returns `status='blocked'` with reasons.
14. **Cycle detection.** `MergePlanBuilder.build` raises `CyclicRepoDependencyError` (with the cycle) for mutually-dependent repos and `MultipleOrNoPrimaryError` when not exactly one `role:primary`; the run is refused before any worktree is created.
15. **Single-repo back-compat.** For a Task with exactly one repo target, behaviour is identical to F08: one PR, one approval, F08's single-repo handlers chosen by the dispatch rule; `AgentRunResult.changed_files` and `objective.repo_target` properties return the primary repo's values. A regression test runs F08's happy-path suite unchanged against F22 code.
16. **Per-repo installation token.** Each repo's PR open/status/merge uses **that repo's** GitHub App installation token (asserted via the fake adapter recording which `repo_id`/token was used per call).
17. **Audit completeness.** Every per-repo check, per-repo PR open, per-repo approval decision, each individual merge, and the partial-merge halt write an immutable `AuditEvent` to F39's `audit_log` (and an F07 `workflow_transition` for state changes) with `repo_id` + actor + timestamp.
18. **No self-approval, per repo.** The `agent-runner` identity cannot resolve any repo's PR approval; a `viewer` cannot approve; each repo's `review_rules.required_reviewers`/`min_approvals` are evaluated against that repo's policy independently (AC enforced server-side).
19. **Bounded multi-repo loop.** A single AgentRun spanning N repos never exceeds `max_iterations`; on exhaustion it finalizes `awaiting_input`/`failed` with `needs_human_reason="max_iterations_reached"`, leaving all worktrees cleaned up.

### 6.x Definition of Done
All ACs covered by passing tests; an integration test drives a seeded two-repo task end-to-end (executing → per-repo verify → two PRs → two approvals → ordered merge → closed) against a fake multi-repo GitHub adapter + real Postgres (testcontainer); F08's single-repo flow test passes unchanged (AC 15); coverage of new F22 code ≥ 80% (`backend-tdd`, the bar Forge applies to itself).

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first. Layout: `packages/agent-runtime/tests/` (runtime/sandbox/guard), `packages/workflow-engine/tests/multi_repo/` (plan/verify/pr/merge/guards), `apps/api/tests/` (API), `apps/worker/tests/` (fan-out/merge integration), `apps/web/__tests__/` (review tabs).

**Key fixtures:**
- `two_repo_objective` — `AgentObjective` with `org/api` (primary) + `org/web` (secondary, `depends_on=[org/api]`), distinct `PolicySnapshot`s and skill profiles.
- `tmp_multi_git` — two temp bare repos under `REPO_CACHE_ROOT`, each with `app/`, `tests/`, `AGENTS.md`, `.forge/policy.yaml` (different `write_deny`/`commands`).
- `ScriptedModelClient` (from F06) extended to emit repo-scoped tool calls (`{name, args:{repo, ...}}`).
- `fake_multi_github` — in-memory `GitHubIntegration` keyed by `repo_id`, recording per-repo `open_pr`/`update_pr`/`get_ci_status`/`merge_pr` and the `installation_id`/token used; settable per-repo CI status and a `fail_merge_for={repo_id}` switch.
- `pg` — Postgres testcontainer at Alembic head (F22 migration applied).
- `make_multi_run` — seeds `workflow_run` + `pr_group` + per-repo `pull_request`/`approval_request` at a chosen state.

**Unit — merge plan (`test_merge_plan.py`):**
- `test_topo_order_simple` — `web depends_on api` ⇒ order `[api, web]`.
- `test_cycle_detected` — mutual depends ⇒ `CyclicRepoDependencyError(cycle=...)` (AC 14).
- `test_requires_exactly_one_primary` — zero/two primaries ⇒ `MultipleOrNoPrimaryError` (AC 14).
- `test_unknown_dependency_repo` — `depends_on` an id not in targets ⇒ `UnknownDependencyRepoError`.

**Unit — multi-worktree + guard (`test_multi_workspace.py`, `test_multi_policy_guard.py`):**
- `test_create_all_distinct_worktrees_same_branch` (AC 1).
- `test_change_sets_has_changes_flag` — repo with edits ⇒ true; untouched ⇒ false (AC 8).
- `test_guard_selects_policy_by_repo` — same path allowed in A, denied in B (AC 2).
- `test_cross_repo_path_escape_denied` — `repo=A`, path into B ⇒ denied (AC 3).
- `test_unknown_repo_raises` (AC 4).

**Unit — verification fan-out (`multi_repo/test_verification.py`):**
- `test_runs_each_repo_with_own_commands` — asserts api uses api's `commands`, web uses web's (AC 5).
- `test_aggregate_all_passed` / `test_aggregate_any_failed` (AC 5, 6).
- `test_persists_report_per_repo_attempt` — unique `(run, repo, attempt)` (AC 5).

**Unit — PR group (`multi_repo/test_pr_group.py`):**
- `test_one_pr_per_changed_repo` — two changed repos ⇒ two `open_pr` calls + two `pull_request` rows (AC 7).
- `test_no_pr_for_no_diff_repo` (AC 8).
- `test_cross_pr_link_block_present` — each body names the other PR + merge order (AC 7).
- `test_one_pending_approval_per_repo` — unique index holds; second build does not duplicate (AC 9).

**Unit — aggregate gate + merger (`multi_repo/test_merge.py`):**
- `test_gate_blocks_until_all_repos_ready` — one repo unapproved ⇒ `can_merge=false` with repo-prefixed reasons (AC 9, 10).
- `test_gate_green_when_all_ready` (AC 10).
- `test_merge_order_respected` — `merge_pr` call order = `[api, web]` (AC 11).
- `test_partial_merge_halts` — `fail_merge_for=org/web` ⇒ api merged, web not, `partially_merged`, no rollback (AC 12).
- `test_gate_recheck_before_first_merge_blocks` — CI flips red between approval and merge ⇒ nothing merged, `blocked` (AC 13).
- `test_per_repo_installation_token` (AC 16).

**Unit — FSM dispatch (`multi_repo/test_dispatch.py`):**
- `test_single_repo_uses_f08_handlers` — len 1 ⇒ F08 path (AC 15).
- `test_multi_repo_uses_f22_handlers` — len 2 ⇒ F22 guards/actions, no new states introduced.

**API (`apps/api/tests/test_multi_repo_review.py`):**
- `test_repos_summary_endpoint`, `test_pr_group_endpoint`.
- `test_approve_one_repo_then_aggregate_still_blocked` (AC 9).
- `test_merge_group_blocked_returns_reasons` / `test_merge_group_merges_when_all_green` (AC 10, 11).
- `test_agent_runner_cannot_approve_any_repo`, `test_viewer_cannot_approve` (AC 18).

**Integration — full flow (`apps/worker/tests/test_multi_repo_flow.py`, Postgres + fakes):**
- `test_two_repo_happy_path_to_merged` — seed two-repo `task_ready`; drive `advance_workflow`; assert executing → per-repo verify (chord) → two PRs → two approvals → ordered merge → `merged → closed`; one `pr_group`, two reports per attempt, audit events with `repo_id` (AC 1,5,7,9,11,17).
- `test_one_repo_fails_retries_whole_task` (AC 6).
- `test_partial_merge_to_needs_human_input` (AC 12).
- `test_no_diff_repo_skipped` (AC 8).
- `test_f08_single_repo_regression` — run F08's `test_happy_path_to_merged` unchanged (AC 15).

**Frontend (`apps/web/__tests__/multiRepoReview.test.tsx`):**
- `test_renders_repo_tabs_for_multi_repo` and `no_tabs_for_single_repo` (AC 15 UI).
- `test_merge_group_button_disabled_until_gate_green` (AC 10).
- `test_keyboard_tab_switch_and_merge_shortcut`.

## 8. Security & policy considerations

- **Per-repo policy is non-negotiable.** Every tool call is evaluated against `policies[call.repo]` — there is **no merged super-policy** and **no implicit default repo**. An out-of-scope or unknown `repo` is denied, not coerced (AC 2, 4). This upholds Build Constraint 2 ("the agent never self-assigns permissions or expands its own scope") across repos.
- **Worktree isolation per repo (V1 sandbox boundary applies per repo).** Each repo gets a private worktree; path confinement is asserted against *that repo's* worktree, so a tool scoped to repo A structurally cannot read/write repo B (AC 3). Cross-task isolation from F06 is preserved; F22 adds cross-repo isolation within a task.
- **Merge is human-gated per repo AND aggregate-gated.** No PR merges until **every** required repo PR is approved + CI-green + spec-validated; each repo's `review_rules` (`required_reviewers`, `min_approvals`) are enforced independently (AC 10, 18). The agent-runner identity can approve none of them.
- **No force-merge; ordered + halt-on-failure.** There is no endpoint to merge a single repo out of band or to bypass the aggregate gate. The merger re-checks the full gate before the first merge (AC 13) and halts on the first failure (AC 12). The residual partial-merge window (one repo merged, a later one failing) is an accepted, **documented** V2 limitation — Forge does **not** auto-revert merged PRs (revert is risky and itself needs review); it escalates to a human with a precise record of what merged.
- **Per-installation least privilege.** Each repo's GitHub operations use that repo's installation token (AC 16); a token scoped to `org/api` is never used against `org/web`.
- **Audit + redaction across repos.** Every per-repo check/PR/approval/merge writes an immutable `AuditEvent` to F39's central, tamper-evident `audit_log` tagged with `repo_id` (AC 17); verification logs and PR bodies pass F39's secret-redaction filter before persistence per repo.
- **Knowledge scope honored.** The agent's `read_repo`/`read_knowledge` are limited to repos in `repo_targets` ∪ `knowledge_scope.repos`; a repo not in scope cannot be read even if mirrored.
- **MCP read-only + BYOK preserved.** F22 changes nothing about model or MCP access: the single AgentRun uses one BYOK `ModelConfig` (`api_key_ref`, never a raw key) for all repos, and MCP access stays read-only / query-through (`allow_write: false` default) with no per-repo write path. Hybrid retrieval (F05) supplies `initial_context` across the in-scope repos unchanged.
- **Fail-closed config.** A missing/invalid `.forge/policy.yaml` in *any* target repo, a missing mirror, a dependency cycle, or no/multiple primaries all **block the run before execution** with a named cause (AC 14) — never a partial silent run.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** F22 touches the agent runtime (multi-worktree + repo-scoped tools/guard), the workflow engine (per-repo verification fan-out/fan-in, multi-PR builder, aggregate gate, ordered merger, FSM dispatch), the API, the web review page, and a non-trivial migration — and the cross-repo merge coordination is genuinely subtle. Rough split: multi-worktree runtime + repo-scoped guard (M), verification fan-out/fan-in chord (M), PR group + cross-links + per-repo approvals (M), aggregate gate + ordered merger + partial-merge handling (M), API + web tabs + merge panel (M), migration + back-compat (S).

**Key risks:**
1. **No atomic cross-repo merge (highest).** Partial-merge is physically possible. *Mitigation:* strict all-green pre-gate, immediate-before-first-merge re-check, ordered merge by dependency DAG, halt-on-failure with precise audit + human escalation; document the limitation; leave a hook for a future merge-queue/2-phase approach (§12).
2. **Contract widening drift with F06/F08.** Widening frozen `AgentObjective`/`AgentRunResult`/`RepoTarget`/`ToolContext`. *Mitigation:* keep V1 fields as computed back-compat properties; AC 15 + replaying F08's suite unchanged is the guard; freeze the widened DTOs in §4 first.
3. **Fan-out/fan-in correctness under Celery at-least-once.** A repo verification chord could double-run or fan in twice. *Mitigation:* idempotent per-`(run, repo, attempt)` reports (unique index), the chord callback keyed by `(run, attempt)`, status-guard before emitting `checks_*`.
4. **Disk blow-up from N worktrees/run.** *Mitigation:* `cleanup_all` on every terminal state, document `WORKTREE_ROOT` sizing, optionally shallow worktrees.
5. **Reasoning-budget exhaustion across many repos.** A single agent over many repos may loop. *Mitigation:* raised `max_iterations` default (60) + per-repo progress tracking; bounded loop AC 19; large repo-counts can be split into multiple tasks (advise in docs).
6. **Mixed-language verification across repos.** Each repo may use a different toolchain. *Mitigation:* verification already runs each repo's own `policy.commands`; the V1 python-first parser limitation (F08) carries over per repo — non-python repos get exit-code-only results until pluggable parsers land.

## 10. Key files / paths (exact)

```
# Agent runtime (widen to multi-worktree)
packages/agent-runtime/forge_agent/sandbox.py            # + MultiRepoWorkspace
packages/agent-runtime/forge_agent/policy_guard.py       # + MultiRepoPolicyGuard
packages/agent-runtime/forge_agent/tools/base.py         # ToolContext.repo; repo-scoped arg schemas
packages/agent-runtime/forge_agent/tools/{read_repo,write_code,run_tests,read_knowledge}.py  # repo arg
packages/agent-runtime/forge_agent/nodes.py              # load_context all worktrees; per-repo verify
packages/agent-runtime/forge_agent/objective.py          # build repo_targets[]/policies/agents_md/skills
packages/agent-runtime/tests/test_multi_workspace.py
packages/agent-runtime/tests/test_multi_policy_guard.py

# Workflow engine (per-repo verify, multi-PR, aggregate gate, ordered merge, dispatch)
packages/workflow-engine/forge_workflow/multi_repo/plan.py
packages/workflow-engine/forge_workflow/multi_repo/verification.py
packages/workflow-engine/forge_workflow/multi_repo/pr.py
packages/workflow-engine/forge_workflow/multi_repo/merge.py
packages/workflow-engine/forge_workflow/transitions/handlers_multi_repo.py
packages/workflow-engine/forge_workflow/transitions/guards_multi_repo.py
packages/workflow-engine/tests/multi_repo/{test_merge_plan,test_verification,test_pr_group,test_merge,test_dispatch}.py

# Policy SDK
packages/policy-sdk/forge_policy/loader.py               # + load_policies()
packages/policy-sdk/forge_policy/evaluator.py            # + RepoScopedPolicyEvaluator

# Contracts (widen, back-compat)
packages/contracts/forge_contracts/agent.py             # RepoTarget, AgentObjective, AgentRunResult, RepoChangeSet

# API
apps/api/app/routers/runs_multi_repo.py
apps/api/app/routers/approvals.py                        # extend: repo-scoped decision + aggregate gate
apps/api/app/services/merge_group_service.py
apps/api/tests/test_multi_repo_review.py

# Worker
apps/worker/forge_worker/tasks/verification_multi_repo.py
apps/worker/forge_worker/tasks/pr_multi_repo.py
apps/worker/forge_worker/tasks/merge_multi_repo.py
apps/worker/tests/test_multi_repo_flow.py

# Web
apps/web/app/(dashboard)/projects/[projectId]/tasks/[taskId]/review/page.tsx   # repo tabs
apps/web/components/review/MergeGroupPanel.tsx
apps/web/components/board/RepoLanes.tsx
apps/web/lib/api/{prGroup,merge}.ts
apps/web/__tests__/multiRepoReview.test.tsx

# Migration
packages/db/forge_db/models/{pr_group,agent_repo_workspace}.py                # new models
packages/db/migrations/versions/00XX_f22_multi_repo.py   # pr_group, agent_repo_workspace; extend
                                                         # verification_report/pull_request (approval_request unchanged — F36)
```

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md → **Task Schema** (`repo_targets[]` with `branch_strategy`, `branch_prefix`, `base_branch`, `worktree`) — the data the run iterates.
- FORGE_SPEC.md → **Phased Roadmap → Phase 2 (V2)**: "Multi-repo task execution" (this slice).
- FORGE_SPEC.md → **Repo Policy System** (per-repo `.forge/policy.yaml`: `commands`, `write_rules`, `review_rules`) — F22 enforces these *per repo*.
- FORGE_SPEC.md → **Human Approval System** (PR approval "Always required before merge"; "Approval UI Must Show" 9 items) — extended per repo + aggregate gate.
- FORGE_SPEC.md → **Multi-Agent Orchestration → Pattern Selection Guide** ("Parallel independent subtasks → Fan-out/Fan-in") — explains why coupled cross-repo changes stay single-agent, not fan-out, in V2.
- FORGE_SPEC.md → **Security** (per-repo policy evaluation, sandbox isolation via git worktrees, immutable audit, secret redaction) — applied per repo.
- Open SWE (isolated sandbox per task, `AGENTS.md` repo-context loading, PR-opening workflow): https://github.com/langchain-ai/open-swe · https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents (research report §"Symphony and Open SWE") — the per-repo worktree + per-repo AGENTS.md pattern generalizes the single-repo case.
- Symphony (task-as-control-plane; a task is the unit of work that maps to workspaces): https://openai.com/index/open-source-codex-orchestration-symphony/ (research report cite:126).
- LangGraph (single `StateGraph` agent loop unchanged; only tool/sandbox layers became repo-aware): https://langchain-ai.github.io/langgraph/
- Builds directly on: `docs/implementation-slices/v1/F06-single-execution-agent.md`, `.../v1/F07-feature-workflow-fsm.md`, `.../v1/F08-plan-execute-verify-pr-approval.md`, `.../v1/F04-repo-policy.md`, `.../v1/F03-github-app.md`, `.../cross-cutting/F36-human-approval-system.md`, `.../cross-cutting/F39-audit-log.md`.

## 12. Out of scope / future

- **Atomic cross-repo merge / true 2-phase commit.** V2 ships pre-gate + ordered merge + halt-and-escalate; a real atomic guarantee (merge-queue integration, GitHub merge-queue, or a saga with automated cross-repo revert) is future work. The `MultiRepoMerger` + `pr_group.status` are designed to host it.
- **Automated partial-merge rollback.** V2 escalates to a human on partial merge; auto-reverting already-merged PRs is deferred (revert must itself be reviewed).
- **Fan-out to multiple agents per repo (supervised multi-agent).** F22 is one agent over N worktrees. Per-repo specialist subagents (implementer per repo, cross-repo reviewer) are **Phase 3** (Supervisor pattern), gated by `subagent_policy`.
- **Cross-repo dependency *content* analysis.** F22 honors explicitly declared `depends_on` merge ordering; it does not auto-infer dependencies from code (e.g. detecting that the client uses the new endpoint). Declared edges only.
- **Per-repo divergent retry.** V2 retries the **whole task** when any repo fails (single AgentRun). Retrying only the failed repo while preserving passed repos' diffs is a future optimization.
- **>2 GitHub orgs / mixed providers (GitLab) in one task.** V2 supports multiple GitHub repos (possibly across installations); GitLab/mixed-provider multi-repo is gated on the GitLab adapter (V2 integrations, separate slice).
- **Container/Firecracker per-repo sandboxes.** V2 uses git worktrees per repo; stronger per-repo isolation rides on the container-sandbox slice (V2/V3).
- **Cross-repo coverage/aggregate quality dashboards.** Per-repo verification persists; an aggregate spec-validation dashboard across repos is the "Spec validation dashboard" V2 slice, not here.
