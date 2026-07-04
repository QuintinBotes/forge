# F08 — Plan → Execute → Verify → PR → Approval Flow

> Phase: v1 · Spec module(s): Orchestrator / Workflow Engine (effect bodies + merge-gate data), Review & Approval Layer (PR gate), Spec Gating (validation→merge), GitHub App integration (PR open/merge/CI) · Status target: "Done" = a task in `task_ready` with an approved plan can be driven through `executing → verifying → pr_opened → awaiting_review → merged → closed` end-to-end, where F08 supplies the **authoritative gate verification** (`run_checks` effect), the **spec-traceable PR open** (`open_pr_with_spec_traceability` effect), the **PR approval gate** (`create_pr_approval` effect + approval service), and the **merge gate** (review-approved AND CI-green AND spec-validated, computed and enforced before the GitHub merge). Every check result, PR, approval decision, and merge is persisted and audit-logged. Lint + types + `pytest` green on the new F08 modules and the API/worker/web tests.

---

## 1. Intent — what & why

This slice implements the **implementation arc** of the default feature workflow: the segment that runs once a task has an approved plan and is `task_ready`, ending when the resulting PR is merged and the run closes. F08 is the **glue + gate** slice — it does **not** own the FSM, the agent, GitHub I/O, or the spec engine; it owns the four workflow effect bodies and the approval/merge logic that those subsystems leave unimplemented.

The FSM transition table is owned by **F07** (`v1/F07-feature-workflow-fsm`). F07's bundled `default_feature.yaml` already declares the exact transitions, guards, and effect **names** F08 implements (see F07 §4 DSL). F08 registers the named **effect bodies** into F07's worker-side `EffectRegistry` and emits follow-up **events** (`POST /workflow/runs/{id}/events`); it does **not** add a driver loop, does **not** re-define F07's guards, and does **not** own retry/backoff.

Concretely, F08 owns four things:

1. **`run_checks`** (effect for `executing --agent_completed--> verifying`) — a deterministic, **independent** gate verifier. It materialises a fresh worktree from the agent's produced branch, runs the repo policy commands (F04) selected by the skill directives (F11), parses each tool's output into typed results, enforces the coverage threshold, persists `verification_report` + `check_result`, uploads logs, submits a `ValidationReport` to the Spec Engine (F02), and emits `checks_passed` / `checks_failed` with a `payload.checks` map. This is authoritative and independent of the agent's in-loop self-checks (F06 also runs verification, but the gate must not be self-certified by the agent — see §3.3 reconciliation).
2. **`open_pr_with_spec_traceability`** (effect for `verifying --checks_passed--> pr_opened`) — composes a `SpecTraceability` covering every acceptance criterion plus a body containing the verification table, confidence, and knowledge provenance, opens the PR via the GitHub App (F03), persists the `pull_request` row, then emits `pr_ready`.
3. **`create_pr_approval`** (effect for `pr_opened --pr_ready--> awaiting_review`) + **the approval/merge gate** — creates the `ApprovalRequest(kind=pr)`, surfaces the 9-item approval context, and on `approve` enforces that **review_approved AND ci_green AND spec_validated** all hold before calling `GitHubIntegration.merge_pr` and emitting `review_approved`.
4. **Merge-gate data + approval REST surface** — the `MergeGateEvaluator` that computes the three booleans F07's `merge_ready` guard reads from the event payload, and the `/approvals/*` endpoints + `ApprovalService`.

**F08 does NOT own:** the FSM engine / transition table / guards / preconditions / retry budget / backoff / escalation routing (all **F07**); the agent loop and `start_agent_run` effect (**F06**); GitHub auth, webhook ingestion, `open_pr`/`merge_pr`/`get_ci_status` transport, and `render_pr_body` (**F03**); the `SpecManifest`, `ValidationReport` builder, and traceability matrix (**F02**); the baseline `workflow_run`/`agent_runs`/`approval_request`/`task` tables (**F00 foundation**).

Why it matters: this slice makes Forge's "human-in-the-loop, spec-gated, policy-aware" promise real for the part of the lifecycle where the agent's code is verified, traced, gated, and merged. Without it, F06 produces a branch but nothing independently verifies it, traces it to the spec, gates it on human approval + CI + validation, or merges it.

---

## 2. User-facing behavior / journeys

**Journey A — Happy path (engineer + agent):**
1. An engineer has an approved spec + plan for `TASK-123` (`skill_profile: backend-tdd`). The run is `task_ready`.
2. They trigger execution (board action → `POST /workflow/runs/{id}/events {type: execute}`). F07 advances `task_ready → executing` and dispatches `start_agent_run` (F06). The card shows a live "Executing" badge + streaming run trace (F10).
3. The agent finishes and emits `agent_completed`. F07 advances `executing → verifying` and dispatches **F08's `run_checks`**. The card shows "Verifying" with a live checklist: Lint ✓, Type check ✓, Tests ✓ (142 passed), Coverage ✓ (87% ≥ 80%).
4. `run_checks` emits `checks_passed` (`payload.checks` all true). F07's `all_checks_passed` guard holds → `verifying → pr_opened`, dispatching **F08's `open_pr_with_spec_traceability`**. A PR appears in GitHub, linked on the timeline; its body shows a **Spec Traceability** table (A1 ✓, A2 ✓), verification results, confidence, and the knowledge chunks used.
5. `open_pr_with_spec_traceability` emits `pr_ready` → F07 advances `pr_opened → awaiting_review`, dispatching **F08's `create_pr_approval`**, which creates an `ApprovalRequest(kind=pr)`. Reviewers get a Slack/email notification (F16) with a deep link.
6. The engineer opens the **Review** page, sees the diff, verification, traceability matrix, confidence, knowledge provenance, and risk flags, then clicks **Approve**.
7. `ApprovalService.resolve(approve)` evaluates the merge gate: review approved ✓, CI green ✓ (fresh `get_ci_status`), spec validated ✓. It calls `GitHubIntegration.merge_pr` (squash, sha-pinned), records `merged_sha`/`merged_at`, and emits `review_approved` (payload booleans) → F07 reaches `merged`. On the F03 `pull_request_merged` webhook, F08 emits `close` → `closed`. The board updates.

**Journey B — Checks fail, agent retries (F07-driven):**
- `run_checks` emits `checks_failed` (`payload.checks.test == false`). F07's `retry_budget_remaining` guard holds (attempt 1 of 3) → `verifying → executing`, dispatching `start_agent_run` again after F07's exponential backoff (30s). The agent re-runs; if a later attempt passes, flow rejoins Journey A. (F08 only emits the event; F07 owns the retry count + backoff.)

**Journey C — Retry budget exhausted:**
- After the 4th `checks_failed`, F07's `retry_budget_exhausted` guard matches → `verifying → needs_human_input` with `pause_and_notify`. The task shows "Needs human input" with the last verification report + logs. No PR is opened.

**Journey D — Low confidence escalation (F06/F07-driven):**
- The agent completes with confidence `0.61 < 0.72` and emits `agent_low_confidence` (not `agent_completed`). F07's `confidence_below_threshold` guard holds → `executing → needs_human_input` **before** verification or any PR. F08's `run_checks` is never dispatched.

**Journey E — Reviewer requests changes / rejects:**
- **Request changes** (with a note): `ApprovalService` resolves the approval `changes_requested` and emits `review_changes_requested`. Per F07's DSL this routes `awaiting_review → executing`, re-running the agent with the reviewer note as added context. **Reject**: the approval resolves `rejected`, F08 closes the PR (`update_pr state=closed`) and emits `cancel`, routing the run to `cancelled`.

