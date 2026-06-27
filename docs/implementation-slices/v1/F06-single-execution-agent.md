# F06 — Single Execution Agent (LangGraph routing)

> Phase: v1 · Spec module(s): Execution Agent Runtime (`packages/agent-runtime/forge_agent`) + Workflow Engine *agent-level routing* (LangGraph `StateGraph`) · Status target: a Celery-driven single execution agent that, given a fully prepared `AgentObjective`, executes a LangGraph `StateGraph` loop (load_context → plan → act → observe → verify → finalize) inside a git-worktree sandbox with policy-scoped tools, runs the skill profile's verification steps, persists every step + a structured `AgentRunResult`, and supports human-in-the-loop interrupt/resume via a Postgres checkpointer. "Done" = acceptance criteria 1–19 pass and the unit + integration suite for `packages/agent-runtime` is green (ruff + types + pytest).

---

## 1. Intent — what & why

Forge runs in **single-agent execution mode by default** (Product Vision, Core Design Principle 1). F06 is that default execution agent: the component that actually turns an approved task + spec + retrieved context + repo policy into committed code on a task branch, with verification results and a confidence-scored, acceptance-criteria-mapped result.

Per the Workflow Engine architecture table, top-level task state (created → executing → verifying → … → merged) is a Postgres FSM (**F07**), while **agent-level routing** — how the agent plans, picks tools, decides when to retry, and when to ask a human — is a **LangGraph `StateGraph`** with conditional edges, durable checkpointing, and HITL interrupts. This slice builds exactly that agent-level layer.

The research mandate (LangGraph 2026 production guide, cite:93) is explicit: ship the *smallest useful agent loop first*, use `StateGraph` with checkpointers, and prove it against a golden eval set before adding multi-agent complexity. F06 is that smallest useful loop. It borrows two patterns from Open SWE (cite:35): (a) **load `AGENTS.md` repo context before execution**, and (b) **provide structured task context + a curated, scoped tool set instead of free-form prompting**, all inside an **isolated git-worktree sandbox** (Sandbox V1 = git worktrees).

Non-negotiables this slice enforces (from the Build Prompt constraints):
- The agent **never self-assigns permissions or expands its own scope** — the tool registry is frozen from task policy at run start.
- **Every tool invocation is policy-checked** before execution (Security table: "Policy evaluation — every tool invocation checked against repo policy before execution").
- Every step is an **immutable audit record** in `agent_steps`, **and** every security-relevant step (tool call, MCP call, policy decision, interrupt) is mirrored to the central, tamper-evident `audit_log` via the `AuditSink` (`cross-cutting/F39-audit-log`); secrets are **redacted** from steps, audit metadata, results, and logs.
- **Spec-gated** for feature-class work: F06 runs only after spec approval (FSM) and rejects feature objectives with no acceptance criteria; `finalize` cites which AC are satisfied. F06 never merges — human PR approval is always required (`cross-cutting/F36-human-approval-system`).
- **BYOK**: the model client is built from a provider-agnostic `ModelClient`, keyed from the encrypted vault (`cross-cutting/F37-auth-secrets-byok`) via `api_key_ref` (no vendor lock-in, raw key never persisted).

## 2. User-facing behavior / journeys

F06 has no first-class UI of its own (the trace viewer is **F10**, approvals are **F08**). Its "users" are the workflow engine (machine) and, indirectly, the human reviewer reading the artifacts it produces.