**Journey F — Merge blocked by gate:**
- A reviewer approves, but CI is still red. `ApprovalService.resolve(approve)` returns `merged=false` with `blocking_reasons: ["CI status is failure (1 of 3 checks)"]`; **no** `review_approved` event is emitted, the run stays `awaiting_review`, and the UI shows a blocking banner. When CI turns green, an authorized user can re-approve (the gate re-reads `get_ci_status`) and the merge proceeds.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

Models live in `packages/db/forge_db/models/`; migrations in `packages/db/forge_db/migrations/versions/` (same locations as F03/F06/F07). F08 **adds three tables** and **extends the baseline `approval_request`** (foundation). It does **not** touch `workflow_run` (F07 already owns `retry_count`, `failure_reason`, `paused_from_state`, etc.; F08 reads them).

**New table `verification_report`** — one per verification attempt.

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workflow_run_id` | `uuid` FK → `workflow_run.id` | indexed |
| `agent_run_id` | `uuid` FK → `agent_runs.id` | the attempt that produced the code |
| `attempt` | `int` not null | 1-based; `workflow_run.retry_count + 1` at run time |
| `status` | `text` not null | `passed` \| `failed` \| `error` |
| `coverage_pct` | `numeric(5,2)` null | overall coverage if a coverage check ran |
| `coverage_threshold` | `int` null | from skill directives `min_test_coverage` |
| `all_passed` | `boolean` not null | denormalized convenience |
| `head_sha` | `varchar(40)` not null | commit the checks ran against |
| `created_at` | `timestamptz` default now() | |

Unique constraint `(workflow_run_id, attempt)`.

**New table `check_result`** (children of a report).

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `verification_report_id` | `uuid` FK → `verification_report.id` ON DELETE CASCADE | |
| `name` | `text` not null | `lint` \| `type_check` \| `test` \| `coverage` |
| `status` | `text` not null | `passed` \| `failed` \| `skipped` \| `error` |
| `command` | `text` not null | the exact policy command executed |
| `exit_code` | `int` null | null if `skipped` |
| `duration_ms` | `int` not null | |
| `summary` | `text` not null | one-line human summary (e.g. "142 passed, 0 failed") |
| `metrics` | `jsonb` not null default `{}` | parsed numbers (passed, failed, errors, coverage_pct) |
| `output_ref` | `text` null | MinIO object key for full stdout/stderr |
| `created_at` | `timestamptz` default now() | |

**New table `pull_request`** — system-of-record for the agent-opened PR (F03 has no PR table).

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workflow_run_id` | `uuid` FK → `workflow_run.id` | unique (one PR per run in V1) |
| `repo_id` | `text` not null | `github.com/org/api` (spec form) |
| `repo_full_name` | `text` not null | `org/api` (F03 form, used for GitHub calls) |
| `installation_id` | `int` not null | resolved from `RepositoryConnection` (F03) |
| `provider_pr_number` | `int` null | GitHub PR number (null until opened) |
| `url` | `text` null | |
| `head_branch` | `text` not null | e.g. `forge/TASK-123` |
| `base_branch` | `text` not null | e.g. `main` |
| `head_sha` | `varchar(40)` not null | refreshed from F03 push/PR events |
| `title` | `text` not null | |
| `body` | `text` not null | rendered markdown body (F03 `render_pr_body`) |
| `confidence` | `numeric(4,3)` null | agent confidence at PR time |
| `traceability` | `jsonb` not null | list of `TraceabilityRow` (see §4) |
| `ci_status` | `text` not null default `pending` | `pending` \| `success` \| `failure` \| `error` |
| `spec_validated` | `boolean` not null default false | from the F02 `ValidationReport` |
| `validated_head_sha` | `varchar(40)` null | sha the passing validation ran against |
| `merged_sha` | `varchar(40)` null | set on merge (merge idempotency key) |
| `opened_at` | `timestamptz` null | |
| `merged_at` | `timestamptz` null | |

**Extend baseline `approval_request`** (created in `v1/F00-foundation-substrate`; used today by F07 as `ApprovalRequest(kind=spec|plan|pr)`). F08 reuses the `kind='pr'` value and **adds, additively, any columns not already present**:

| column | type | notes |
|---|---|---|
| `decision_note` | `text` null | reviewer free text |
| `resolver_user_id` | `uuid` null FK → `user.id` | who decided |
| `requested_by` | `uuid` null FK → `user.id` | null when agent/effect-requested |
| `resolved_at` | `timestamptz` null | |

Baseline columns assumed present from foundation: `id`, `workflow_run_id`, `agent_run_id`, `kind` (`spec|plan|pr|deploy`), `status` (`pending|approved|rejected|changes_requested`), `requested_at`. F08 adds the partial unique index: `CREATE UNIQUE INDEX uq_pending_pr_approval ON approval_request (workflow_run_id) WHERE kind='pr' AND status='pending'` (one live PR approval per run). The migration must reconcile (no-op) if a column already exists.

Audit: all writes are recorded in the foundation immutable audit log (F00 / `v1/F14-observability-audit`). FSM transitions are recorded by F07's `workflow_transition`. F08 emits audit entries for `check_completed`, `pr_opened`, `approval_requested`, `approval_resolved`, `merge_blocked`, `merged`, each secret-redacted with actor + payload hash.

### 3.2 Backend (FastAPI routes + services/packages)

**Package `packages/workflow-engine/forge_workflow/`** — F08 adds **new subpackages** under F07's package without modifying F07's `engine.py`/`guards.py`/`effects.py`/`dsl.py`. These are workflow **effect bodies** + gate logic, registered into F07's public `EffectRegistry` at worker boot:

- `verification/parsers.py` — pure functions: `parse_ruff`, `parse_mypy`, `parse_pytest`, `parse_coverage` → `CheckResult` (§4).
- `verification/runner.py` — `WorktreeCommandRunner(SandboxCommandRunner)` (§4): materialises a verify worktree from the agent's branch via F06's `WorktreeSandbox`, runs a command as a confined subprocess, gating each command through F04's `PolicyEvaluator` (`run_command` decision) so only `policy.commands` values execute.
- `verification/service.py` — `VerificationService` (§4): resolves checks from skill directives + policy, runs them, persists `verification_report` + `check_result`, uploads logs, builds the F02 `ValidationReport`, returns the report.
- `pr/traceability.py` — `SpecTraceabilityComposer.compose(manifest, agent_result) -> list[TraceabilityRow]` (covers every manifest AC).
- `pr/builder.py` — `PRBuilderService` (§4): composes `PullRequestSpec` (F03) incl. `SpecTraceability`, renders the narrative body sections (verification + confidence + knowledge), opens the PR via `GitHubIntegration`, persists `pull_request`.
- `gates/merge.py` — `MergeGateEvaluator.evaluate(workflow_run_id) -> MergeGateResult` (§4).
- `effects/handlers.py` — async effect bodies `run_checks`, `open_pr_with_spec_traceability`, `create_pr_approval`; `register_f08_effects(registry: EffectRegistry, deps)` wires them by name. Each handler does its work then calls back `POST /workflow/runs/{id}/events` (or `deliver_event`) with the follow-up event + `idempotency_key`.

**App `apps/api/forge_api/`** (not `apps/api/app/`):