1. **Happy path (machine-initiated).** The workflow FSM reaches `task_ready → executing` and fires `start_agent_run` (preconditions `repo_target_set, policy_loaded, skill_profile_set, knowledge_synced` already satisfied). F06 creates a worktree on `forge/TASK-123` off `main`, loads `AGENTS.md` + policy into context, plans, reads code, writes code + tests, runs the skill profile's verification commands, and returns an `AgentRunResult{status=succeeded, confidence, acceptance_criteria[], verifications[], branch_name, head_commit_sha, changed_files}`. The FSM then advances to `verifying`/`pr_opened`.
2. **Verification-fail retry.** A verification step fails (e.g. coverage 71% < the profile's 80% floor). The agent loops back to `act` to fix, within the retry budget. On budget exhaustion it finalizes `status=awaiting_input` with `needs_human_reason`.
3. **HITL interrupt (low confidence / policy conflict).** At finalize, confidence `< confidence_threshold` (default 0.72), or mid-run the model attempts a `restricted_action` whose policy `Decision.requires_approval` is true (a Policy-override gate). The graph calls `interrupt(...)`, the checkpointer persists the thread, the run status becomes `awaiting_input`, and the FSM moves to `needs_human_input`. A human resolves the gate via the canonical approval system (`cross-cutting/F36-human-approval-system`, surfaced inside the `v1/F08-plan-execute-verify-pr-approval` flow); the decision drives **F06 `resume`**, which continues from the exact checkpoint with no re-execution of prior tool calls.
4. **Denied action (no human needed).** The model requests a tool outside `allowed_actions`, or a write to a `write_deny` path. The PolicyGuard denies it inline, records a `denied` step, and feeds the denial back to the model as an observation so it can choose another action — no human interrupt for ordinary scope violations.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

The base `agent_runs` row and the `AgentRun`/`ApprovalRequest`/`SubAgentRun`/`WorkflowRun` SQLAlchemy models are created by the cross-cutting platform-foundation prerequisite (`cross-cutting/F00-platform-foundation`, Phase-0 Task 0.2 — `packages/db/forge_db/models`; this foundation slug has no dedicated numbered file yet — sibling slices reference it variously and it must be reconciled when the foundation slice lands). This slice extends `packages/db/forge_db/models/agent.py` and owns one Alembic migration (`packages/db/migrations/versions/0006_agent_runtime.py`) that chains on the Phase-0 baseline `0001_*` (which creates `agent_runs`) and shares the single Alembic history with `v1/F07-feature-workflow-fsm` (which extends `workflow_run` in the same history). The migration:

**Extends `agent_runs`** (adds runtime-lifecycle columns; keep `inputs`/`result` jsonb already present from the Phase-0 baseline. `inputs` stores the serialized `AgentObjective` — including `model.api_key_ref`, **never** the raw key — which the worker rehydrates at run time, see §3.3):
| Column | Type | Notes |
|---|---|---|
| `status` | enum `agent_run_status` (`pending, running, verifying, awaiting_input, succeeded, failed, cancelled`) | drives idempotency & resume |
| `confidence` | `float` null | finalize node output |
| `worktree_path` | `text` null | absolute path under `WORKTREE_ROOT` |
| `branch_name` | `text` null | e.g. `forge/TASK-123` |
| `base_commit_sha` | `varchar(40)` null | worktree base |
| `head_commit_sha` | `varchar(40)` null | last commit produced |
| `checkpoint_thread_id` | `text` not null | LangGraph thread id (== `agent_run_id` str) |
| `token_usage` | `jsonb` default `{}` | `{prompt, completion, total, cost_usd}` |
| `error` | `jsonb` null | `{type, message}` (redacted) |
| `started_at` / `finished_at` | `timestamptz` null | |

**Creates `agent_steps`** (ordered, append-only — the high-volume, step-level *trace* source consumed by the run-trace viewer `v1/F10-run-trace-viewer`. This is distinct from the central, low-volume, tamper-evident `audit_log` owned by `cross-cutting/F39-audit-log`: F06 *also* emits a redacted security-event summary to F39's `AuditSink` for every tool call / MCP call / policy decision / interrupt, with `detail_ref` pointing back at the corresponding `agent_steps` row — see §3.3 and §8. F06 does **not** own or create the `audit_log` table.):
| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `agent_run_id` | uuid FK `agent_runs.id` ON DELETE CASCADE | |
| `seq` | `int` not null | monotonic per run; unique `(agent_run_id, seq)` |
| `node` | `text` | graph node: `load_context, plan, act, observe, verify, finalize` |
| `step_type` | enum `agent_step_type` (`decision, tool_call, tool_result, message, verification, interrupt, error`) | |
| `tool_name` | `text` null | for `tool_call`/`tool_result` |
| `action` | `text` null | policy action vocab (`read_repo, write_code, …`) |
| `input` | `jsonb` | args / decision input — **secret-redacted** |
| `output` | `jsonb` | result / decision — **redacted, truncated to 64 KB; overflow → MinIO ref in `artifacts`** |
| `status` | enum `agent_step_status` (`ok, denied, error`) | |
| `policy_decision` | `jsonb` null | `{allowed, reason, requires_approval}` |
| `artifacts` | `jsonb` default `[]` | MinIO object refs (raw logs, diffs) |
| `latency_ms` | `int` null | |
| `token_usage` | `jsonb` null | per-step model usage |
| `created_at` | `timestamptz` default now | |

Indexes: `ix_agent_steps_run_seq (agent_run_id, seq)`, `ix_agent_runs_status (status)`, `ix_agent_runs_workflow_run (workflow_run_id)`.

**LangGraph checkpoint tables** (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`) are created by `langgraph.checkpoint.postgres.PostgresSaver.setup()` into a dedicated `langgraph` Postgres schema — invoked once from `forge_agent.checkpoint.ensure_checkpoint_schema()` and wired into `make migrate`. Not hand-written Alembic.

### 3.2 Backend (FastAPI routes + services/packages)

Router `apps/api/forge_api/routers/agent.py` is pre-stubbed (Phase 0 Task 0.4); this slice fills two handlers. Service logic lives in `apps/api/forge_api/services/agent_runs.py`. All routes require auth (`agent-runner` or `member`+ role) and workspace scoping.

| Method | Path | Body / Query | Returns | Purpose |
|---|---|---|---|---|
| `GET` | `/api/v1/agent-runs/{agent_run_id}` | — | `AgentRunRead` (run + ordered `steps[]`) | read persisted run + trace (feeds F10 viewer; pure read) |
| `POST` | `/api/v1/agent-runs/{agent_run_id}/resume` | `HumanResumeInput` | `202 {accepted: true}` | resume an `awaiting_input` run after human decision (called by F08) |

Internal service functions (consumed by the workflow engine **F07**, *not* HTTP):
- `agent_runs.create_and_enqueue(task_id, workflow_run_id, *, db) -> UUID` — builds the `AgentObjective` via `AgentRunInputBuilder`, inserts the `agent_runs` row (`status=pending`), enqueues the Celery task, returns `agent_run_id`. This is what `start_agent_run` calls.
- `agent_runs.enqueue_resume(agent_run_id, human_input, *, db) -> None` — validates status `== awaiting_input`, persists the human decision as an `interrupt`-resolving step, enqueues the resume Celery task.

`AgentRunInputBuilder` (in `forge_agent.objective`) composes the objective from injected provider callables so F07 only wires dependencies once:
```
build(task_id, workflow_run_id) ->
  task        = board.get_task(task_id)                     # F01
  spec        = spec_engine.get_acceptance_criteria(task.spec_id)  # F02 (optional for chore/bug)
  policy      = policy_evaluator.load(repo_root)            # F04
  agents_md   = policy_loader.load_agents_md(repo_root)     # F04
  skill       = skill_registry.get(task.skill_profile)      # F11
  context     = knowledge.search(objective_query, task.knowledge_scope, k=10)  # F05
  model       = model_resolver.resolve(workspace_id)        # BYOK — cross-cutting/F37-auth-secrets-byok vault (returns ModelConfig w/ api_key_ref, never the raw key)
```

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

Package: **`packages/agent-runtime/forge_agent`** (module `forge_agent`). This is the heart of the slice. It implements the frozen `AgentRuntime` Protocol from `forge_contracts`.

Celery tasks in `apps/worker/forge_worker/tasks/agent_runner.py`:
```python
@celery_app.task(bind=True, name="agent.run", acks_late=True, max_retries=0)
def run_agent_task(self, agent_run_id: str) -> dict: ...

@celery_app.task(bind=True, name="agent.resume", acks_late=True, max_retries=0)
def resume_agent_task(self, agent_run_id: str) -> dict: ...
```
- `max_retries=0` at the Celery layer: retries are governed *inside* the graph by the skill profile's verification budget and the FSM's retry policy. Celery-level retry would double-execute side effects.
- `acks_late=True` + idempotency: the task first loads the run; if `status not in {pending, awaiting_input}` it returns a no-op (handles at-least-once redelivery). Resume always goes through the checkpointer, so a re-delivered message replays from the last checkpoint, not from scratch.
- Tasks run on a dedicated `agent` queue (CPU/IO heavy: git, subprocess, model calls). Both tasks load the `agent_runs` row, **rehydrate the `AgentObjective` from `agent_runs.inputs`** (which carries `model.api_key_ref`, never the raw key), construct `RuntimeDeps` (the `model_factory` resolves the provider key from the `cross-cutting/F37-auth-secrets-byok` vault in-memory at call time), and call `ExecutionAgent.run(objective)` / `.resume(agent_run_id, human_input)` via `asyncio.run`.
- Long-run visibility: set Redis `visibility_timeout` ≥ `max_iterations × per-step ceiling` (default 2h) so the broker doesn't redeliver an in-progress run.

LangGraph graph (`forge_agent/graph.py`): `build_execution_graph(deps) -> CompiledStateGraph`.

```
START → load_context → plan
plan        ─(router_after_plan)→ act | finalize
act         ─(always)→ observe                # act performs the policy-checked tool call
observe     ─(router_after_observe)→ act | plan | verify | finalize
verify      ─(router_after_verify)→ act | finalize
finalize    ─(router_after_finalize)→ END | INTERRUPT(needs_human_input)
```

`INTERRUPT` is LangGraph's `interrupt()` called *from within a node* (it pauses the whole graph and persists the checkpoint regardless of the static edges drawn above). Two nodes can raise it: **`act`** when the model requests a `restricted_action` whose policy `Decision.requires_approval` is true (a Policy-override gate — "Always required" per the spec Approval Gate Types), and **`finalize`** on low confidence or an unsatisfied required acceptance criterion. Both set `agent_runs.status = awaiting_input` and let the FSM (`v1/F07-feature-workflow-fsm`) move the task to `needs_human_input`; the human decision arrives via the approval gate (`cross-cutting/F36-human-approval-system`) and is replayed by `resume`.

Node responsibilities:
- **load_context** — create the worktree (via `WorktreeSandbox`), build the system message embedding `AGENTS.md` + a policy summary + the skill-profile behavior block + the objective + acceptance criteria + the top-k retrieved chunks with source attribution. Emits a `message` step.
- **plan** — single model call producing an ordered plan (`requires_plan` profiles must produce a non-empty plan before any `write_code`). Emits a `decision` step.
- **act** — the model (bound to the *scoped* LangChain tools) emits a tool call; the node runs it through `PolicyGuard` → `ToolRegistry.get(name).run(args, ctx)`; appends a `tool_call` step (+ `policy_decision`). Denied/needs-approval handled here (deny → observation; approval-required restricted action → `interrupt`).
- **observe** — fold `ToolResult` into state as a `tool_result` step; bump iteration; the model's next decision flows through routing.
- **verify** — run the skill profile's `verification_steps` (`lint, type_check, unit_tests, integration_tests, coverage`) by executing the corresponding `policy.commands` via the `run_tests` tool / `SandboxCommandRunner`; produce `VerificationResult[]`; emit `verification` steps. This is the agent's **internal self-correction loop** (it drives the act↔verify retry and the finalize decision). It is *not* the authoritative FSM-level gate: the canonical `run_checks` that feeds the PR approval UI is `VerificationService` in `v1/F08-plan-execute-verify-pr-approval`, which re-runs the same checks **inside this slice's worktree** through the `SandboxCommandRunner` F06 exposes (§4). No contradiction: F06 self-checks to decide when to stop; F08 produces the gate-of-record.
- **finalize** — model maps each acceptance criterion → satisfied + evidence, emits a confidence score with rationale; assembles `AgentRunResult` (including `knowledge_refs` — the retrieved chunks that informed the change, for the spec's Approval-UI "knowledge provenance" must-show item). If `confidence < threshold` or any required AC unsatisfied → `interrupt`.

**Spec-gating (non-negotiable #4).** Before `plan`, `load_context` asserts the objective is spec-gated: when `kind == "feature"` the objective **must** carry a non-empty `acceptance_criteria`; if empty, the run fails fast (`status=failed`, `needs_human_reason="missing_acceptance_criteria"`) rather than implementing un-spec'd feature work. (The FSM only reaches `executing` after `spec_approved`, so this is a defense-in-depth assertion, not the primary gate.) `chore`/`bug`/`spike` kinds may legitimately have empty AC. This complements the spec rule "Agent output must cite which acceptance criteria it believes are satisfied", which `finalize` satisfies via `acceptance_criteria[]`.

State persistence: every node writes its steps to `agent_steps` (through `deps.step_sink`) *and* the LangGraph `PostgresSaver` checkpoints graph state after each super-step, keyed by `thread_id = str(agent_run_id)`. In the same DB transaction as the `agent_steps` write, security-relevant steps (`tool_call`, `query_mcp` results, policy denials, `interrupt`) are also emitted to the central audit log via `deps.audit.emit(session, AuditEvent(...))` (`cross-cutting/F39-audit-log`) — `action ∈ {tool.call, mcp.tool_call, mcp.resource_read, mcp.write_blocked, policy.tool_allowed, policy.tool_denied, policy.override, agent.action}`, `actor_type=agent_runner`, `actor_label=f"agent_run:{agent_run_id}"`, `detail_ref={"table":"agent_steps","id":<step_id>}`. This is the spec's "audit log for every agent action, tool call, and MCP call" non-negotiable.

### 3.4 Frontend / UI (Next.js routes/components)

N/A — F06 ships no UI. The persisted `agent_runs` + `agent_steps` are rendered by the **Run Trace Viewer (F10)** and surfaced in the **Approval UI (F08)**; the `GET /api/v1/agent-runs/{id}` endpoint defined here is the data contract those slices consume.

### 3.5 Infra / deploy (compose, helm, caddy)

Touches `deploy/docker-compose.yml` / `docker-compose.dev.yml` (`worker` service, owned by F00/Task 0.6; this slice adds the agent-specific config):
- **`git` must be installed** in the worker image (Dockerfile for `apps/worker`).
- New env: `WORKTREE_ROOT=/var/forge/worktrees`, `REPO_CACHE_ROOT=/var/forge/repo-cache`, `AGENT_MAX_ITERATIONS=40`, `AGENT_STEP_TIMEOUT_SECONDS=600`, `AGENT_CONFIDENCE_THRESHOLD=0.72`, `LANGGRAPH_CHECKPOINT_DSN` (Postgres).
- **Named volumes** `forge-worktrees` and `forge-repo-cache` mounted into `worker` (never bind mounts for prod data, per the production checklist). The bare repo mirrors under `REPO_CACHE_ROOT` are populated/refreshed by the GitHub App slice (**F03**); F06 only adds *worktrees* off them.
- A dedicated Celery worker replica consuming the `agent` queue (`celery -A forge_worker worker -Q agent -c <n>`), with CPU/memory limits and `autoheal=true` label. Run as non-root.
- No Caddy/helm changes in V1 (helm is V2).

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols)

> DTOs marked **(contracts)** are frozen in `packages/contracts/forge_contracts` (Phase 0 Task 0.3) and are shaped *for this slice*. Types marked **(agent-runtime)** are internal to `forge_agent`.

**Frozen Protocol implemented here** (contracts):
```python
class AgentRuntime(Protocol):
    async def run(self, objective: "AgentObjective") -> "AgentRunResult": ...
    async def resume(self, agent_run_id: UUID, human_input: "HumanResumeInput") -> "AgentRunResult": ...
```

**Input / output DTOs (contracts):**
```python
class RepoTarget(BaseModel):
    repo: str                      # github.com/org/api
    base_branch: str = "main"
    branch_prefix: str             # forge/TASK-123
    branch_name: str               # resolved branch
    worktree: bool = True

class AcceptanceCriterion(BaseModel):
    id: str                        # A1
    spec_ref: str | None           # SPEC-17/A1 (None for non-feature kinds)
    text: str

class SkillProfileSpec(BaseModel):                  # produced by skill-sdk (F11)
    name: str
    requires_plan: bool = True
    requires_tests_before_implementation: bool = False
    min_test_coverage: int | None = None            # percent
    verification_steps: list[str] = []              # lint|type_check|unit_tests|integration_tests|coverage
    review_required: bool = True
    forbidden_shortcuts: list[str] = []             # skip_tests|no_error_handling|hardcoded_secrets

class PolicySnapshot(BaseModel):                     # subset of forge_policy.Policy needed at runtime
    repo_id: str
    commands: dict[str, str]                        # install|lint|type_check|test|test_coverage|build
    write_allow: list[str]                          # globs
    write_deny: list[str]
    allowed_actions: list[str]
    restricted_actions: list[str]

class RetrievedChunk(BaseModel):
    chunk_id: str; source: str; source_type: str; text: str; score: float

class ModelConfig(BaseModel):                        # BYOK — provider-agnostic
    provider: str                                   # e.g. anthropic|openai|<self-hosted>
    model: str
    api_key_ref: str                                # secret-vault reference id, NEVER the raw key
    temperature: float = 0.0
    max_tokens: int = 8192

class KnowledgeScope(BaseModel):
    repos: list[str] = []; mcp_sources: list[str] = []
    source_types: list[str] = []; freshness_min_hours: int | None = None

class AgentObjective(BaseModel):
    agent_run_id: UUID
    workspace_id: UUID
    task_id: UUID
    workflow_run_id: UUID | None
    kind: str                                       # feature|bug|chore|spike|incident|...
    objective: str                                  # title + description
    acceptance_criteria: list[AcceptanceCriterion]
    repo_target: RepoTarget
    policy: PolicySnapshot
    skill_profile: SkillProfileSpec
    agents_md: str                                  # narrative repo instructions
    initial_context: list[RetrievedChunk]
    knowledge_scope: KnowledgeScope
    model: ModelConfig
    max_iterations: int = 40
    confidence_threshold: float = 0.72

class ToolCall(BaseModel):                           # also used by policy-sdk
    action: str                                     # read_repo|write_code|...
    name: str
    args: dict[str, Any]

class Decision(BaseModel):                           # returned by PolicyEvaluator
    allowed: bool
    reason: str
    requires_approval: bool = False

class Step(BaseModel):
    seq: int
    node: str
    step_type: Literal["decision","tool_call","tool_result","message","verification","interrupt","error"]
    tool_name: str | None = None
    action: str | None = None
    input: dict[str, Any] = {}
    output: dict[str, Any] = {}
    status: Literal["ok","denied","error"] = "ok"
    policy_decision: Decision | None = None
    latency_ms: int | None = None

class AcceptanceCriterionResult(BaseModel):
    id: str; satisfied: bool; evidence: str; test_refs: list[str] = []

class VerificationResult(BaseModel):
    step: str                                       # lint|type_check|unit_tests|coverage|...
    passed: bool; summary: str
    coverage_pct: float | None = None
    raw_output_ref: str | None = None               # MinIO ref

class TokenUsage(BaseModel):
    prompt: int = 0; completion: int = 0; total: int = 0; cost_usd: float | None = None

class KnowledgeRef(BaseModel):                       # provenance for Approval-UI "knowledge provenance"
    chunk_id: str
    source: str                                     # github.com/org/api:app/middleware/auth.py
    score: float

class AgentRunResult(BaseModel):
    agent_run_id: UUID
    status: Literal["succeeded","failed","awaiting_input","cancelled"]
    confidence: float
    summary: str
    acceptance_criteria: list[AcceptanceCriterionResult]
    verifications: list[VerificationResult]
    branch_name: str
    base_commit_sha: str
    head_commit_sha: str | None
    changed_files: list[str]
    test_paths: list[str] = []                       # test files written/touched (consumed by F08 validation)
    knowledge_refs: list[KnowledgeRef] = []          # retrieved chunks that informed the change
    diff_stat: dict[str, int]                        # {files, insertions, deletions}
    needs_human_reason: str | None = None
    token_usage: TokenUsage
    steps: list[Step]
```

> **Consumer-contract reconciliation (F08).** `AgentRunResult` is the *frozen producer contract* owned by this slice in `packages/contracts/forge_contracts/agent.py`; `v1/F08-plan-execute-verify-pr-approval` consumes it. F08's draft restates the shape with divergent field names/enum values (`head_branch`/`head_sha`/`satisfied_criteria`/`status ∈ {completed,needs_input,failed}`). **This slice's shape is authoritative** — F08 must map onto it: `head_branch→branch_name`, `head_sha→head_commit_sha`, `satisfied_criteria→acceptance_criteria` (`criterion_id→id`, `rationale→evidence`), `knowledge_refs` and `test_paths` are now provided here so F08 needs no separate shape, and the status enum maps `succeeded→completed`, `awaiting_input→needs_input`, `failed→failed`, `cancelled→failed`. Reconcile when freezing the contract.

```python

class HumanResumeInput(BaseModel):
    decision: Literal["approve","reject","redirect"]
    note: str | None = None
    redirect_instruction: str | None = None         # required when decision == redirect

class ModelClient(Protocol):                         # contracts; BYOK adapter
    async def chat(self, messages: list[dict], tools: list[dict] | None,
                   *, model: str, temperature: float, max_tokens: int) -> "ModelResponse": ...
```

**Internal interfaces (agent-runtime):**
```python
class ToolResult(BaseModel):
    ok: bool
    output: Any = None
    error: str | None = None
    artifacts: list[str] = []                       # MinIO refs

class ToolContext(BaseModel, arbitrary_types_allowed=True):
    agent_run_id: UUID
    workspace_id: UUID
    worktree_path: str
    policy: PolicySnapshot
    knowledge_scope: KnowledgeScope
    deps: "RuntimeDeps"

class Tool(Protocol):
    name: str
    action: str                                     # maps to allowed_actions vocabulary
    args_schema: type[BaseModel]
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...

class ToolRegistry:
    def __init__(self, tools: Sequence[Tool]) -> None: ...
    def scoped(self, allowed: list[str], restricted: list[str]) -> "ToolRegistry": ...
    def get(self, name: str) -> Tool: ...            # raises ToolNotAllowed if out of scope
    def names(self) -> list[str]: ...
    def as_model_tool_specs(self) -> list[dict]: ...  # JSON schema specs bound to the model

class PolicyGuard:                                   # thin adapter over forge_policy.PolicyEvaluator
    def __init__(self, policy: PolicySnapshot, evaluator: PolicyEvaluator) -> None: ...
    def check(self, call: ToolCall) -> Decision: ...           # action + restricted_actions
    def check_write_path(self, path: str) -> Decision: ...     # write_allow/deny globs + traversal/symlink escape
    def check_command(self, command: str) -> Decision: ...     # only values present in policy.commands

class WorktreeHandle(BaseModel):
    repo: str; worktree_path: str; branch_name: str; base_commit_sha: str

class WorktreeSandbox:
    def __init__(self, repo_cache_root: str, worktree_root: str) -> None: ...
    async def create(self, repo: str, base_branch: str, branch_name: str) -> WorktreeHandle: ...
    async def commit_all(self, handle: WorktreeHandle, message: str) -> str: ...   # returns sha
    async def diff_stat(self, handle: WorktreeHandle) -> dict[str, int]: ...
    async def changed_files(self, handle: WorktreeHandle) -> list[str]: ...
    async def cleanup(self, handle: WorktreeHandle, *, keep_branch: bool = True) -> None: ...
    def command_runner(self, handle: WorktreeHandle) -> "SandboxCommandRunner": ...  # bound to this worktree

class CommandOutput(BaseModel):                          # contracts; shared with F08 VerificationService
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

class SandboxCommandRunner(Protocol):                    # contracts; PRODUCED here, CONSUMED by F08 run_checks
    async def run(self, command: str, *, cwd: str, timeout_s: int,
                  env: Mapping[str, str] | None = None) -> CommandOutput: ...

@dataclass
class RuntimeDeps:
    db_session_factory: Callable[[], AsyncSession]
    checkpointer: "BaseCheckpointSaver"
    model_factory: Callable[[ModelConfig], ModelClient]   # BYOK; resolves api_key_ref via vault (F37)
    policy_evaluator: PolicyEvaluator
    knowledge: KnowledgeStore                              # read_knowledge backend (v1/F05-hybrid-knowledge-retrieval)
    mcp: MCPClient                                         # query_mcp backend (v1/F09-mcp-gateway-v1)
    github: IntegrationClient                              # open_pr / push backend (v1/F03-github-app)
    object_store: ObjectStore                              # MinIO artifact sink
    step_sink: "StepSink"                                  # persists Step -> agent_steps
    audit: "AuditSink"                                     # central audit log emit() (cross-cutting/F39-audit-log)
    settings: AgentSettings

class ExecutionAgent(AgentRuntime):
    def __init__(self, deps: RuntimeDeps) -> None: ...
    async def run(self, objective: AgentObjective) -> AgentRunResult: ...
    async def resume(self, agent_run_id: UUID, human_input: HumanResumeInput) -> AgentRunResult: ...
```

**Tool catalog (V1)** — each implements `Tool`; the model only ever sees the *scoped* subset:
| name | action | args_schema (key fields) | backend |
|---|---|---|---|
| `read_repo` | `read_repo` | `path: str \| glob, max_bytes` | worktree FS (read-only, confined) |
| `list_dir` | `read_repo` | `path` | worktree FS |
| `write_code` | `write_code` | `path, content` (full-file) | worktree FS (PolicyGuard.check_write_path first) |
| `apply_patch` | `write_code` | `unified_diff` | worktree FS (path-checked per hunk) |
| `run_tests` | `run_tests` | `command_key` (lint/type_check/test/test_coverage) | subprocess in worktree (PolicyGuard.check_command) |
| `read_knowledge` | `read_knowledge` | `query, k=10` | `KnowledgeStore.search` (F05) |
| `query_mcp` | `query_mcp` | `source, resource_or_tool, args` | `MCPClient` read-only (F09) |
| `open_pr` | `open_pr` | `title, body, draft` | `IntegrationClient.open_pr` (F03), thin delegate |

`open_pr` is included so policy can grant it, but the **default V1 boundary** is that PR opening is driven by the FSM's `verifying → pr_opened` transition (F08); F06's tool merely delegates to F03 when a task's policy/skill explicitly authorizes the agent to open the PR itself.

## 5. Dependencies — features/slices that must exist first

> Slugs below are the actual file paths under `docs/implementation-slices/`. `cross-cutting/F00-platform-foundation` is the one Phase-0 prerequisite that does **not** yet have a dedicated numbered file (sibling slices reference it variously, e.g. `v1/phase0-data-model`, `cross-cutting/C01-monorepo-and-api-foundations`); reconcile its slug when the foundation slice lands. All other refs match existing files.

| Ref | Why F06 needs it | Hard/Soft |
|---|---|---|
| `cross-cutting/F00-platform-foundation` (db models, `forge_contracts`, API+worker skeleton, RBAC roles + auth dependency) | `agent_runs`/`AgentRun` model, frozen DTOs/Protocols, `agent` router stub, RBAC on routes | **Hard** |
| `cross-cutting/F37-auth-secrets-byok` (`auth-sdk`/`forge_auth`) | encrypted per-workspace BYOK vault + resolver for `ModelConfig.api_key_ref`; `Principal`/`require_role(...)` auth on routes; `SecretRedactor` reused by `redaction.py` | **Hard** |
| `cross-cutting/F39-audit-log` (`forge_contracts.audit` + `SqlAuditWriter`) | `AuditSink` Protocol + `AuditEvent` contract that `deps.audit` emits to for every tool/MCP/policy event | **Hard** (tests use a `FakeAuditSink` capturing emitted events) |
| `v1/F04-repo-policy` (`policy-sdk`/`forge_policy`) | `PolicyEvaluator.load/evaluate`, `AGENTS.md` loader, `PolicySnapshot` source of truth | **Hard** |
| `v1/F11-skill-profiles` (`skill-sdk`/`forge_skill`) | `SkillProfileSpec` (verification steps, coverage floor, requires_plan, forbidden_shortcuts) | **Hard** (a built-in `backend-tdd` default can stand in for early tests) |
| `v1/F03-github-app` (`integration-sdk`/`forge_integrations`) | bare repo mirror under `REPO_CACHE_ROOT` (worktree base), commit/push creds, `open_pr` backend | **Hard** for real runs; tests use a local fixture repo |
| `v1/F05-hybrid-knowledge-retrieval` (`knowledge-core`/`forge_knowledge`) | `read_knowledge` tool + `initial_context` (the spec-mandated hybrid pgvector+BM25+RRF+rerank pipeline; F06 only consumes `KnowledgeStore.search`) | **Soft** — stub `KnowledgeStore` returns `[]` |
| `v1/F09-mcp-gateway-v1` (`mcp-sdk`/`forge_mcp`) | `query_mcp` tool (read-only) | **Soft** — stub `MCPClient` |
| `v1/F02-spec-engine` (`spec-engine`/`forge_spec`) | acceptance criteria for `objective` and finalize mapping | **Soft** — for `chore`/`bug` kinds, AC may be empty |
| `v1/F07-feature-workflow-fsm` (`workflow-engine`/`forge_workflow`) | calls `start_agent_run` → `create_and_enqueue`; consumes `AgentRunResult`; owns `workflow_runs` | **Integration peer** — F06 must run standalone (no FSM) for its own tests |
| `v1/F08-plan-execute-verify-pr-approval` | consumes `SandboxCommandRunner` + `AgentRunResult`; calls `resume` after a human approval gate; owns the authoritative `run_checks` and the `pr` approval gate | **Integration peer** — F06 exposes the contracts; no F08 code needed for F06 tests |
| `cross-cutting/F36-human-approval-system` | the canonical approval-gate primitive/UI that resolves F06's `interrupt` (low-confidence / policy-override) before `resume` | **Integration peer** — F06 only produces `awaiting_input`; the gate lives in F36/F08 |

External libs: `langgraph`, `langgraph-checkpoint-postgres`, `langchain-core` (message/tool types), `pydantic v2`, `sqlalchemy[asyncio]`, `celery`. Model SDKs are loaded behind `ModelClient` (BYOK), not imported directly by graph code.

## 6. Acceptance criteria (numbered, testable)

1. **Golden path.** Given a valid `AgentObjective` against a fixture git repo and a scripted model that writes a file + a passing test, `ExecutionAgent.run` creates a worktree on `repo_target.branch_name` off `base_branch`, returns `AgentRunResult.status == "succeeded"` with non-empty `changed_files`, a `head_commit_sha`, and `diff_stat.files >= 1`.
2. **Tool scoping at the model boundary.** When `policy.allowed_actions` excludes `query_mcp`, `ToolRegistry.scoped(...).as_model_tool_specs()` does **not** contain `query_mcp`, and `registry.get("query_mcp")` raises `ToolNotAllowed`.
3. **Action denial is recorded, not crashed.** If the model nonetheless emits a `query_mcp` call while out of scope, the `act` node records an `agent_steps` row with `status="denied"` and a `policy_decision.allowed == False`; **no** MCP call is made; the denial is fed back as an observation.
4. **Write-path enforcement.** A `write_code` to a `write_deny` path (e.g. `.env`, `secrets/k.pem`) is denied by `PolicyGuard.check_write_path`, recorded as a `denied` step, and the file is never written to disk (verified by FS assertion).
5. **AGENTS.md + policy injected.** After `load_context`, the first model-call message list contains the literal `agents_md` content and a policy summary (assert substring + that the policy commands appear). A `message` step is persisted.
6. **`requires_plan` gate.** With a skill profile where `requires_plan=True`, any `write_code` attempted before a non-empty plan exists is blocked and re-routed to `plan` (assert no `write_code` step precedes the first `decision` plan step).
7. **Verification + coverage floor.** With `backend-tdd` (`verification_steps=[lint,type_check,unit_tests,coverage]`, `min_test_coverage=80`), the `verify` node runs `policy.commands` and a coverage of 71% yields `VerificationResult{step="coverage", passed=False}` and routes back to `act` (within budget).
8. **Retry budget exhaustion → awaiting_input.** When verification keeps failing past the budget, finalize returns `status="awaiting_input"` with `needs_human_reason` mentioning the failing step; status persisted on `agent_runs`.
9. **Ordered, append-only steps.** Every tool call / decision / verification produces exactly one `agent_steps` row with a strictly increasing `seq`; `(agent_run_id, seq)` is unique.
10. **HITL interrupt on low confidence.** If finalize confidence `< confidence_threshold`, the graph interrupts: `agent_runs.status == "awaiting_input"`, a `step_type="interrupt"` step is written, and a checkpoint exists for `thread_id == str(agent_run_id)`.
11. **Resume continuity.** `ExecutionAgent.resume(id, HumanResumeInput(decision="approve"))` continues from the checkpoint to a terminal state; **no prior tool_call step is re-executed** (assert: the count of `tool_call` rows with a given `(node, tool_name, input)` does not increase across resume).
12. **Worktree isolation + cleanup.** Two concurrent runs on the same repo receive distinct `worktree_path`s; on terminal `succeeded`, `cleanup(keep_branch=True)` removes the worktree dir but the branch + its commits remain resolvable in the repo cache.
13. **Secret redaction.** Given a model API key and a value matching a configured secret pattern, neither appears in any `agent_steps.input/output`, in `AgentRunResult`, or in emitted logs (assert against serialized output + a captured log buffer).
14. **Bounded loop.** `iteration` never exceeds `max_iterations`; on reaching it the run finalizes with `status in {failed, awaiting_input}` and `needs_human_reason == "max_iterations_reached"`.

15. **Idempotent worker.** `run_agent_task` is idempotent — invoking it twice for one `agent_run_id` starts only one graph execution (second observes `status != pending` and no-ops).
16. **BYOK.** Swapping `ModelConfig.provider/model` changes the constructed `ModelClient` with no code change; the raw key is resolved from the vault at runtime and never persisted in `agent_runs.inputs`.
17. **Central audit emission.** Every `tool_call`, `query_mcp` result, policy denial, and `interrupt` emits exactly one `AuditEvent` to `deps.audit` with `actor_type=agent_runner`, `actor_label=f"agent_run:{id}"`, the correct `action` (e.g. `tool.call`, `mcp.tool_call`, `policy.tool_denied`), `outcome` reflecting allow/deny, and `detail_ref` linking to the `agent_steps` row — asserted against a `FakeAuditSink` (Security non-negotiable: audit log for every agent action, tool call, MCP call).
18. **Knowledge provenance.** When `initial_context`/`read_knowledge` supply chunks that inform the change, `AgentRunResult.knowledge_refs` is non-empty with `{chunk_id, source, score}` (feeds the Approval-UI "knowledge provenance" must-show item via F08).
19. **Spec-gating defense-in-depth.** A `kind=="feature"` objective with empty `acceptance_criteria` fails fast (`status=="failed"`, `needs_human_reason=="missing_acceptance_criteria"`) and writes **no** `write_code` step; a `kind=="chore"` objective with empty AC runs normally.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; implement to green. Layout under `packages/agent-runtime/tests/`.

**Key fixtures (`conftest.py`):**
- `tmp_git_repo` — a temp repo with `app/`, `tests/`, `AGENTS.md`, `.forge/policy.yaml`; serves as the `REPO_CACHE_ROOT` bare mirror.
- `ScriptedModelClient` — a `ModelClient` fake driven by an ordered list of canned responses (plain text, tool calls, or a finalize JSON). Lets every graph path be deterministic without a network/LLM. (Mirrors overnight "scripted fake model".)
- `policy_fixture` — `PolicySnapshot` with realistic `write_allow=[app/**, tests/**]`, `write_deny=[.env*, secrets/**, *.pem]`, `allowed_actions`, `restricted_actions=[deploy_prod, push_to_main, delete_files]`.
- `pg_container` — Postgres (testcontainers) for checkpointer + `agent_steps` integration tests; `langgraph` schema set up via `ensure_checkpoint_schema()`.
- `stub_knowledge` / `stub_mcp` / `stub_github` — minimal Protocol stubs.
- `FakeAuditSink` — an `AuditSink` whose `emit`/`emit_async` append `AuditEvent`s to an in-memory list for assertions (AC 17).
- `redacting_log_capture` — captures logs to assert redaction.

**Unit tests:**
- `test_tool_registry.py` — `scoped()` filters by `allowed_actions`; `restricted_actions` removed even if listed in allowed; `get()` raises `ToolNotAllowed`; `as_model_tool_specs()` excludes out-of-scope (AC 2).
- `test_policy_guard.py` — write to `app/x.py` allowed; to `.env`/`secrets/**`/`../escape` denied; symlink-escape from worktree denied; `check_command` allows only `policy.commands` values; restricted action denied (AC 3,4).
- `test_worktree_sandbox.py` — `create` makes a worktree on the right branch off base; two creates → distinct paths; `commit_all` returns a real sha; `diff_stat`/`changed_files` accurate; `cleanup(keep_branch=True)` removes dir, keeps branch (AC 1,12).
- `test_redaction.py` — `Step` serialization redacts api keys + pattern matches; truncation at 64 KB pushes overflow to an artifact ref (AC 13).
- `test_routing.py` — `router_after_plan/observe/verify/finalize` return the right next node given crafted `AgentState` (plan empty → plan; verification failed + budget → act; budget exhausted → finalize; confidence < threshold → interrupt) (AC 6,7,8,10,14).
- `test_state_reducers.py` — `seq` monotonic; `add_messages` accumulates; iteration increments only in `observe`.
- `test_objective_builder.py` — `AgentRunInputBuilder.build` composes from injected providers; `api_key_ref` carried (not raw key) (AC 16); feature-kind objective with empty AC raises before any tool runs (AC 19).
- `test_verification_parsing.py` — parse `pytest --cov` term-missing / coverage.xml → `coverage_pct`; lint/type non-zero exit → `passed=False`; non-python repo → exit-code-only result.
- `test_sandbox_command_runner.py` — `WorktreeSandbox.command_runner(handle).run("ruff check .", cwd=…, timeout_s=5)` returns a `CommandOutput` with real `exit_code`/`stdout`; a sleeping command past `timeout_s` returns `timed_out=True`; cwd is confined to the worktree (the contract F08 consumes).

**Integration tests (graph end-to-end, scripted model + Postgres):**
- `test_graph_happy_path.py` — scripted: read_repo → write_code(app/x.py) → write_code(tests/test_x.py) → run_tests(pass) → finalize(conf 0.9, AC satisfied). Assert `AgentRunResult.status=="succeeded"`, branch/commit, AC mapping, ordered steps persisted (AC 1,5,9).
- `test_graph_denied_action.py` — scripted query_mcp while out of scope → denied step, no mcp call, run still completes (AC 2,3).
- `test_graph_write_deny.py` — scripted write to `.env` → denied, file absent (AC 4).
- `test_graph_verify_retry.py` — first run_tests fails coverage, second passes → exactly one retry loop, succeeds (AC 7).
- `test_graph_budget_exhausted.py` — verification always fails → `awaiting_input` + reason (AC 8).
- `test_graph_interrupt_resume.py` — finalize confidence 0.5 → interrupt + checkpoint; `resume(approve)` → terminal; assert no duplicate tool_call rows (AC 10,11).
- `test_worker_idempotency.py` — call `run_agent_task` twice → one execution; worker rehydrates `AgentObjective` from `agent_runs.inputs` (AC 15).
- `test_byok_provider_swap.py` — two `ModelConfig`s (different provider) → `model_factory` returns distinct clients; raw key never in persisted `inputs` (AC 16).
- `test_audit_emission.py` — happy-path + denied-action runs against a `FakeAuditSink`: assert a `tool.call` event per tool call, a `policy.tool_denied` event for the out-of-scope `query_mcp`, a `mcp.tool_call` event for an allowed MCP read, and that each carries `actor_label=f"agent_run:{id}"` + `detail_ref.table=="agent_steps"` (AC 17).
- `test_knowledge_provenance.py` — with `initial_context` supplying chunks and a scripted `read_knowledge` call, `AgentRunResult.knowledge_refs` is populated and persisted (AC 18).
- `test_graph_missing_ac.py` — `kind="feature"` + empty `acceptance_criteria` → `status=="failed"`, `needs_human_reason=="missing_acceptance_criteria"`, no `write_code` step; `kind="chore"` + empty AC runs to `succeeded` (AC 19).

**Coverage gate:** F06 itself is built under the `backend-tdd` discipline — ≥80% line coverage on `forge_agent`, ruff + type check green, before "done".

## 8. Security & policy considerations

- **Policy on every tool call.** `act` routes 100% of tool invocations through `PolicyGuard` *before* execution (Security table requirement). No tool runs without an `allowed` `Decision`. The model never gains access to a tool outside the run-start scope (no self-expansion).
- **Restricted actions hard-deny.** `deploy_prod, delete_files, push_to_main, modify_access_controls` (and any `restricted_actions`) are denied regardless of `allowed_actions`; attempting one raises an `interrupt` only if `requires_approval`, otherwise a flat deny.
- **Filesystem confinement.** All `write_code`/`read_repo`/`run_tests` paths are resolved and asserted to live under `worktree_path`; `..` traversal and symlinks escaping the worktree are rejected. Git worktrees give per-task isolation (V1 sandbox); cross-task FS access is structurally impossible because each run gets a private worktree dir.
- **Command allowlist.** `run_tests` may execute *only* the literal command strings from `policy.commands` — never model-authored shell. Subprocesses run with a timeout (`AGENT_STEP_TIMEOUT_SECONDS`), captured/size-capped output, and no network for `write_code`.
- **Spec-gated implementation (non-negotiable #4).** F06 runs only from a `task_ready`/`executing` FSM state that is reachable only after `spec_approved`; as defense-in-depth, `load_context` additionally rejects any `kind=="feature"` objective lacking `acceptance_criteria` (AC 19). `finalize` cites which AC it believes are satisfied (spec Gating Rule "Agent output must cite which acceptance criteria it believes are satisfied"). Merge gating itself is never F06's — that is the always-required human PR approval (F36/F08).
- **BYOK secret handling.** The model key is resolved from the encrypted per-workspace vault (`cross-cutting/F37-auth-secrets-byok`) via `api_key_ref` at runtime, held only in memory, never written to `agent_runs.inputs`/steps/logs. Agent tokens carry automatic expiry (Security: "automatic expiry for agent tokens").
- **MCP read-only.** `query_mcp` calls `MCPClient` which defaults `allow_write=false` (MCP Security Rule 1); all MCP calls are audited (rule 4 — via the `AuditSink` emit with `action=mcp.tool_call`/`mcp.resource_read`, and `mcp.write_blocked` on any write attempt) and namespace-scoped (rule 5); the same policy evaluation applies as any tool (rule 7).
- **Audit + redaction (non-negotiable: audit log).** Two layers: (1) `agent_steps` is the append-only step-level trace; (2) every security-relevant step is also emitted to the central, tamper-evident `audit_log` via `deps.audit.emit(session, AuditEvent(...))` (`cross-cutting/F39-audit-log`) in the same transaction, satisfying "Audit log — every agent action, tool call, MCP call — immutable, queryable". `redaction.py` (built on F37's shared `SecretRedactor`) strips secrets from every step `input/output`, the `AuditEvent.metadata`, the result, and logs (Security: secret redaction from logs/traces). Large/raw outputs go to MinIO with the same redaction.
- **No self-expanded scope, provably audited.** Because every denial is written both as a `denied` `agent_steps` row and a `policy.tool_denied` audit event with the matched rule, an auditor can prove (F39 Journey B) the agent never expanded its own scope — Build Prompt constraint #2.
- **HITL is the safe default for ambiguity.** Low confidence, unsatisfied required AC, or `requires_approval` actions pause via `interrupt` rather than proceeding — human approval gates risky actions (Core Principle 2; Approval Gate Types: Policy override "Always required").

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** The graph, scoped tool registry, worktree sandbox, Postgres checkpointer wiring, policy integration, verification runner, redaction, and interrupt/resume are each non-trivial and must compose. Roughly: graph+nodes+routing (M), tools+registry+policy guard (M), sandbox+verification (M), checkpointer+resume+idempotent worker (M), redaction+persistence+API (S).

Key risks:
- **LangGraph interrupt/resume + Postgres checkpointer surface** — the durable-interrupt API and `Command(resume=...)` semantics must be pinned to a specific LangGraph version; checkpoint schema setup is a deploy-time step. *Mitigation:* pin versions, cover resume with `test_graph_interrupt_resume`, run `ensure_checkpoint_schema()` in `make migrate`.
- **Deterministic testing of an LLM loop** — flaky if real models are used. *Mitigation:* `ScriptedModelClient` everywhere; real-model runs only in the F12 golden eval, not unit/integration gates.
- **Git worktrees are isolation, not a security boundary** — subprocesses can still touch the worktree FS and (unless blocked) network. Accepted for V1 per spec (Docker/Firecracker is V2). *Mitigation:* command allowlist + path confinement + timeouts; document the V1 limitation.
- **Long runs vs broker redelivery / Celery visibility** — a multi-hour run could be redelivered. *Mitigation:* `acks_late` + status-guard idempotency + checkpoint-based replay + tuned `visibility_timeout`.
- **Coverage/verification parsing across languages** — V1 is python-first (`pytest --cov`); other languages get exit-code-only verification. *Mitigation:* parser is pluggable; documented in F11 skill profiles.

## 10. Key files / paths (exact)

```
packages/agent-runtime/forge_agent/
├── __init__.py
├── runtime.py            # ExecutionAgent(AgentRuntime): run(), resume()
├── objective.py          # AgentRunInputBuilder
├── graph.py              # build_execution_graph(deps) -> CompiledStateGraph
├── state.py              # AgentState (TypedDict) + reducers
├── nodes.py              # load_context, plan, act, observe, verify, finalize
├── routing.py            # router_after_plan/observe/verify/finalize
├── checkpoint.py         # PostgresSaver wiring + ensure_checkpoint_schema()
├── llm.py                # default ModelClient factory (BYOK, provider-agnostic)
├── policy_guard.py       # PolicyGuard adapter over forge_policy.PolicyEvaluator
├── sandbox.py            # WorktreeSandbox, WorktreeHandle, SandboxCommandRunner/CommandOutput (consumed by F08)
├── verification.py       # run skill-profile verification_steps -> VerificationResult[] (via SandboxCommandRunner)
├── redaction.py          # secret redaction (wraps F37 SecretRedactor) + truncation/artifact offload
├── persistence.py        # StepSink (Step -> agent_steps), run status updates, AuditSink emit (F39)
├── settings.py           # AgentSettings (env: WORKTREE_ROOT, thresholds, timeouts)
└── tools/
    ├── __init__.py
    ├── base.py           # Tool Protocol, ToolResult, ToolContext, ToolNotAllowed
    ├── registry.py       # ToolRegistry (+ scoped())
    ├── read_repo.py      # read_repo, list_dir
    ├── write_code.py     # write_code, apply_patch
    ├── run_tests.py
    ├── read_knowledge.py
    ├── query_mcp.py
    └── open_pr.py
packages/agent-runtime/tests/        # unit + integration (see §7)
packages/agent-runtime/pyproject.toml

apps/worker/forge_worker/tasks/agent_runner.py   # run_agent_task, resume_agent_task
apps/api/forge_api/routers/agent.py              # GET /agent-runs/{id}, POST .../resume (fill stubs)
apps/api/forge_api/services/agent_runs.py        # create_and_enqueue, enqueue_resume
packages/db/forge_db/models/agent.py             # extend AgentRun model; AgentStep model
packages/db/migrations/versions/0006_agent_runtime.py  # agent_runs cols + agent_steps (shares Alembic history w/ F07)

# Consumed contracts (defined in Phase 0, shaped for this slice):
packages/contracts/forge_contracts/agent.py      # AgentObjective, AgentRunResult, KnowledgeRef, Step, ToolCall, Decision, CommandOutput, SandboxCommandRunner, ModelClient, AgentRuntime
packages/contracts/forge_contracts/audit.py      # AuditSink, AuditEvent (owned by cross-cutting/F39-audit-log; imported here)

# Deploy:
deploy/docker-compose.yml / docker-compose.dev.yml  # worker: git, WORKTREE_ROOT/REPO_CACHE_ROOT, agent queue, volumes
apps/worker/Dockerfile                              # ensure git installed
```

## 11. Research references (relevant links from the spec/research report)

- **LangGraph (agent routing, `StateGraph`, conditional edges, checkpointers, HITL interrupts):** https://langchain-ai.github.io/langgraph/ · source https://github.com/langchain-ai/langgraph
- **LangGraph production guide 2026 (smallest useful loop first, golden eval, `StateGraph`+checkpointers):** https://www.reactify-solutions.com/articles/langgraph-production-agents-2026 (research report cite:93)
- **Human-in-the-loop with LangGraph (interrupt/resume from checkpoint):** https://www.youtube.com/watch?v=4F8wvpb8JkI
- **Temporal vs LangGraph (why FSM owns top-level state, LangGraph owns agent routing):** https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines (cite:96)
- **Open SWE (isolated sandbox per task, `AGENTS.md` context loading, curated tools, structured task context):** https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents · https://github.com/langchain-ai/open-swe (cite:35)
- **Symphony (task-as-control-plane; agents work in dedicated workspaces):** https://openai.com/index/open-source-codex-orchestration-symphony/ (cite:126)
- **Multi-agent patterns — Orchestrator-Worker as the single-agent default:** https://beam.ai/agentic-insights/multi-agent-orchestration-patterns-production (cite:114)
- **MCP security (read-only default, token binding RFC 8707, validate inputs, audit) for `query_mcp`:** https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF · https://modelcontextprotocol.io/specification/2025-11-25
- **Spec sections:** Execution Agent Runtime (Product Scope table); Workflow Engine → Architecture (agent-level routing = LangGraph) & Workflow DSL (`start_agent_run`, `run_checks`, `escalation_policy.confidence_threshold=0.72`); Repo Policy System (`.forge/policy.yaml`, `AGENTS.md`); Skill Profiles (verification_steps, coverage, forbidden_shortcuts); Task Schema (allowed/restricted_actions, handoff_rules); Spec Gating Rules ("cite which acceptance criteria are satisfied"); Human Approval System (Approval Gate Types: PR "Always required before merge", Policy override "Always required"); Core Data Model (`WorkflowRun → AgentRun → steps[]`); Security table (Policy evaluation on every tool call, immutable audit log, secret redaction, MCP read-only, sandbox isolation, BYOK/secrets).

## 12. Out of scope / future

- **Top-level workflow FSM** (created → … → merged), retry/escalation orchestration, and the `start_agent_run` trigger logic — **`v1/F07-feature-workflow-fsm`** (this slice exposes `create_and_enqueue` and consumes nothing from the FSM at test time).
- **Plan → execute → verify → PR → approval wiring**, the authoritative `run_checks`, and PR-open-from-`verifying` — **`v1/F08-plan-execute-verify-pr-approval`** (F06 only provides the `open_pr` tool delegate, the `SandboxCommandRunner`, and the `AgentRunResult` F08 consumes).
- **Central audit log table, writer, query API, chain verifier, audit viewer** — **`cross-cutting/F39-audit-log`** (F06 only *emits* `AuditEvent`s through the `AuditSink`).
- **Approval-gate primitive + approval UI** — **`cross-cutting/F36-human-approval-system`** (+ the `pr` gate in F08); F06 only raises `interrupt` and exposes `resume`.
- **Hybrid retrieval pipeline** (pgvector + BM25 + RRF + Jina reranker) — **`v1/F05-hybrid-knowledge-retrieval`** (F06 calls `KnowledgeStore.search` and injects `initial_context`).
- **MCP gateway / connector layer** — **`v1/F09-mcp-gateway-v1`** (F06 calls `MCPClient` read-only).
- **GitHub App** (repo mirror sync, PR/CI/review events, push credentials) — **`v1/F03-github-app`**.
- **Run trace viewer UI** — **`v1/F10-run-trace-viewer`** (F06 supplies the `agent_steps` data + `GET /agent-runs/{id}`).
- **Skill profile authoring/registry** — **`v1/F11-skill-profiles`**.
- **Supervised multi-agent mode** (Supervisor, subagent spawning, `SubAgentRun`, scoped specialist tools) — **`v3/F27-supervised-multi-agent`**; F06 hardcodes `execution_mode = single_agent`.
- **Container / Firecracker sandboxing** — **`v2/F19-container-sandboxing` / `v3/F34-firecracker-sandbox`** (V1 sandbox is git worktrees).
- **Temporal-backed durable agent execution** — **V2**.
- **Multi-repo task execution** — **V2** (V1 targets a single `repo_target`).
- **Multi-language coverage parsers** beyond python exit-code/`pytest --cov` — future skill-profile extensions.