- `routers/approvals.py` — REST endpoints (§4): list/get approvals, post decision.
- `routers/runs_verification.py` — GET verification report, GET PR + traceability, GET check log (signed URL).
- `services/approval_service.py` — `ApprovalService.resolve(approval_id, decision, principal, note)`: authorises the actor (RBAC + no self-approval + policy `review_rules`), records the decision, and on `approve` runs `MergeGateEvaluator`; if mergeable, calls `GitHubIntegration.merge_pr`, records `merged_sha`, and emits `review_approved`. On `request_changes`/`reject` emits `review_changes_requested`/`cancel`. Returns `ApprovalResolution`.
- `schemas/approval.py`, `schemas/verification.py`, `schemas/pull_request.py` — API request/response Pydantic schemas (thin views over the contracts in §4).

Shared DTOs (`VerificationReport`, `CheckResult`, `TraceabilityRow`, `MergeGateResult`) live in `packages/contracts/forge_contracts/verification.py` so web/api/worker share one definition, matching how F03/F06/F07 freeze contracts.

### 3.3 Worker / agent runtime (Celery tasks)

There is **no F08 driver task** — the FSM is event-driven (F07). The chain `executing → verifying → pr_opened → awaiting_review` advances purely through effect-handler callbacks emitting events. F08 wires its effect bodies into F07's `run_effect`:

- At worker boot, `register_f08_effects(EffectRegistry, deps)` registers `run_checks`, `open_pr_with_spec_traceability`, `create_pr_approval`. When F07's `run_effect(run_id, effect, skill, payload)` resolves one of these names, it invokes the F08 handler.
- `run_checks` handler: reads the latest `agent_runs` row for the run (F06's `branch_name`, `head_commit_sha`, `acceptance_criteria`, `changed_files`, `confidence`), constructs `VerificationService` with a `WorktreeCommandRunner`, runs the checks, persists the report, submits the F02 validation, and emits `checks_passed`/`checks_failed`.
- `open_pr_with_spec_traceability` handler: builds + opens the PR via `PRBuilderService`, persists `pull_request`, emits `pr_ready`.
- `create_pr_approval` handler: creates `ApprovalRequest(kind=pr)`, emits the F16 notification event (`approval_requested`).

**Worktree lifecycle reconciliation (important).** F06 cleans up its worktree on terminal `succeeded` (F06 AC#12), but guarantees the branch + commits remain resolvable in the repo cache and are pushed to the mirror used by F03's `open_pr`. F08 therefore does **not** reuse the agent's worktree; it **materialises a fresh, throwaway verify worktree** off the task branch tip:
`WorktreeSandbox.create(repo, base_branch=<branch_name>, branch_name=f"forge-verify/{run_id}-{attempt}")` checks out the task branch's HEAD (== `head_commit_sha`) into a new working tree; F08 runs `install` then the checks there, then `cleanup(keep_branch=False)`. This requires F06/F03 to have pushed the branch (true at `pr`-time) — a hard dependency captured in §5.

**Verification independence reconciliation.** F07's dependency note (F07 §5) tentatively attributes `run_checks` to agent-runtime, and F06 already runs the skill profile's verification steps inside its loop (returning `AgentRunResult.verifications`). F08 is the **authoritative owner** of the `run_checks` effect body: it re-runs the policy commands in a clean checkout so the gate (a) cannot be self-certified or gamed by the agent, (b) is reproducible and auditable, and (c) yields the persisted `verification_report` consumed by the PR body, the approval UI, and the merge gate. F06's in-loop `verifications` are advisory (they drive the agent's own iteration/confidence) and MAY seed the runner's cache but never substitute for the authoritative run.

**Queue:** `run_checks`, `open_pr_with_spec_traceability` are routed to a dedicated `verification` Celery queue (long, CPU/IO-heavy) so they don't starve indexing/sync workers.

### 3.4 Frontend / UI (Next.js routes/components)

**App `apps/web/`:**

- Route `app/(dashboard)/projects/[projectId]/tasks/[taskId]/review/page.tsx` — the **PR Approval Review** page implementing the spec's "Approval UI Must Show" 9-item checklist:
  1. Task goal + spec requirements (task + manifest).
  2. Changed files with diff preview + syntax highlighting (`components/review/DiffViewer.tsx`; diff fetched via F03 `get_file`/PR diff proxy).
  3. Verification results table (`components/review/VerificationPanel.tsx`).
  4. Spec traceability matrix (`components/review/TraceabilityMatrix.tsx`).
  5. Knowledge provenance list (`components/review/KnowledgeProvenance.tsx`).
  6. Confidence score + rationale (`components/review/ConfidenceBadge.tsx`).
  7. Risk flags — policy warnings, security findings, uncertain areas (`components/review/RiskFlags.tsx`).
  8. Full run trace (reuse F10 `RunTraceTimeline`).
  9. Actions: Approve / Reject / Request changes (`components/review/ApprovalActions.tsx`).
- `components/board/WorkflowStatusBadge.tsx` — renders `executing | verifying | pr_opened | awaiting_review | needs_human_input | merged | cancelled` with live updates.
- `components/board/VerificationChecklist.tsx` — live lint/type/test/coverage checklist on the task card (subscribes to F07/F10 run events over SSE/WebSocket).
- Data layer: TanStack Query hooks `useVerificationReport(runId)`, `usePullRequest(runId)`, `useApproval(approvalId)`, mutation `useResolveApproval()` with optimistic update + rollback (board UX standards).

Keyboard-first: `a` = approve (confirm), `r` = request changes, `x` = reject, `j/k` = navigate diff hunks.

### 3.5 Infra / deploy (compose, helm, caddy)

- **Worker toolchain + volumes:** the Celery `worker` image must run the repo's check commands. V1 (Python golden set) includes `uv`, `ruff`, `mypy`, `pytest`, `pytest-cov`, `git`. The verify worktree uses F06's named volumes `forge-worktrees` + `forge-repo-cache` (already mounted into `worker`). Non-Python repos are out of scope for V1 (§12).
- **MinIO bucket `forge-checks`** for verification logs (full stdout/stderr per check), with a lifecycle expiry (e.g. 30 days) — add to the bucket bootstrap in `deploy/scripts/install.sh`.
- **Dedicated Celery queue `verification`** — route `run_checks` / `open_pr_with_spec_traceability` to it in the `worker` command in `deploy/docker-compose.yml`.
- **GitHub webhook events:** CI/review/merge ingestion is owned by F03. F08 consumes the resulting `ci_status_changed`, `review_submitted`, `pull_request_merged`, `pull_request_opened`/`push_received` domain events. The required webhook events (`check_suite`, `status`, `pull_request_review`, `pull_request`) are documented by F03 in `docs/integrations/github-app-setup.md`; F08 adds no new webhook config.
- Helm: N/A for V1 (V2). No Caddy changes.

---

## 4. Public interfaces / contracts

### 4.1 Consumed contracts (owned by siblings — referenced verbatim, not re-defined)

**Agent result (F06, frozen in `forge_contracts`)** — F08 consumes this exact shape; it does **not** define its own `AgentRunResult`:

```python
class AcceptanceCriterionResult(BaseModel):   # F06
    id: str; satisfied: bool; evidence: str; test_refs: list[str] = []

class VerificationResult(BaseModel):          # F06 (advisory, in-loop)
    step: str; passed: bool; summary: str
    coverage_pct: float | None = None; raw_output_ref: str | None = None

class AgentRunResult(BaseModel):              # F06 — consumed by F08
    agent_run_id: UUID
    status: Literal["succeeded", "failed", "awaiting_input", "cancelled"]
    confidence: float
    summary: str
    acceptance_criteria: list[AcceptanceCriterionResult]
    verifications: list[VerificationResult]
    branch_name: str
    base_commit_sha: str
    head_commit_sha: str | None
    changed_files: list[str]
    diff_stat: dict[str, int]
    needs_human_reason: str | None = None
    token_usage: "TokenUsage"
    steps: list["Step"]
```

Knowledge provenance for the PR body / approval UI is derived from the persisted `agent_steps` `read_knowledge` tool results (F06) and the objective's `initial_context` (`RetrievedChunk{chunk_id, source, source_type, text, score}`), read via F06's `GET /api/v1/agent-runs/{id}`. `test_paths` are derived from `AgentRunResult.changed_files` ∩ test globs and `AcceptanceCriterionResult.test_refs`.

**Worktree sandbox (F06):**

```python
class WorktreeHandle(BaseModel):
    repo: str; worktree_path: str; branch_name: str; base_commit_sha: str

class WorktreeSandbox:                                  # F06
    async def create(self, repo: str, base_branch: str, branch_name: str) -> WorktreeHandle: ...
    async def cleanup(self, handle: WorktreeHandle, *, keep_branch: bool = True) -> None: ...
```

**GitHub integration (F03, frozen `GitHubIntegration` in `forge_contracts.integration`):**

```python
class PullRequestSpec(BaseModel):        # F03
    repo_full_name: str; head: str; base: str; title: str; body: str
    draft: bool = False
    labels: list[str] = []; reviewers: list[str] = []
    traceability: "SpecTraceability | None" = None

class ACResult(BaseModel):               # F03
    id: str; text: str; satisfied: bool; evidence: str | None = None

class SpecTraceability(BaseModel):       # F03
    spec_id: str; task_id: str; acceptance_criteria: list[ACResult]

class PullRequest(BaseModel):            # F03
    number: int; url: str; head_sha: str
    state: Literal["open", "closed", "merged"]; draft: bool

class CIStatus(BaseModel):               # F03
    sha: str; state: Literal["pending", "success", "failure", "error"]
    checks: list["CheckRunResult"] = []

class GitHubIntegration(Protocol):       # F03 — methods used by F08
    async def open_pr(self, spec: PullRequestSpec, *, installation_id: int) -> PullRequest: ...
    async def update_pr(self, repo_full_name: str, number: int, *, installation_id: int,
                        title: str | None = None, body: str | None = None,
                        state: str | None = None) -> PullRequest: ...
    async def merge_pr(self, repo_full_name: str, number: int, *, installation_id: int,
                       sha: str, method: Literal["merge","squash","rebase"] = "squash") -> PullRequest: ...
    async def get_ci_status(self, repo_full_name: str, sha: str, *, installation_id: int) -> CIStatus: ...

def render_pr_body(spec: PullRequestSpec) -> str: ...    # F03 — renders body + traceability section
```

**Spec engine (F02):**

```python
class AcceptanceCriterion(BaseModel):     # F02 (manifest)
    id: str; req_refs: list[str]; text: str

class SpecManifest(BaseModel):            # F02
    id: str; requirements: list["Requirement"]
    acceptance_criteria: list[AcceptanceCriterion]; ...

class AgentClaim(BaseModel):              # F02
    criterion_id: str; rationale: str; diff_refs: list[str] = []

class TestResult(BaseModel):             # F02
    node_id: str; criterion_id: str | None
    outcome: Literal["passed", "failed", "skipped"]

class ValidationReport(BaseModel):       # F02
    spec_id: str; task_id: str
    status: Literal["pass", "fail"]
    verdicts: list["CriterionVerdict"]
    coverage: float | None = None; created_at: datetime

def build_validation_report(manifest: SpecManifest, claims: list[AgentClaim],
                            test_results: list[TestResult], *, spec_id: str,
                            task_id: str, coverage: float | None) -> ValidationReport: ...
def build_traceability_matrix(manifest: SpecManifest,
                              report: ValidationReport | None) -> list["TraceRow"]: ...
```

`ValidationReport` carries no commit sha; F08 records `pull_request.validated_head_sha = head_sha` at the moment it submits the validation, and treats the report as pinned to that sha (see merge gate below).

**Repo policy (F04):** `Policy` with `commands: PolicyCommands` (`.all_commands()` → set of allowed command keys), `review_rules: ReviewRules{approval_required_for_merge: bool, min_approvals: int}`, `write_rules`; `PolicyEvaluator.evaluate(ToolCall) -> Decision` used by `WorktreeCommandRunner` to gate each command (`run_command`).

**Skill directives (F11):** `SkillProfile.to_directives() -> SkillDirectives` exposing `verification_steps: list[str]`, `min_test_coverage: int | None`, `review_required: bool`.

**FSM (F07):** F08 emits `WorkflowEvent{type, payload, actor, confidence, idempotency_key}` via `POST /workflow/runs/{id}/events`. Relevant guards are **built into F07** and read the event payload — F08 supplies the payload, never the guards:
- `all_checks_passed` / `any_check_failed` ← `event.payload["checks"]` (a `{check_name: bool}` map).
- `merge_ready` ← `event.payload` booleans `review_approved_by_human`, `ci_status_green`, `spec_validated`.
F08 registers effect bodies into F07's `EffectRegistry` (`register(name, fn)`); it does not subclass or modify the engine.

### 4.2 Owned contracts (defined by F08)

**Verification models** (`packages/contracts/forge_contracts/verification.py`):

```python
from enum import Enum
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel

class CheckName(str, Enum):
    LINT = "lint"; TYPE_CHECK = "type_check"; TEST = "test"; COVERAGE = "coverage"

class CheckStatus(str, Enum):
    PASSED = "passed"; FAILED = "failed"; SKIPPED = "skipped"
    ERROR = "error"          # tool crashed / command missing / timeout

class CheckResult(BaseModel):
    name: CheckName
    status: CheckStatus
    command: str
    exit_code: int | None
    duration_ms: int
    summary: str
    metrics: dict[str, float] = {}      # e.g. {"passed":142,"failed":0,"coverage_pct":87.4}
    output_ref: str | None = None       # MinIO key for full log

class VerificationReport(BaseModel):    # F08 gate report (distinct from F06 VerificationResult)
    workflow_run_id: UUID
    agent_run_id: UUID
    attempt: int
    status: CheckStatus                 # PASSED iff all results PASSED
    results: list[CheckResult]
    all_passed: bool
    coverage_pct: float | None
    coverage_threshold: int | None
    head_sha: str
    created_at: datetime

    def checks_payload(self) -> dict[str, bool]:
        """The {name: bool} map emitted to F07's all_checks_passed / any_check_failed guards."""
        return {r.name.value: r.status is CheckStatus.PASSED for r in self.results}
```

**Sandbox command runner (F08-owned; subprocess in the verify worktree, policy-gated):**

```python
from typing import Protocol, Mapping

class CommandOutput(BaseModel):
    exit_code: int; stdout: str; stderr: str; duration_ms: int; timed_out: bool

class SandboxCommandRunner(Protocol):
    async def run(self, command: str, *, cwd: str, timeout_s: int,
                  env: Mapping[str, str] | None = None) -> CommandOutput: ...

class WorktreeCommandRunner(SandboxCommandRunner):
    def __init__(self, sandbox: "WorktreeSandbox", policy: "Policy",
                 evaluator: "PolicyEvaluator") -> None: ...
    async def materialize(self, *, repo: str, branch_name: str, run_id: UUID,
                          attempt: int) -> "WorktreeHandle": ...     # create verify worktree off branch tip
    async def teardown(self, handle: "WorktreeHandle") -> None: ...  # cleanup(keep_branch=False)
    # run() denies any command whose key is not in policy.commands.all_commands()
```

**VerificationService:**

```python
class VerificationService:
    def __init__(self, runner: WorktreeCommandRunner, artifacts: ArtifactStore,
                 repo: VerificationRepository, specs: "SpecValidationClient"): ...

    async def run_checks(
        self, *,
        workflow_run_id: UUID,
        agent: AgentRunResult,           # F06 result (branch, sha, claims, changed files)
        spec_id: str, task_id: str,
        attempt: int,
        repo_id: str,
        policy: Policy,                  # F04
        directives: SkillDirectives,     # F11
        per_check_timeout_s: int = 600,
    ) -> VerificationReport: ...
```

Check-resolution rules (deterministic, table-driven):

| skill `verification_steps` entry | policy command used | failure condition |
|---|---|---|
| `lint` | `commands.lint` | `exit_code != 0` |
| `type_check` | `commands.type_check` | `exit_code != 0` |
| `unit_tests` / `integration_tests` / `tests` | `commands.test` (or `commands.test_coverage` when coverage required) | `exit_code != 0` or `failed > 0` |
| coverage (`min_test_coverage` set) | `commands.test_coverage` | `coverage_pct < min_test_coverage` |

If a required command is missing from `policy.commands` → that check is `ERROR` (treated as failed). `install` runs once before checks; its failure makes the whole report `ERROR`. After a passing run, the service calls `build_validation_report(manifest, claims=<from agent.acceptance_criteria>, test_results=<from the test run>, spec_id, task_id, coverage)` and submits it to F02 (`POST /specs/{spec_id}/validation`).

**PR / traceability:**

```python
class TraceabilityRow(BaseModel):
    criterion_id: str
    criterion_text: str
    satisfied: bool
    evidence_files: list[str]
    evidence_tests: list[str]
    agent_rationale: str

class SpecTraceabilityComposer:
    def compose(self, *, manifest: SpecManifest,
                agent: AgentRunResult) -> list[TraceabilityRow]: ...
    def to_github(self, rows: list[TraceabilityRow], *, spec_id: str,
                  task_id: str) -> SpecTraceability: ...   # adapt to F03 type

class PRBuilderService:
    def __init__(self, github: GitHubIntegration, specs: SpecReadClient,
                 repo: PullRequestRepository, artifacts: ArtifactStore): ...
    async def build_and_open(self, *, workflow_run_id: UUID, agent: AgentRunResult,
                             verification: VerificationReport,
                             repo_id: str, repo_full_name: str, installation_id: int,
                             base_branch: str) -> "PullRequest": ...
```

`build_and_open` composes `TraceabilityRow`s for **every** manifest AC (an AC the agent did not claim → `satisfied=false`), renders the narrative `body` (verification table + confidence + knowledge provenance), builds a `PullRequestSpec` with `traceability=SpecTraceability(...)`, calls `github.open_pr(spec, installation_id=...)` once, and persists the `pull_request` row with `provider_pr_number`/`url`/`head_sha`.

**Merge gate:**

```python
class MergeGateResult(BaseModel):
    can_merge: bool
    review_approved: bool
    ci_green: bool
    spec_validated: bool
    blocking_reasons: list[str]

class MergeGateEvaluator:
    def __init__(self, prs: PullRequestRepository, approvals: ApprovalRepository,
                 github: GitHubIntegration): ...
    async def evaluate(self, workflow_run_id: UUID) -> MergeGateResult: ...
```

- `review_approved` iff the `ApprovalRequest(kind=pr)` for the run is `approved`.
- `ci_green` iff a fresh `github.get_ci_status(repo_full_name, head_sha, installation_id=...)` returns `state == "success"` (the `pull_request.ci_status` cache is refreshed from this call).
- `spec_validated` iff `pull_request.spec_validated` is true **and** `pull_request.validated_head_sha == pull_request.head_sha` (stale validation after a new push does not satisfy the gate).

**REST API (all under `/api/v1`, all authenticated):**

```
GET    /workflow-runs/{run_id}/verification           -> VerificationReport (latest attempt)
GET    /workflow-runs/{run_id}/verification/{attempt} -> VerificationReport
GET    /workflow-runs/{run_id}/pull-request           -> PullRequestResponse (incl. traceability)
GET    /check-results/{check_id}/log                  -> 302 redirect to signed MinIO URL
GET    /approvals?status=pending&kind=pr              -> list[ApprovalSummary]
GET    /approvals/{approval_id}                       -> ApprovalDetail (the 9 UI items)
POST   /approvals/{approval_id}/decision             -> ApprovalResolution
```

```python
class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject", "request_changes"]
    note: str | None = None

class ApprovalResolution(BaseModel):
    approval_id: UUID
    status: str                      # approved|rejected|changes_requested
    merge: MergeGateResult | None    # populated when decision == approve
    merged: bool
    workflow_state: str              # resulting FSM state after the emitted event
```

`ApprovalService.resolve` maps decisions to F07 events: `approve` → (gate passes) `review_approved`; `request_changes` → `review_changes_requested`; `reject` → `cancel` (+ `update_pr state=closed`). On a confirmed F03 `pull_request_merged` event it emits `close` (`merged → closed`).

**Workflow DSL keys F08 relies on (defined in F07's `default_feature.yaml`):**

```yaml
retry_policy:        { max_retries: 3, backoff: exponential, initial_delay_seconds: 30 }   # owned by F07
escalation_policy:   { confidence_threshold: 0.72, on_low_confidence: pause_and_notify }   # owned by F07
```

F08 owns no retry/backoff/escalation logic; it only emits `checks_passed`/`checks_failed`/`pr_ready`/`review_*`/`cancel`/`close` events.

---

## 5. Dependencies — features/slices that must exist first

- `v1/F07-feature-workflow-fsm` — **hard.** Owns the Postgres FSM, `workflow_run`/`workflow_transition`, the `EffectRegistry`/`GuardRegistry`, built-in guards (`all_checks_passed`, `any_check_failed`, `merge_ready`, `retry_budget_*`, `confidence_below_threshold`, `approval_granted:pr`), retry/backoff, escalation routing, and the `POST /workflow/runs/{id}/events` ingress. F08 registers effect bodies and emits events into it. The bundled `default_feature.yaml` must contain the transitions in F07 §4.
- `v1/F06-single-execution-agent` — **hard.** Owns `start_agent_run`, the agent loop, the frozen `AgentRunResult`, the `agent_runs` table, and `WorktreeSandbox`. **Requires** that the task branch + commits remain resolvable in the repo cache after the agent run and are pushed to the mirror (F06 AC#12) so F08 can materialise a verify worktree off the branch tip and F03 can open the PR.
- `v1/F03-github-app` — **hard.** Provides the frozen `GitHubIntegration` (`open_pr`, `merge_pr`, `get_ci_status`, `update_pr`), `render_pr_body`, the `RepositoryConnection` (→ `installation_id`), and the `ci_status_changed`/`review_submitted`/`pull_request_merged`/`pull_request_opened`/`push_received` domain events F08 consumes.
- `v1/F04-repo-policy` — **hard.** Provides `Policy` (`commands.all_commands()`, `review_rules`, `write_rules`) and `PolicyEvaluator` used to resolve, gate, and authorise checks + merge.
- `v1/F11-skill-profiles` — **hard.** Provides `SkillProfile.to_directives()` (`verification_steps`, `min_test_coverage`, `review_required`) that selects checks and the coverage threshold.
- `v1/F02-spec-engine` — **hard.** Provides `SpecManifest` (acceptance criteria for traceability), `build_validation_report`/`build_traceability_matrix`, the `POST /specs/{id}/validation` endpoint, and the `ValidationReport` consumed by the `spec_validated` signal.
- `v1/F00-foundation-substrate` — **hard.** Baseline `workflow_run`, `agent_runs`, `approval_request`, `task`, `user` tables; `packages/contracts`; RBAC (admin/member/viewer/agent-runner); encrypted secrets vault; MinIO `ArtifactStore`; the immutable audit log (`v1/F14-observability-audit`).
- `v1/F10-run-trace-viewer` — **soft.** The Review page embeds `RunTraceTimeline`; F08 ships with a stub if F10 lags.
- `v1/F16-slack-notifications` — **soft.** Approval-requested / escalation notifications; F08 emits the events regardless and degrades to email/in-app if F16 lags.

---

## 6. Acceptance criteria (numbered, testable)

1. The `run_checks` effect materialises a fresh verify worktree off the agent's `branch_name` (HEAD == `head_commit_sha`) via `WorktreeSandbox.create`, runs the policy `install` command first, then runs **exactly** the checks resolved from `directives.verification_steps` mapped to `policy.commands` — verified by asserting the executed command set + order via the runner.
2. Each executed check persists a `check_result` with correct `status`, `exit_code`, parsed `metrics`, and an `output_ref` pointing to an uploaded MinIO log; a `verification_report` persists with correct `all_passed`, `head_sha`, and `attempt == workflow_run.retry_count + 1`.
3. A coverage check fails when parsed coverage `< directives.min_test_coverage`, passes when `≥`, and the threshold is recorded on the report.
4. A required command missing from `policy.commands` → that check is `ERROR` and the report is not `passed`; an `install` failure makes the whole report `ERROR`; a runner `timed_out` → `ERROR`.
5. `run_checks` emits `checks_passed` with `payload.checks` all true when every check passes, otherwise `checks_failed` with the per-check `payload.checks` map — and never opens a PR itself. (F07's guards/transitions then drive `pr_opened` vs retry/exhaustion; asserted at integration level.)
6. The `open_pr_with_spec_traceability` effect composes a `SpecTraceability`/`traceability` covering **every** acceptance criterion in the task's `SpecManifest` (an AC the agent did not claim → `satisfied=false`), calls `GitHubIntegration.open_pr` **exactly once**, persists a `pull_request` row with `provider_pr_number`/`url`/`head_sha`, then emits `pr_ready`.
7. The rendered PR body (via F03 `render_pr_body` over the composed `PullRequestSpec`) contains the traceability table; every `satisfied=true` row carries ≥1 evidence file or test sourced from `AgentRunResult.acceptance_criteria` + `changed_files`; the stored `traceability` jsonb matches the rendered rows.
8. The `create_pr_approval` effect (dispatched by F07 on `pr_opened → awaiting_review`) creates **exactly one** pending `approval_request(kind=pr)` (the partial unique index rejects a duplicate), and emits an `approval_requested` notification event (F16).
9. After a passing run, `run_checks` builds a `ValidationReport` via F02 using `AgentRunResult.acceptance_criteria` as `agent_claims` and the test run's `TestResult`s, and records `pull_request.spec_validated` + `validated_head_sha == head_sha`.
10. `POST /approvals/{id}/decision {approve}` returns `403` when the actor has role `viewer` or is the `agent-runner` identity that produced the run; it is accepted for `member`/`admin` satisfying `policy.review_rules` (V1 `min_approvals: 1`).
11. On `approve`, `MergeGateEvaluator.evaluate` computes `review_approved AND ci_green (fresh get_ci_status) AND spec_validated`; merge proceeds **only if** all true. If any is false, the API returns `merged=false` with the matching `blocking_reasons`, **no** `review_approved` event is emitted, and the run stays `awaiting_review`.
12. When all three conditions hold, `GitHubIntegration.merge_pr` is called once (sha-pinned, `method="squash"`), `pull_request.merged_sha`/`merged_at` are set, and a `review_approved` event with the payload booleans is emitted → F07's `merge_ready` guard passes → `merged`; a second `approve` no-ops on `merged_sha` (idempotent).
13. `request_changes` resolves the approval `changes_requested` and emits `review_changes_requested` → F07 routes `awaiting_review → executing` (re-run agent); `reject` resolves `rejected`, calls `update_pr(state="closed")`, and emits `cancel` → `cancelled`.
14. The `spec_validated` signal is true only when `pull_request.spec_validated` and `validated_head_sha == pull_request.head_sha`; after a `push_received`/`ci_status_changed` event advances `pull_request.head_sha`, the signal is false until a fresh verification re-validates the new sha.
15. `ci_status` changes from F03 domain events update `pull_request.ci_status`; after CI turns green, re-evaluating the merge gate (via a re-`approve` or webhook-triggered re-eval) returns `ci_green=true` and allows a previously blocked merge to proceed.
16. On the F03 `pull_request_merged` domain event for the run's PR, F08 emits `close` → F07 reaches `closed`.
17. Every check completion, PR open, approval decision, and merge writes an immutable audit entry with actor (`user:<id>` or `agent:<id>`) + payload hash; FSM transitions are recorded by F07's `workflow_transition`.
18. Check stdout/stderr uploaded to MinIO and the rendered PR body pass the secret-redaction filter before persistence/transmission; `GET /check-results/{id}/log` returns a short-TTL signed URL and authorises the requester against the run's workspace (cross-workspace → 404).
19. `GET /approvals/{id}` returns an `ApprovalDetail` containing all 9 spec-mandated UI items (goal+requirements, diff ref, verification, traceability, knowledge provenance, confidence+rationale, risk flags, run-trace ref, actions).

### 6.x Definition of Done

All ACs covered by passing tests; an integration test drives a seeded `task_ready` run end-to-end (mocked `GitHubIntegration`, real Postgres testcontainer, `NullEffectDispatcher` capturing events, F07 engine for state) through `executing → verifying → pr_opened → awaiting_review → merged → closed`; coverage of new F08 code ≥ 80% (the `backend-tdd` bar Forge applies to itself).

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first. Layout: `packages/workflow-engine/tests/f08/` (unit), `apps/api/tests/` (API), `apps/worker/tests/` (effect/integration), `apps/web/__tests__/` (component).

**Key fixtures:**
- `fake_runner` — `SandboxCommandRunner` returning scripted `CommandOutput` per command substring (`"ruff"`→exit 0, `"pytest"`→exit 1 with failing-tests stdout). Records call order.
- `policy_backend` — `Policy` with the spec's example `commands` (install/lint/type_check/test/test_coverage) + `review_rules{approval_required_for_merge:true, min_approvals:1}`.
- `directives_backend_tdd` — `SkillDirectives{verification_steps:[lint,type_check,unit_tests,integration_tests], min_test_coverage:80, review_required:true}`.
- `manifest` — `SpecManifest` with criteria A1, A2.
- `agent_result_pass` / `agent_result_lowconf` — `AgentRunResult` variants (F06 shape).
- `fake_github` — in-memory `GitHubIntegration` recording `open_pr`/`merge_pr`/`update_pr`, with settable `get_ci_status`.
- `event_sink` — `NullEffectDispatcher` + a fake events endpoint recording emitted `WorkflowEvent`s.
- `pg` — Postgres testcontainer + migrations upgraded to head.
- `make_workflow_run` — factory seeding a `workflow_run` (F07) at a given state with task/spec/policy/skill wired and a linked `agent_runs` row.

**Unit — parsers (`tests/f08/test_parsers.py`):**
- `test_parse_pytest_counts` — "142 passed, 0 failed" → metrics `{passed:142, failed:0}`, PASSED.
- `test_parse_pytest_failures` — "3 failed, 100 passed" → FAILED, `failed:3`.
- `test_parse_coverage_total` — "TOTAL ... 87%" → `87.0`.
- `test_parse_ruff_clean_vs_violations` — exit 0 → PASSED; exit 1 + N findings → FAILED `violations:N`.
- `test_parse_mypy_errors` — "Found 2 errors" → FAILED `errors:2`.

**Unit — VerificationService (`tests/f08/test_verification_service.py`):**
- `test_runs_only_resolved_checks` — exact command set + order for `directives_backend_tdd` (install, lint, type_check, test_coverage); AC#1.
- `test_install_runs_first`; AC#1.
- `test_coverage_below_threshold_fails` (70 vs 80 → COVERAGE FAILED, `all_passed=false`) / `test_coverage_at_threshold_passes` (80 vs 80); AC#3.
- `test_missing_command_is_error` (policy lacks `type_check`) / `test_install_failure_is_error` / `test_timeout_is_error`; AC#4.
- `test_persists_report_and_logs` — report + 3 `check_result`s persisted, `output_ref` set, `attempt = retry_count+1`, `head_sha` set; AC#2.
- `test_emits_checks_passed_payload` / `test_emits_checks_failed_payload` — emitted event type + `payload.checks` map; AC#5.
- `test_submits_validation_on_pass` — F02 `build_validation_report` called with agent claims + test results; `validated_head_sha` recorded; AC#9.

**Unit — traceability + PR builder (`tests/f08/test_pr.py`):**
- `test_every_criterion_present` — rows cover all manifest AC even if the agent omitted one (omitted → `satisfied=false`); AC#6.
- `test_satisfied_row_has_evidence` — satisfied rows include ≥1 evidence file/test; AC#7.
- `test_open_calls_github_once_and_persists` — `fake_github.open_pr` called once; `pull_request` row created with number/url/head_sha; emits `pr_ready`; AC#6.
- `test_body_contains_traceability_and_verification` — rendered body (via `render_pr_body`) has traceability + verification + confidence + knowledge sections; AC#7.

**Unit — merge gate (`tests/f08/test_merge_gate.py`):**
- `test_blocks_when_ci_red` / `test_blocks_when_not_validated` / `test_blocks_when_not_approved` → `can_merge=false` with the right `blocking_reasons`; AC#11.
- `test_stale_validation_blocks` — `validated_head_sha != head_sha` → `spec_validated=false`; AC#14.
- `test_allows_when_all_green` → `can_merge=true`; AC#11/#12.

**API tests (`apps/api/tests/test_approvals.py`):**
- `test_viewer_cannot_approve` (403), `test_agent_runner_cannot_self_approve` (403), `test_member_can_approve`; AC#10.
- `test_approve_blocked_returns_reasons` — approve with red CI → 200, `merged=false`, reasons, no `review_approved` emitted; AC#11.
- `test_approve_merges_when_green` — `fake_github.merge_pr` called once (sha-pinned, squash), `merged_sha` set, `review_approved` emitted; re-approve no-ops; AC#12.
- `test_request_changes_emits_review_changes_requested`, `test_reject_closes_pr_and_emits_cancel`; AC#13.
- `test_get_approval_detail_has_nine_items`; AC#19.
- `test_check_log_signed_url_authz` — cross-workspace → 404; AC#18.

**Integration — full flow (`apps/worker/tests/test_flow.py`, Postgres + fakes + F07 engine):**
- `test_happy_path_to_merged` — seed `task_ready`; emit `execute`, then simulate F06 `agent_completed`; F07 dispatches `run_checks` (F08) → `checks_passed` → `pr_opened` → `open_pr_with_spec_traceability` → `pr_ready` → `awaiting_review` → `create_pr_approval`; approve via service → `merged`; emit F03 `pull_request_merged` → `close` → `closed`; assert one PR, one report, audit events; AC#1,5,6,8,12,16,17.
- `test_retry_then_succeed` — first `checks_failed` → F07 returns to `executing` (retry_count=1); second attempt passes → `pr_opened`; AC#5 (+F07 retry).
- `test_retry_exhausted_to_needs_human_input` — four `checks_failed` → `needs_human_input`, no PR.
- `test_low_confidence_escalates_before_pr` — `agent_low_confidence` → `needs_human_input`; `run_checks` and `open_pr` never invoked; AC (Journey D).
- `test_ci_turns_green_unblocks_merge` — approve while CI pending (blocked), set `fake_github` status success, re-approve → merge; AC#15.

**Frontend (`apps/web/__tests__/review.test.tsx`, React Testing Library):**
- `test_renders_nine_panels` — all required sections present; AC#19.
- `test_approve_optimistic_then_rollback_on_block` — Approve shows optimistic state, rolls back + shows blocking banner when API returns `merged=false`; AC#11.
- `test_keyboard_shortcuts` — `a/r/x` trigger the correct actions.

---

## 8. Security & policy considerations

- **No self-approval / least privilege:** the `agent-runner` identity that produced the run cannot resolve its own `pr` approval; `viewer` cannot approve. Enforced server-side in `ApprovalService.resolve` (AC#10) and again by F07's send-event RBAC (human-gate events require member/admin). Approve requires `policy.review_rules.approval_required_for_merge` and `min_approvals` (V1: 1).
- **Merge is gated, always:** merge cannot occur via any code path without `review_approved AND ci_green AND spec_validated`; no "force merge" endpoint in V1. The merge uses F03's installation-token-scoped, sha-pinned `merge_pr`, and is idempotent on `pull_request.merged_sha`.
- **Policy-checked command execution:** `WorktreeCommandRunner` runs **only** commands whose key is in `policy.commands.all_commands()`, evaluated through F04's `PolicyEvaluator` (`run_command`); arbitrary command execution is denied. The human-gated merge is allowed even though `push_to_main` is a restricted **agent** action (the restriction binds the agent, not the human-approved merge).
- **Sandbox isolation:** checks run only inside a throwaway verify worktree (F06 sandbox), torn down after the run; no cross-task filesystem access; the worker runs as a non-root user.
- **Secret redaction:** check stdout/stderr (to MinIO) and the rendered PR body pass the secret-redaction filter before persistence/transmission. `GET /check-results/{id}/log` returns a short-TTL signed URL authorised against the run's workspace.
- **Audit:** every check, PR open, approval decision, and merge writes an immutable audit entry with actor + payload hash (AC#17).
- **Tenant isolation:** all queries filter by workspace; approval/PR/verification reads for another workspace return 404 (no existence leak).
- **Rate limiting:** approval-decision and merge endpoints are per-user rate limited to prevent accidental double-merge; merge is idempotent on `merged_sha`.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — the largest single integration slice in V1; it spans engine effect bodies, worker, API, web, and four sibling contracts. Rough split: verification service + parsers + runner (M), PR builder + traceability (M), merge gate + approval service + effect wiring (M), API endpoints (S/M), web review page (M).

**Key risks:**
1. **Contract coordination across F06/F03/F02/F07.** F08 is glue and consumes four sibling contracts (`AgentRunResult`, `GitHubIntegration`, `ValidationReport`, F07 events/guards/registry). *Mitigation:* consume the frozen `forge_contracts` shapes verbatim (§4.1); build against the fakes in §7; sequence F08 after F06/F03/F02/F07 reach contract-stable.
2. **Worktree availability for the gate.** The gate re-checkout depends on F06/F03 having pushed the branch and kept commits resolvable. *Mitigation:* the §5 hard dependency on F06 AC#12; verify worktree is created off the pushed branch tip and torn down (`keep_branch=False`).
3. **Validation sha-pinning gap.** F02's `ValidationReport` carries no commit sha. *Mitigation:* F08 records `pull_request.validated_head_sha` at submission and gates on `validated_head_sha == head_sha` (AC#14); if F02 later adds a sha, the association moves there.
4. **Output parsing brittleness.** *Mitigation:* parsers are pure + fixture-tested; prefer machine-readable flags (`pytest --json-report`, `coverage json`, `ruff --output-format=json`, `mypy --output json`); unparseable-but-exit-0 → PASSED with a `summary` fallback.
5. **CI timing races.** *Mitigation:* the merge gate re-reads `get_ci_status` at evaluation time and supports webhook-driven re-eval (AC#15); never trust the cached `ci_status`.
6. **FSM event idempotency.** Two `run_effect` deliveries for one effect could double-open a PR. *Mitigation:* event `idempotency_key`s (F07), the `pull_request` per-run uniqueness, and the partial unique pending-approval index; merge idempotent on `merged_sha`.

---

## 10. Key files / paths (exact)

**packages/workflow-engine/forge_workflow/** (new subpackages only; F07's files untouched)
- `verification/parsers.py`, `verification/runner.py`, `verification/service.py`, `verification/repository.py`
- `pr/builder.py`, `pr/traceability.py`, `pr/repository.py`
- `gates/merge.py`
- `effects/handlers.py` (`run_checks`, `open_pr_with_spec_traceability`, `create_pr_approval`, `register_f08_effects`)
- `tests/f08/`

**packages/contracts/forge_contracts/**
- `verification.py` (`CheckName`, `CheckStatus`, `CheckResult`, `VerificationReport`, `TraceabilityRow`, `MergeGateResult`)

**packages/db/forge_db/models/**
- `verification.py` (`verification_report`, `check_result`), `pull_request.py` (`pull_request`), `approval.py` (extend baseline `approval_request`)

**packages/db/forge_db/migrations/versions/**
- `xxxx_f08_verification_pr.py` (verification_report, check_result, pull_request)
- `xxxx_f08_approval_pr_columns.py` (approval_request additive columns + partial unique index)

**apps/api/forge_api/**
- `routers/approvals.py`, `routers/runs_verification.py`
- `services/approval_service.py`
- `schemas/{approval,verification,pull_request}.py`
- `tests/test_approvals.py`, `tests/test_runs_verification.py`

**apps/worker/forge_worker/**
- `bootstrap/effects.py` (calls `register_f08_effects` at worker boot; routes the `verification` queue)
- `tests/test_flow.py`

**apps/web/**
- `app/(dashboard)/projects/[projectId]/tasks/[taskId]/review/page.tsx`
- `components/review/{DiffViewer,VerificationPanel,TraceabilityMatrix,KnowledgeProvenance,ConfidenceBadge,RiskFlags,ApprovalActions}.tsx`
- `components/board/{WorkflowStatusBadge,VerificationChecklist}.tsx`
- `lib/api/{approvals,verification,pullRequests}.ts`
- `__tests__/review.test.tsx`

**deploy/**
- `docker-compose.yml` (worker `verification` queue + Python check toolchain), `scripts/install.sh` (`forge-checks` MinIO bucket)

---

## 11. Research references (relevant links from the spec/research report)

- Workflow Engine — Default Feature Workflow States + DSL (`run_checks`, `open_pr_with_spec_traceability`, retry/escalation): `docs/FORGE_SPEC.md` §Workflow Engine; transition table mirrored in `docs/implementation-slices/v1/F07-feature-workflow-fsm.md` §4.
- Spec Gating Rules + Manifest (acceptance criteria, traceability, "no merge without validation"): `docs/FORGE_SPEC.md` §Spec-Driven Development Engine; consumed via `v1/F02-spec-engine`.
- Repo Policy `policy.yaml` (commands, write_rules, review_rules): `docs/FORGE_SPEC.md` §Repo Policy System; `v1/F04-repo-policy`.
- Skill Profiles (`verification_steps`, `min_test_coverage`, `review_required`): `docs/FORGE_SPEC.md` §Skill Profiles; `v1/F11-skill-profiles`.
- Human Approval System — Approval Gate Types + "Approval UI Must Show" 9 items: `docs/FORGE_SPEC.md` §Human Approval System.
- Open SWE — repo-aware context, curated tools, PR-opening workflow, isolated worktree sandbox: https://github.com/langchain-ai/open-swe ; deep dive https://byteiota.com/open-swe-langchain-autonomous-coding-agent/ (research report §"Symphony and Open SWE: What to Borrow").
- LangGraph human-in-the-loop interrupts (pause/resume at gates): https://langchain-ai.github.io/langgraph/ ; https://www.youtube.com/watch?v=4F8wvpb8JkI
- GitHub Spec Kit — phase quality gates / Implement-against-spec: https://github.com/github/spec-kit
- GitHub App as PR/CI/review event source: `docs/FORGE_SPEC.md` §Integrations; `v1/F03-github-app`.
- Celery retries + backoff (owned by F07): https://docs.celeryq.dev/
- MinIO artifact storage for logs: https://min.io/

---

## 12. Out of scope / future

- **Plan drafting / plan review / task generation** (`plan_drafting → … → task_ready`) — owned by F02/F07. F08 begins at `task_ready`.
- **The FSM engine, transition table, guards, preconditions, retry budget, backoff, and escalation routing** — owned by F07. F08 emits events and registers effect bodies only; it adds no `workflow_run` columns.
- **The agent loop and `start_agent_run`** — owned by F06.
- **GitHub auth, webhook ingestion/signature verification, installation-token mgmt, `render_pr_body`** — owned by F03; F08 consumes the domain events and the `GitHubIntegration` Protocol.
- **The `SpecManifest`, validation builder, and traceability matrix** — owned by F02; F08 submits claims/results and reads the report.
- **Multi-repo / multi-PR tasks** — V2 (`repo_targets[]` with >1 repo). V1 = single repo → single PR per run.
- **Multi-reviewer / `min_approvals > 1` / CODEOWNERS routing** — V1 supports `min_approvals: 1`.
- **Deploy approval gate** (`kind=deploy`) — separate slice; F08 implements only `kind=pr` on the shared `approval_request` primitive.
- **Container/Firecracker sandbox for check execution** — V1 uses git worktrees (V2/V3 for stronger isolation).
- **Non-Python toolchains in the verification image** (TypeScript/Go/etc.) — V1 ships the Python golden-set toolchain; pluggable per-language runners are a fast-follow.
- **Note on `request_changes`:** per F07's DSL, `review_changes_requested` routes `awaiting_review → executing` (automatic agent re-run with the reviewer note); F08 emits the event but does not implement the re-run loop (that is F06/F07).
