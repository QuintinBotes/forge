# F25 — Temporal Workflow Engine Integration

> Phase: v2 · Spec module(s): Workflow Engine V2 (Temporal durable top-level + LangGraph agent routing), Temporal activities, Background jobs V2 (`packages/workflow-engine`, `apps/worker`, `apps/api`, `deploy/`) · Status target: **Done** = a deployment can set `WORKFLOW_ENGINE_BACKEND=temporal` and run the exact same `default_feature` lifecycle as the V1 Postgres FSM, but as a **durable Temporal Workflow** that survives worker/API restarts mid-run with no lost work and no duplicated side effects; the top-level workflow drives every state `created → … → closed` (plus the error states) via Temporal **Workflow Update** (for synchronous human/agent events), **Signals** (cancel), **Queries** (read state), and **Activities** (every effect: spec drafting, `run_agent` invoking the unchanged F06 LangGraph agent, `run_checks`, `open_pr`, approvals, notifications); retry/backoff is a durable `workflow.sleep` loop with Temporal activity `RetryPolicy`; the `WorkflowRun` + append-only `workflow_transition` Postgres projection stays byte-faithful so the board timeline, run-trace viewer, and audit log are unchanged; secrets never enter Temporal's durable event history (redacting/encrypting `PayloadCodec`); both engines implement the identical frozen `WorkflowEngine` Protocol and pass one shared cross-engine scenario suite; and `packages/workflow-engine` is green under `ruff` + `mypy` + `pytest` (including a Temporal time-skipping integration tier and a workflow-replay determinism test).

---

## 1. Intent — what & why

The spec's Technology Stack pins two workflow engines: **V1 = "Postgres FSM + LangGraph for agent routing"** (shipped as F07 + F06) and **V2 = "Temporal (durable top-level) + LangGraph (agent routing) — June 2026: best production combination for AI systems."** The research report is explicit about *why* and *which layer*:

> "use Temporal for core durable orchestration of long-running task workflows and use LangGraph for agent-level routing … The top-level workflow engine — the one that manages task state through Created → Executing → Verifying → Approved → Merged — should use Temporal-style durable execution because tasks can run for hours or days and must not be lost on restart. The agent runtime layer … can use LangGraph-style graph routing." (`docs/forge-research-report.md` → "Workflow Engine: LangGraph, Temporal, or Hybrid")

F25 implements exactly that swap, and **only** that swap:

- It **replaces the top-level engine** (F07's `PostgresWorkflowEngine`) with a `TemporalWorkflowEngine`, both implementing the *same* frozen `WorkflowEngine` Protocol so nothing upstream (API routers, the `start_run` board action, the agent trigger) changes.
- It **does not touch agent-level routing.** The F06 LangGraph `ExecutionAgent` (load_context → plan → act → observe → verify → finalize, with its own Postgres checkpointer + HITL interrupt) is invoked *inside* a Temporal Activity (`run_agent`). LangGraph stays the agentic brain; Temporal becomes the durable spine. This is the literal hybrid the spec mandates.
- It **reuses F07's deterministic transition vocabulary**: the same `WorkflowState`/`WorkflowEventType` enums, the same `default_feature.yaml` DSL, and the same guard *semantics* — but evaluated inside a Temporal Workflow under Temporal's determinism rules (IO-bound guards move to Activities/Signal payloads; see §3.3).
- It **keeps the Postgres `workflow_run` + `workflow_transition` projection** authoritative for the *product* (board timeline, run-trace viewer, immutable audit log), while Temporal's own event history is the authoritative *durability + replay* substrate. A single `persist_transition` Activity keeps the projection faithful.

Why a whole slice and not a config flag: durable execution changes the failure model. Effects must become idempotent Activities; human gates become Updates/Signals instead of HTTP→`engine.transition`; retry/backoff becomes durable timers instead of Celery countdowns; secrets must be kept out of Temporal's durable history; and "the workflow resumes from history after a crash" needs real replay/continuity tests. F25 owns the Temporal engine implementation, the workflow + activity definitions, the engine-selection plumbing, the Temporal worker, the codec, the compose/helm wiring, and the cross-engine parity test harness. It explicitly does **not** rewrite the effect *bodies* (F02/F06/F08/F16 own those) — it wraps them as Activities.

## 2. User-facing behavior / journeys

Temporal is an operator/infra concern; end-user behavior is **identical to V1 by design** (that is the acceptance bar). Journeys are observable at the operator and reviewer level.

1. **Operator enables Temporal.** An operator sets `WORKFLOW_ENGINE_BACKEND=temporal`, starts the `temporal` compose profile (`docker compose --profile temporal …`), and runs `forge-cli temporal bootstrap` (registers the namespace + retention). New `WorkflowRun`s are created with `engine_backend=temporal`; existing `postgres_fsm` runs keep running on the FSM (no forced migration of in-flight runs).
2. **Start a feature workflow (member).** Identical to F07: a member triggers a run for a `ready_for_agent` Task (`POST /workflow/runs`). A `WorkflowRun` row is created and a Temporal Workflow `forge.FeatureWorkflow` is started with `workflow_id = "wf-<workflow_run_id>"`. The run advances `created → spec_drafting`; the board timeline shows "Workflow started" exactly as before.
3. **Spec / plan / PR human gates.** When the run enters `spec_review`, `plan_review`, or `awaiting_review`, an `ApprovalRequest` is created (by an Activity) and the workflow **waits** (durably, indefinitely, surviving restarts). A human approving in the Approval UI causes the API to send a **Workflow Update** carrying the `WorkflowEvent`; the workflow validates it against the DSL and advances. To the reviewer this is the same approve/reject UI; the difference is the wait is now durable across deploys.
4. **Execute → verify → retry, durably.** On `task_ready → executing` the workflow runs the `run_agent` Activity (the F06 LangGraph agent, heartbeating). On verification failure it loops `verifying → executing` up to 3 times with **durable** exponential backoff (`workflow.sleep(30s, 60s, 120s)`); if Forge is restarted *during* a 60s backoff, the timer resumes — the work is never lost. Budget exhausted → `needs_human_input`.
5. **Crash continuity (the headline V2 benefit).** If the API and all workers are killed while a run is mid-`executing` (e.g. a multi-hour agent run) and brought back, the Temporal Workflow resumes from its event history; the in-flight Activity is retried per its `RetryPolicy` (and, because Activities are idempotent + the agent has its own LangGraph checkpoint, no code is re-written and no PR is double-opened). The operator sees the run continue, not restart.
6. **HITL interrupt / low confidence / policy conflict.** The `run_agent` Activity returns `status=awaiting_input` (it does not raise); the workflow transitions to `needs_human_input`, creates an approval/notification, and waits for a `resume`/`cancel` Update — same states and Slack/email as V1.
7. **Cancel.** A cancel from the board sends a Temporal **Signal** (`cancel_run`); the workflow records `cancelled`, runs any compensation Activity (worktree cleanup), and completes.
8. **Reads & replay (maintainer).** `GET /workflow/runs/{id}` and `/transitions` return the same DTOs (served from the Postgres projection). A maintainer can additionally open the Temporal Web UI to see the full durable event history and **replay** the run step-by-step — satisfying "replayable workflow runs with step-level inspection" at the orchestration layer.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

F25 adds **three columns to `workflow_run`** and **reuses F07's `workflow_transition`** unchanged (it is the audit/projection both engines write). One Alembic migration `packages/db/migrations/versions/<rev>_f25_temporal_engine.py` whose `down_revision` is the current head and whose ancestry **must include F07's `workflow_transition` migration** (F25 writes to that table). No new Postgres extension.

**`workflow_run` (added columns):**

| Column | Type | Notes |
|---|---|---|
| `engine_backend` | `VARCHAR(16)` NOT NULL default `'postgres_fsm'` | **CHECK** in (`postgres_fsm`,`temporal`); which engine owns this run. Indexed. |
| `temporal_workflow_id` | `VARCHAR(255)` NULL | `wf-<workflow_run_id>`; NULL for FSM runs. **Partial UNIQUE** where not null. |
| `temporal_run_id` | `VARCHAR(255)` NULL | Temporal's run id of the latest execution (changes on continue-as-new / reset); informational. |

Index `ix_workflow_run_temporal_wfid` on `temporal_workflow_id`. The existing baseline columns (`workflow_name`, `current_state` → `WorkflowState`, `execution_mode`, `status` → `RunStatus`, `context`, `started_at`, `completed_at` from `packages/db/forge_db/models/runs.py`) and F07's additions (`retry_count`, `paused_from_state`, etc.) are reused as-is; the Temporal workflow updates `current_state`/`status`/`completed_at` through the `persist_transition` Activity, so every existing reader keeps working.

**`workflow_transition` (reused from F07, not recreated):** the `persist_transition` Activity inserts exactly the same append-only rows (`sequence`, `from_state`, `to_state`, `event`, `guard_results`, `effects_dispatched`, `record`, `actor`, redacted `payload`, `idempotency_key`). The Activity is idempotent on `(workflow_run_id, idempotency_key)` so Temporal's at-least-once Activity delivery never double-writes a transition.

**Temporal server persistence is out of Forge's schema.** Temporal self-hosted keeps its own state in dedicated databases (`temporal`, `temporal_visibility`) on the same Postgres instance (separate logical DBs, created by `forge-cli temporal bootstrap` / the Temporal auto-setup image) — **not** Alembic-managed. Migration is reversible (`downgrade` drops the three columns + index; it does **not** drop `workflow_transition`, which F07 owns). Unit tests use SQLite (column subset, `JSON` for `JSONB`); the partial-unique index + CHECK are verified against the Postgres test container.

### 3.2 Backend (FastAPI routes + services/packages)

**No new routes.** F25 fills the *same* `/workflow/*` handlers F07 defined (`apps/api/forge_api/routers/workflow.py`); the only change is that `deps.get_workflow_engine` returns a `TemporalWorkflowEngine` when `settings.workflow_engine_backend == "temporal"` (or when the resolved per-workspace override selects it). The router code is engine-agnostic because both engines implement the frozen `WorkflowEngine` Protocol.

Engine selection (`apps/api/forge_api/deps.py`):

```python
def get_workflow_engine(session=Depends(get_session),
                        settings=Depends(get_settings),
                        principal=Depends(get_principal)) -> WorkflowEngine:
    backend = resolve_backend(settings, workspace_id=principal.workspace_id)  # env, optional ws override
    if backend == "temporal":
        return TemporalWorkflowEngine(session=session, client=get_temporal_client(), definitions=DEFINITIONS)
    return PostgresWorkflowEngine(session=session, dispatcher=CeleryEffectDispatcher(celery_app), definitions=DEFINITIONS)
```

New package subtree `packages/workflow-engine/forge_workflow/temporal/`:

```
forge_workflow/temporal/
├── __init__.py
├── engine.py          # TemporalWorkflowEngine(WorkflowEngine): start/transition/get_run/list_runs/history/definition
├── workflows.py       # @workflow.defn FeatureWorkflow (+ submit_event Update, cancel_run Signal, queries)
├── activities.py      # @activity.defn wrappers around effect bodies + persist_transition + load_guard_inputs
├── determinism.py     # pure DSL transition evaluator (no IO) reused inside the workflow sandbox
├── client.py          # get_temporal_client(): connect, namespace, mTLS, data converter/codec
├── converter.py       # RedactingEncryptionCodec (PayloadCodec) — keeps secrets out of history
├── worker.py          # build_temporal_worker(): registers FeatureWorkflow + all activities on the task queue
├── config.py          # TemporalSettings (env)
└── ids.py             # workflow_id / activity idempotency-id helpers
```

- **`TemporalWorkflowEngine.start`** inserts the `WorkflowRun` row (`engine_backend=temporal`, `temporal_workflow_id=wf-<id>`), then `client.start_workflow(FeatureWorkflow.run, WorkflowParams(...), id="wf-<id>", task_queue=TQ, id_reuse_policy=REJECT_DUPLICATE, retry_policy=None)`. The partial-unique index + `REJECT_DUPLICATE` give the same single-active-run guarantee as F07's `DuplicateRunError`.
- **`TemporalWorkflowEngine.transition`** uses a **Temporal Workflow Update** (synchronous, returns a value): `handle.execute_update(FeatureWorkflow.submit_event, event)` → returns the new `WorkflowState`. The workflow's update *validator* rejects events with no DSL rule / failing guard, which the engine maps to `InvalidTransitionError`/`GuardFailedError` → HTTP 409 (identical bodies to F07). Cancel is the exception: it is a fire-and-forget **Signal** (`handle.signal(FeatureWorkflow.cancel_run, reason)`).
- **`get_run`/`list_runs`/`history`** read the **Postgres projection** (cheap, indexed, always-on) rather than querying Temporal, so the read path has no Temporal dependency and is identical for both engines. (`definition` returns the same DSL.)
- A thin `apps/api/forge_api/services/temporal_health.py` adds `temporal` reachability to `GET /readyz` only when the Temporal backend is selected (so F14's readiness contract stays honest).

`forge-cli` gains `temporal bootstrap` (register namespace + retention + create `temporal`/`temporal_visibility` DBs) and `temporal replay <workflow_id>` (download history + run the `Replayer` for debugging) — implemented in `apps/api` CLI module.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

**The Temporal worker is the new runtime component.** `apps/worker/forge_worker/temporal_main.py` is a new process entrypoint that builds and runs a `temporalio.worker.Worker` registering `FeatureWorkflow` + all Activities on task queue `forge-feature` (configurable). It shares the `apps/worker` image and dependency closure; it is a *second* process alongside the existing Celery worker (Celery is still used by FSM-backed runs and by non-workflow background jobs unless/until fully retired).

**`FeatureWorkflow` (workflows.py)** — the durable top-level orchestrator. Key structure:

```python
@workflow.defn(name="forge.FeatureWorkflow")
class FeatureWorkflow:
    @workflow.run
    async def run(self, params: WorkflowParams) -> WorkflowResult:
        evaluator = TransitionEvaluator(load_definition_inline(params.definition_name))  # pure, no IO
        state = WorkflowState.created
        await self._advance(state, WorkflowEventType.start_workflow, effects=["generate_spec_draft"], skill="spec-analyst")
        # … spec_drafting → clarification → spec_review (create_spec_approval) …
        # gate: await a human event
        ev = await self._await_event({spec_approved, spec_changes_requested, cancel})
        # … plan gate (policy-conditional, decided by load_guard_inputs activity) …
        # execute/verify/retry loop:
        for attempt in range(params.retry_policy.max_retries + 1):
            agent = await workflow.execute_activity(run_agent, RunAgentInput(...),
                        start_to_close_timeout=AGENT_TIMEOUT, heartbeat_timeout=HEARTBEAT,
                        retry_policy=AGENT_RETRY)
            if agent.status == "awaiting_input":
                await self._goto(WorkflowState.needs_human_input, effects=["pause_and_notify"])
                ev = await self._await_event({resume, cancel})
                if ev.type == cancel: return await self._cancel()
                agent = await workflow.execute_activity(resume_agent, ResumeAgentInput(...), ...)
            checks = await workflow.execute_activity(run_checks, ...)
            if all(checks.results.values()): break
            if attempt == params.retry_policy.max_retries:
                await self._goto(WorkflowState.needs_human_input, effects=["pause_and_notify"]); ...
            else:
                await workflow.sleep(backoff(params.retry_policy, attempt))   # DURABLE timer
        # open_pr → awaiting_review (create_pr_approval) → merge gate → merged → closed
        ...

    @workflow.update
    async def submit_event(self, event: WorkflowEventPayload) -> str: ...   # returns new state
    @submit_event.validator
    def _validate(self, event: WorkflowEventPayload) -> None: ...           # rejects invalid (→409)
    @workflow.signal
    async def cancel_run(self, reason: str) -> None: ...
    @workflow.query
    def current_state(self) -> str: ...
    @workflow.query
    def awaiting(self) -> list[str]: ...                                    # events currently accepted
```

**Determinism handling (the central Temporal constraint).** Workflow code must be deterministic and side-effect-free; F07's guards that read the DB (e.g. `approval_granted:spec`) cannot run inside the workflow. F25 splits guards:
- **Pure guards** evaluated in-workflow via `determinism.TransitionEvaluator` (a pure subset of F07's engine): `retry_budget_remaining/exhausted`, `all_checks_passed/any_check_failed` (over the Update/Activity payload), `confidence_below_threshold`, `merge_ready` (over the event payload). No IO.
- **IO-bound guards / preconditions** are resolved by Activities, *outside* the deterministic core: `load_guard_inputs` (an Activity) reads `task.requires_approval.plan`, the four `execute` preconditions (`repo_target_set`, `policy_loaded`, `skill_profile_set`, `knowledge_synced`), and `ci_status_green`/`spec_validated`, returning a plain `GuardInputs` struct the workflow then evaluates deterministically. Human approvals are not polled — the **approval decision arrives as the Update payload** (the human already approved; the API only signals after the `ApprovalRequest` is resolved), so `approval_granted:*` is satisfied structurally by the event itself.

**Activities (activities.py)** — every F07 *effect* becomes an idempotent `@activity.defn`. Each takes a typed input carrying `workflow_run_id` + `workspace_id` and an `idempotency_key`; bodies delegate to the **existing** slice services (no rewrite):

| Activity (`name=`) | Delegates to (existing) | Retry policy |
|---|---|---|
| `forge.persist_transition` | `WorkflowRunRepository.append_transition` + update `workflow_run` | infinite (must succeed; idempotent) |
| `forge.load_guard_inputs` | board/policy/knowledge readers (F01/F04/F05) | 3×, exp |
| `forge.generate_spec_draft` / `run_clarification` / `generate_plan` / `generate_tasks` | spec-engine (F02) | 3×, exp |
| `forge.create_spec_approval` / `create_plan_approval` / `create_pr_approval` | `ApprovalService.create` (F36; pr merge gate F08) | infinite |
| `forge.run_agent` | `ExecutionAgent.run` (F06 LangGraph) | **maximum_attempts=1** (agent owns its own retry/checkpoint) |
| `forge.resume_agent` | `ExecutionAgent.resume` (F06) | maximum_attempts=1 |
| `forge.run_checks` | verification runner (F08, independent of the agent's self-checks) | 3×, exp |
| `forge.open_pr_with_spec_traceability` | integration-sdk (F03/F08) | 3×, exp; non-retryable on 4xx |
| `forge.pause_and_notify` / `escalate_to_admin` | Slack/email (F16) + approval | 5×, exp |
| `forge.cleanup_worktree` | F06 `WorktreeSandbox.cleanup` (compensation on cancel/fail) | 3×, exp |

`run_agent` is a **long-running, heartbeating Activity**: the F06 agent loop calls `activity.heartbeat(progress)` so Temporal can detect a stuck worker; `start_to_close_timeout` is generous (default 2h, `AGENT_ACTIVITY_TIMEOUT`), `heartbeat_timeout` short (default 120s). It returns an `AgentRunResultDTO`; HITL is a normal return value (`status=awaiting_input`), never an exception, so the workflow controls the gate. Because `maximum_attempts=1` and the agent has its own LangGraph Postgres checkpoint, a worker crash → Temporal will not auto-retry the whole agent; instead the workflow's own `executing` re-entry (or a `resume_agent`) replays from the LangGraph checkpoint — no duplicated code writes / PRs.

**LangGraph: unchanged and central.** The F06 `StateGraph` (agent-level routing, conditional edges, HITL interrupt, Postgres checkpointer) is invoked verbatim inside `run_agent`/`resume_agent`. F25 adds zero LangGraph code — it only changes *who calls the agent* (a Temporal Activity instead of a Celery task). The spec's "Temporal (durable top-level) + LangGraph (agent routing)" is realized exactly here.

**Celery:** still present for FSM-backed runs and other jobs. Per the spec ("Background jobs: Redis + Celery (V1); Temporal activities (V2)"), F25 makes Temporal Activities the V2 path for *workflow* effects; wholesale Celery removal is explicitly out of scope (§12).

### 3.4 Frontend / UI (Next.js routes/components, if any)

**N/A — no net-new product UI.** The board timeline, run-trace viewer (F10), and approval UI (F08) read the same `WorkflowRun`/`workflow_transition` DTOs and the same `/workflow/*` endpoints regardless of engine. The only optional, admin-facing surface is a small read-only badge on the run/settings view showing `engine_backend` (`Postgres FSM` | `Temporal`) and, when Temporal, a deep link to the Temporal Web UI for the `workflow_id` — added to F10's Run Trace page header (`apps/web/app/(dashboard)/projects/[projectId]/tasks/[taskId]/runs/[runId]/page.tsx`) as a one-line conditional, not a new page. Operator-facing Temporal observability is the Temporal Web UI service (compose), not the Forge web app.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

F25 fills the `temporal` Compose **profile** that F14 declared as a stub. In `deploy/docker-compose.yml`, under `profiles: [temporal]`:

| Service | Image (digest-pinned) | Networks | Notes |
|---|---|---|---|
| `temporal` | `temporalio/auto-setup:<ver>@sha256:…` | `backend`, `data` | Temporal frontend+history+matching; auto-creates `temporal`/`temporal_visibility` DBs in the existing Postgres (`DB=postgres12`, `POSTGRES_SEEDS=db`). No host port. Healthcheck: `tctl --address temporal:7233 cluster health`. |
| `temporal-ui` | `temporalio/ui:<ver>@sha256:…` | `backend`, `edge` | Web UI; fronted by Caddy at `/_temporal` (admin-gated), not host-published directly. |
| `temporal-worker` | `ghcr.io/forge-platform/forge-worker@sha256:…` (same image, different command) | `backend`, `data`, `mcp` | Runs `python -m forge_worker.temporal_main`; mounts `forge_repos` (worktrees) like the Celery worker; non-root, `autoheal=true`, resource limits, log caps — inherits F14's hardening matrix. |

All four follow F14's invariants (digest pin, non-root where the image allows, healthcheck, limits, log caps, `internal: true` `data` network). The data-tier services stay unpublished; Temporal reaches Postgres over the internal `data` network. New env (append to `deploy/.env.example` + `.env.production.example`, consumed by `api` + both workers):

| Var | Default | Purpose |
|---|---|---|
| `WORKFLOW_ENGINE_BACKEND` | `postgres_fsm` | `postgres_fsm` \| `temporal` — selects the engine the API/worker wires. |
| `TEMPORAL_HOST` | `temporal:7233` | Frontend address. |
| `TEMPORAL_NAMESPACE` | `forge` | Per-deployment namespace. |
| `TEMPORAL_TASK_QUEUE` | `forge-feature` | Workflow + activity task queue. |
| `TEMPORAL_TLS_CERT` / `_KEY` / `_CA` | (empty) | mTLS to the frontend; empty = plaintext on the internal network only. |
| `TEMPORAL_CODEC_KEY` | (required when temporal) | AES key (vault ref) for the `RedactingEncryptionCodec` payload encryption-at-rest. |
| `TEMPORAL_WORKFLOW_EXEC_TIMEOUT` | `2592000` (30d) | Workflow execution timeout (long-running tasks). |
| `AGENT_ACTIVITY_TIMEOUT` | `7200` | `run_agent` start-to-close seconds. |
| `TEMPORAL_RETENTION_DAYS` | `30` | Namespace history retention (audit window). |

Caddy: add an admin-gated `/_temporal*` route → `temporal-ui:8080` (behind Forge admin auth / basic-auth in `Caddyfile`); the Temporal frontend (7233) is never exposed. Helm (V2 chart slice) picks up the same env on the api/worker deployments and adds a `temporal` subchart or external-Temporal values — referenced, not authored here.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

The frozen contract is **F07's `WorkflowEngine` Protocol** (`packages/contracts/forge_contracts/workflow.py`) — F25 adds a second implementation, it does **not** change the Protocol. The shapes below are F25-internal (`forge_workflow.temporal`) unless marked **(contracts)**.

```python
# forge_contracts/workflow.py — REUSED unchanged (defined by F07)
class WorkflowEngine(Protocol):
    def start(self, task_id: UUID, definition_name: str = "default_feature") -> WorkflowRunDTO: ...
    def transition(self, run_id: UUID, event: WorkflowEvent) -> WorkflowState: ...
    def get_run(self, run_id: UUID) -> WorkflowRunDTO: ...
    def list_runs(self, *, task_id: UUID | None = None) -> list[WorkflowRunDTO]: ...
    def history(self, run_id: UUID) -> list[WorkflowTransitionDTO]: ...
    def definition(self, name: str) -> WorkflowDefinition: ...
# WorkflowEvent, WorkflowRunDTO, WorkflowTransitionDTO, WorkflowState, WorkflowEventType: all reused from F07.
```

```python
# forge_workflow/temporal/engine.py
class TemporalWorkflowEngine:                       # implements WorkflowEngine
    def __init__(self, *, session: Session, client: "TemporalClient",
                 definitions: dict[str, WorkflowDefinition],
                 task_queue: str = "forge-feature") -> None: ...
    def start(self, task_id: UUID, definition_name: str = "default_feature") -> WorkflowRunDTO: ...
    def transition(self, run_id: UUID, event: WorkflowEvent) -> WorkflowState: ...   # via Update; cancel via Signal
    def get_run(self, run_id: UUID) -> WorkflowRunDTO: ...        # reads Postgres projection
    def list_runs(self, *, task_id: UUID | None = None) -> list[WorkflowRunDTO]: ...
    def history(self, run_id: UUID) -> list[WorkflowTransitionDTO]: ...
    def definition(self, name: str) -> WorkflowDefinition: ...
```

```python
# forge_workflow/temporal/workflows.py — Temporal dataclasses (msgspec/pydantic-compatible, codec-serialized)
@dataclass
class WorkflowParams:
    workflow_run_id: UUID
    task_id: UUID
    workspace_id: UUID
    definition_name: str = "default_feature"
    definition_version: str = "1"
    execution_mode: str = "single_agent"
    retry_policy: RetryPolicyDTO            # max_retries/backoff/initial_delay (from DSL)
    escalation_policy: EscalationPolicyDTO  # confidence_threshold/on_low_confidence/on_policy_conflict

@dataclass
class WorkflowEventPayload:                  # Update/Signal arg
    type: WorkflowEventType
    payload: dict[str, Any]
    actor: str                              # "user:<uuid>" | "agent:<uuid>" | "system"
    confidence: float | None = None
    idempotency_key: str | None = None

@dataclass
class WorkflowResult:
    final_state: WorkflowState
    transition_count: int
    failure_reason: str | None = None
```

```python
# forge_workflow/temporal/activities.py — every effect is one idempotent Activity
@dataclass
class TransitionRecord:
    workflow_run_id: UUID; workspace_id: UUID
    from_state: WorkflowState; to_state: WorkflowState
    event: WorkflowEventType; guard_results: dict[str, bool]
    effects_dispatched: list[str]; record: str | None
    actor: str; payload: dict[str, Any]; idempotency_key: str

@activity.defn(name="forge.persist_transition")
async def persist_transition(rec: TransitionRecord) -> int: ...     # returns sequence; idempotent on idem key

@dataclass
class GuardInputs:
    plan_required: bool
    preconditions: dict[str, bool]          # repo_target_set, policy_loaded, skill_profile_set, knowledge_synced
    ci_status_green: bool | None = None
    spec_validated: bool | None = None

@activity.defn(name="forge.load_guard_inputs")
async def load_guard_inputs(workflow_run_id: UUID, phase: str) -> GuardInputs: ...

@dataclass
class RunAgentInput:
    workflow_run_id: UUID; task_id: UUID; workspace_id: UUID; attempt: int

@dataclass
class AgentRunResultDTO:                     # projection of F06 AgentRunResult relevant to the FSM
    agent_run_id: UUID
    status: Literal["succeeded","failed","awaiting_input","cancelled"]
    confidence: float
    needs_human_reason: str | None
    checks: dict[str, bool]                  # lint/type_check/tests/coverage results for run_checks
    branch_name: str | None
    head_commit_sha: str | None

@activity.defn(name="forge.run_agent")
async def run_agent(inp: RunAgentInput) -> AgentRunResultDTO: ...   # invokes ExecutionAgent.run, heartbeats

@activity.defn(name="forge.resume_agent")
async def resume_agent(inp: "ResumeAgentInput") -> AgentRunResultDTO: ...   # invokes ExecutionAgent.resume
```

```python
# forge_workflow/temporal/determinism.py — pure, importable inside the workflow sandbox (no IO, no clock)
class TransitionEvaluator:
    def __init__(self, definition: WorkflowDefinition) -> None: ...
    def resolve(self, state: WorkflowState, event: WorkflowEventType,
                *, pure_guard_ctx: "PureGuardContext") -> "TransitionDecision": ...
    # raises InvalidTransitionError / GuardFailedError (same errors as F07) for the Update validator

@dataclass
class PureGuardContext:                      # only data already inside the workflow (no DB)
    retry_count: int; max_retries: int
    checks: dict[str, bool] | None
    confidence: float | None; confidence_threshold: float
    merge_signals: dict[str, bool] | None    # review_approved_by_human/ci_status_green/spec_validated from payload
    guard_inputs: GuardInputs | None         # from load_guard_inputs activity

@dataclass
class TransitionDecision:
    to_state: WorkflowState
    guard_results: dict[str, bool]
    effects: list[str]
    record: str | None
    skill: str | None
```

```python
# forge_workflow/temporal/converter.py — keep secrets out of Temporal's durable history
class RedactingEncryptionCodec(temporalio.converter.PayloadCodec):
    """encode(): run the canonical forge_auth.redaction.SecretRedactor (F37) over each payload, then
       AES-GCM encrypt with TEMPORAL_CODEC_KEY (resolved from F37's vault).
       decode(): decrypt. Both directions preserve Temporal metadata. Registered on client + worker."""
    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]: ...
    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]: ...
```

```python
# forge_workflow/temporal/config.py
class TemporalSettings(BaseSettings):
    backend: Literal["postgres_fsm","temporal"] = "postgres_fsm"   # WORKFLOW_ENGINE_BACKEND
    host: str = "temporal:7233"
    namespace: str = "forge"
    task_queue: str = "forge-feature"
    tls_cert: str | None = None; tls_key: str | None = None; tls_ca: str | None = None
    codec_key_ref: str | None = None
    workflow_exec_timeout_seconds: int = 2_592_000
    agent_activity_timeout_seconds: int = 7_200
    retention_days: int = 30
```

**DSL:** unchanged — F25 loads the *same* `forge_workflow/definitions/default_feature.yaml` F07 ships. The Temporal engine consumes `retry_policy`/`escalation_policy`/`transitions` identically; the only difference is execution substrate. The Update validator enforces the same `(from,on)` rule set, so invalid-event 409 semantics are identical across engines.

## 5. Dependencies — features/slices that must exist first

References use existing slugs under `docs/implementation-slices/`.

- **v1/F07-feature-workflow-fsm** (REQUIRED) — the frozen `WorkflowEngine` Protocol, `WorkflowState`/`WorkflowEventType` enums, the `WorkflowDefinition` DSL + `default_feature.yaml`, the guard semantics, `WorkflowRunRepository.append_transition`, and the `workflow_transition` table. F25 is a *second implementation* behind F07's Protocol and reuses F07's DSL + projection wholesale.
- **v1/F06-single-execution-agent** (REQUIRED) — `ExecutionAgent.run`/`.resume`, `AgentObjective`/`AgentRunResult`, the LangGraph graph + Postgres checkpointer, `AgentRunInputBuilder`. `run_agent`/`resume_agent` Activities call these unchanged; the agent's own checkpoint is what makes Activity-level non-retry safe.
- **v1/F08-plan-execute-verify-pr-approval** (REQUIRED) — the effect bodies that become Activities: `run_checks` (authoritative, independent of the agent's self-checks), `open_pr_with_spec_traceability`, and the `MergeGateEvaluator` (the three booleans `merge_ready` reads). Approval create/resolve themselves are F36's (above); F08's pr merge gate is re-homed through F36, and `submit_event` Updates are only sent after a real human decision.
- **v1/F14-docker-compose-selfhost** (REQUIRED) — the `temporal` Compose profile stub, the hardening matrix the new services inherit, the env contract, and `forge-cli` (F25 adds `temporal bootstrap`/`replay`).
- **cross-cutting/F36-human-approval-system** (REQUIRED) — the canonical `ApprovalService.create/resolve`, the generalized `approval_request`/`gate_type` schema (`spec|plan|pr|deploy|…`), the spec/plan/pr gate **resolution hooks** that emit the FSM events the `submit_event` Update carries, and the **server-side authorization** (agents and viewers can never approve, no human self-approval of one's own run, per-repo `review_rules`). F25's `create_spec_approval`/`create_plan_approval`/`create_pr_approval` Activities call `ApprovalService.create`; the API sends an approval Update only after F36 has resolved a real human decision. F08 supplies the pr merge gate (`MergeGateEvaluator`) re-homed through F36.
- **cross-cutting/F37-auth-secrets-byok** (REQUIRED) — the encrypted per-workspace vault that resolves `TEMPORAL_CODEC_KEY` (BYOK key material never lives in env or history); the canonical `forge_auth.redaction.SecretRedactor` the `RedactingEncryptionCodec` runs before AES-GCM encryption; the `Principal`/role ranks the API checks before issuing any human-gate Update/Signal; and the internal service token authorizing `system`/`agent` actor events.
- **cross-cutting/F39-audit-log** (REQUIRED) — the central immutable audit log that consumes `workflow_transition` rows (both engines write them) and provides `attach_immutability_trigger(workflow_transition)` (the DB-level UPDATE/DELETE block); F25 keeps these rows byte-faithful so the audit surface is engine-agnostic.
- **cross-cutting/F00-foundation** (REQUIRED, transitive) — `packages/contracts`, `packages/db` (`workflow_run` baseline + Alembic chain), `apps/api` skeleton + `deps`, `apps/worker` image, the Celery app. (Slug matches F07; it reconciles to the final Phase-0 foundation slice, referenced by siblings variously as `cross-cutting/F00-foundation` / `cross-cutting/C01-monorepo-and-api-foundations`.)
- **v1/F02-spec-engine** (SOFT) — `generate_spec_draft`/`run_clarification`/`generate_plan`/`generate_tasks` Activity bodies; until present they "park" exactly as in F07.
- **v1/F16-slack-notifications** (SOFT) — `pause_and_notify`/`escalate_to_admin` Activity bodies.
- **v1/F04-repo-policy** + **v1/F05-hybrid-knowledge-retrieval** (SOFT) — provide the data `load_guard_inputs` reads for the `execute` preconditions.
- **v1/F10-run-trace-viewer** (SOFT) — F25 adds a one-line `engine_backend` badge + Temporal Web UI deep link to F10's Run Trace page header (§3.4); F10's timeline/DTOs are otherwise unchanged.

External libs: `temporalio` (Python SDK — workflows, activities, worker, `WorkflowEnvironment` test server, `Replayer`, `PayloadCodec`), pinned to a specific version (determinism + Update API depend on it). No new model SDKs.

## 6. Acceptance criteria (numbered, testable)

1. **Engine selection.** With `WORKFLOW_ENGINE_BACKEND=temporal`, `deps.get_workflow_engine` returns a `TemporalWorkflowEngine`; with `postgres_fsm` (default) it returns F07's `PostgresWorkflowEngine`. Both satisfy `isinstance`-style structural conformance to the frozen `WorkflowEngine` Protocol (a `mypy`/`runtime_checkable` assertion plus the shared scenario suite in AC2).
2. **Cross-engine parity.** A single parametrized scenario suite (`[postgres_fsm, temporal]`) drives the full happy path `created → … → closed` and produces the **same ordered `workflow_transition` rows** (same `from_state`/`to_state`/`event`/`record` sequence) and the same final `WorkflowRunDTO.current_state` for both engines. The Temporal run is driven on the time-skipping `WorkflowEnvironment`.
3. **Start creates run + workflow.** `TemporalWorkflowEngine.start(task_id)` inserts a `workflow_run` row (`engine_backend=temporal`, `temporal_workflow_id=wf-<id>`), starts `forge.FeatureWorkflow` with `id="wf-<id>"`, and the run ends in `spec_drafting` after the auto `start_workflow` event, with exactly one `created→spec_drafting` transition persisted (`sequence=1`).
4. **Duplicate active run rejected.** Starting a second run for a Task with a live Temporal workflow is rejected by the partial-unique index on `temporal_workflow_id` and/or Temporal `WorkflowIdReusePolicy.REJECT_DUPLICATE`, surfaced as `DuplicateRunError` → 409.
5. **Human gate via Update.** From `spec_review`, `execute_update(submit_event, spec_approved)` advances to `spec_approved` and returns `"spec_approved"`; an event with no DSL rule from the current state causes the Update **validator** to reject it → `InvalidTransitionError` → 409 with `allowed_events`, and the workflow state is unchanged.
6. **Plan gate is policy-conditional.** With `task.requires_approval.plan=False` (from `load_guard_inputs`), `plan_approved` advances without a human via the `plan_not_required` deterministic guard; with `True` it requires the human `submit_event`.
7. **Execute preconditions.** `task_ready → executing` proceeds only when `load_guard_inputs` returns all four preconditions true; any false yields `PreconditionError(unmet_preconditions=[...])` → 409 listing exactly the unmet items, and the workflow stays in `task_ready`.
8. **Durable retry/backoff.** Three consecutive failing `run_checks` results loop `verifying → executing` with `workflow.sleep` delays of **30, 60, 120s** (asserted via the time-skipping clock), incrementing `retry_count`; the fourth failure routes to `needs_human_input` with a `pause_and_notify` Activity (mirrors F07 AC7 exactly).
9. **Agent HITL is a return value, not an exception.** When `run_agent` returns `status=awaiting_input`, the workflow transitions to `needs_human_input`, creates an approval/notification, and waits; a `resume` Update runs `resume_agent` and continues; a `cancel` Signal ends the run in `cancelled` after `cleanup_worktree`.
10. **Crash continuity (no lost work).** Using the `WorkflowEnvironment`, a worker shutdown/restart mid-`executing` resumes the workflow from history and completes; the run reaches a terminal state with the correct transitions and no skipped phases.
11. **Activity idempotency / no double side effects.** `persist_transition` invoked twice with the same `idempotency_key` writes one row (second is a no-op returning the same `sequence`); a forced Activity retry of `open_pr_with_spec_traceability` after a simulated timeout does not open two PRs (the underlying service is idempotency-keyed) — asserted by call-count on a fake integration client.
12. **Merge gate.** `awaiting_review --review_approved--> merged` only when the deterministic `merge_ready` guard passes over the Update payload (`review_approved_by_human ∧ ci_status_green ∧ spec_validated`); any false → `GuardFailedError` → 409, state unchanged.
13. **Reads come from the projection.** `get_run`/`list_runs`/`history` return correct DTOs **without** calling Temporal (assert the Temporal client is not invoked on the read path); the data matches what the workflow persisted via `persist_transition`.
14. **Secret redaction in durable history.** A `WorkflowEventPayload` / Activity input containing a fake API key + PEM block is, after `RedactingEncryptionCodec.encode`, neither plaintext-readable in the encoded payload nor present in `workflow_transition.payload`; `decode` round-trips the redacted-but-functional payload. No secret substring appears in Temporal history dumps, the projection, or logs.
15. **Determinism / replay.** A recorded workflow history replays cleanly through `temporalio.worker.Replayer` with no non-determinism error, proving the workflow body uses only deterministic constructs (`workflow.now`, `workflow.sleep`, Activities — no direct DB/clock/random). A deliberately non-deterministic mutation (e.g. `datetime.now()` in the workflow) makes this test fail.
16. **Cancel + compensation.** A `cancel_run` Signal from any non-terminal state moves the run to `cancelled`, runs `cleanup_worktree`, and persists a `*→cancelled` transition; the workflow completes (no orphaned timers/activities).
17. **Migration up/down.** `alembic upgrade head` adds `engine_backend` (+CHECK), `temporal_workflow_id` (+partial unique), `temporal_run_id`, and the index; `downgrade -1` drops them and leaves `workflow_transition` (F07) intact.
18. **Compose profile.** `docker compose --profile temporal -f deploy/docker-compose.yml --env-file deploy/.env.production.example config --quiet` renders; `temporal`, `temporal-ui`, and `temporal-worker` satisfy F14's contract (digest-pinned, healthcheck, limits, log caps, `autoheal=true`, only on internal/backend networks, no host ports except via Caddy's admin-gated `/_temporal`).
19. **Readiness honesty.** With the Temporal backend selected, `GET /readyz` returns 503 (listing `temporal`) when the frontend is unreachable and 200 when healthy; with the FSM backend, `/readyz` does not check Temporal.
20. **Bootstrap CLI.** `forge-cli temporal bootstrap` is idempotent: it registers the `forge` namespace with `TEMPORAL_RETENTION_DAYS` retention and ensures the `temporal`/`temporal_visibility` databases exist, succeeding cleanly on re-run.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Framework: `pytest` + `pytest-asyncio`. Three tiers — **pure unit** (no Temporal, no DB), **Temporal integration** (the in-memory time-skipping `WorkflowEnvironment` + mocked Activities), and **DB/projection** (Postgres test container). Write tests first; `packages/workflow-engine` must be green (`ruff` + `mypy` + `pytest`, ≥80% per `backend-tdd`) before "done". Real Temporal-server and real-model runs are reserved for F12 eval / release CI, never the unit gate.

**Key fixtures (`packages/workflow-engine/tests/temporal/conftest.py`):**
- `time_skip_env` — `await WorkflowEnvironment.start_time_skipping()`; yields client + a worker registered with `FeatureWorkflow` and **mocked Activities** (deterministic fakes). The backbone of the integration tier; makes the 30/60/120s backoff assertions instant.
- `mock_activities` — fakes for every Activity: `FakeAgentActivity` (scriptable to return `succeeded` / `awaiting_input` / failing checks), `FakeGuardInputs` (toggles preconditions/plan flag/merge signals), `RecordingPersist` (captures `TransitionRecord`s for ordering assertions), `FakeIntegration` (counts `open_pr` calls for idempotency).
- `definition` — `load_definition(default_feature.yaml)` (shared with F07).
- `engines` — parametrized fixture yielding `("postgres_fsm", PostgresWorkflowEngine(...))` and `("temporal", TemporalWorkflowEngine(...))` for the parity suite, both over the same seeded Task.
- `pg` — Postgres+pgvector testcontainer, migrations through `<rev>_f25_temporal_engine`.
- `secret_payload` — payload with a fake AWS key + PEM block (codec redaction).

**Pure unit — `test_determinism.py`** (TransitionEvaluator, no IO):
- `test_resolve_happy_path_edges` — every `(state,event)` resolves to the expected `to_state`/effects/record (AC2 building block).
- `test_invalid_event_raises` / `test_guard_failure_raises` — `InvalidTransitionError` (with `allowed_events`) and `GuardFailedError` (AC5, AC12).
- `test_pure_guards` — `retry_budget_*`, `all_checks_passed`, `confidence_below_threshold`, `merge_ready` over `PureGuardContext` (AC8, AC12).
- `test_evaluator_is_io_free` — import-time/static assertion that `determinism` imports no DB/clock/random module (guards the replay determinism).

**Pure unit — `test_codec.py`** (AC14): `encode` redacts then encrypts (no plaintext secret in bytes); `decode` round-trips; metadata preserved; wrong key fails closed.

**Temporal integration — `test_feature_workflow.py`** (time-skipping env):
- `test_full_happy_path_temporal` — drives `created → … → closed` via Updates; asserts `RecordingPersist` ordering + final state (AC2, AC3).
- `test_spec_gate_update_and_invalid_event` (AC5).
- `test_plan_gate_conditional` (AC6).
- `test_execute_preconditions_block` (AC7).
- `test_retry_backoff_durable_timers` — three failing `run_checks`; assert skipped time == 30+60+120 and `retry_count` progression; 4th → `needs_human_input` (AC8).
- `test_agent_awaiting_input_then_resume` and `test_agent_awaiting_input_then_cancel` (AC9, AC16).
- `test_worker_restart_resumes` — stop/replace the worker mid-run; assert completion from history (AC10).
- `test_open_pr_idempotent_on_activity_retry` — force a `run_checks`/`open_pr` Activity timeout+retry; assert single PR via `FakeIntegration` count (AC11).
- `test_merge_gate` (AC12).
- `test_cancel_signal_runs_cleanup` (AC16).

**Determinism/replay — `test_replay.py`** (AC15): capture a history from a happy-path run, run `Replayer.replay_workflow`; assert no non-determinism error. A xfail variant injects `datetime.now()` into a copy of the workflow to prove the test catches non-determinism.

**Engine parity — `test_engine_parity.py`** (AC2): the `engines` fixture runs one scenario script against both engines; assert identical transition sequences and final state. This is the contract that lets either engine ship.

**DB/projection — `test_projection_and_migration.py`**:
- `test_migration_up_down` (AC17) — assert columns/index/CHECK via `pg_constraint`/`pg_indexes`; clean downgrade leaving `workflow_transition`.
- `test_persist_transition_idempotent` (AC11) — same `idempotency_key` twice → one row.
- `test_reads_do_not_touch_temporal` (AC13) — patch the Temporal client to raise; `get_run`/`history`/`list_runs` still succeed from the projection.
- `test_duplicate_active_run` (AC4).

**API/infra — `apps/api/tests/test_workflow_engine_selection.py` + `deploy/tests/`:**
- `test_backend_selection` (AC1) — env toggles the returned engine type.
- `test_readyz_temporal` (AC19) — unreachable Temporal → 503 listing `temporal`.
- `test_temporal_profile_contract` (AC18) — extend F14's compose-contract suite over the `temporal` profile services.
- `test_temporal_bootstrap_idempotent` (AC20) — run twice against the test server / a fake `tctl`, assert idempotency.

## 8. Security & policy considerations

- **Secrets must stay out of durable history.** Temporal persists every Workflow/Activity input and output in its event history *at rest* (like F20's indexed content). The `RedactingEncryptionCodec` (`PayloadCodec`) runs the canonical `forge_auth.redaction.SecretRedactor` (F37) **and** AES-GCM-encrypts every payload with `TEMPORAL_CODEC_KEY` (resolved from F37's encrypted per-workspace vault), registered on both the client and the worker. This satisfies the Security table's "Secrets stripped from logs, traces" extended to the durable orchestration store (AC14). The Temporal Web UI sees only encrypted/redacted payloads unless an operator runs a trusted codec server.
- **RBAC on events is unchanged and enforced at the API + F36, not the workflow.** `HUMAN_GATE_EVENTS` (spec/plan/PR approvals, resume, cancel) may only be submitted by `member`/`admin` principals; `agent`/`system` events arrive only via the internal service token (F37). F36's `ApprovalService` enforces server-side authorization uniformly (agents and viewers can never approve, no human self-approval, per-repo `review_rules`) *before* the API sends the Update/Signal, so an agent cannot approve its own spec/PR — preserving "the agent never self-assigns permissions or expands its own scope." The `actor` is recorded verbatim on the persisted transition.
- **Human gates remain structural.** Approvals are only *signaled* after a real `ApprovalRequest` is resolved through F36's `ApprovalService.resolve`; the workflow's deterministic core never auto-satisfies `approval_granted:*`. The merge gate still requires human approval ∧ green CI ∧ spec validation — "Human approval is required before PR merge — always."
- **Deterministic routing, no LLM in the spine.** All top-level routing is the pure `TransitionEvaluator` + Activity-loaded guard inputs — no model call decides state. This upholds "Supervisor makes routing decisions via explicit policy, not LLM judgement." The LLM lives only inside the `run_agent` Activity (F06), behind policy-checked tools.
- **Immutable, double audit.** Every transition is an append-only `workflow_transition` row (the product audit log, consumed by `cross-cutting/F39-audit-log` into the central audit surface and protected by F39's `attach_immutability_trigger`) *and* an entry in Temporal's tamper-evident event history (retained `TEMPORAL_RETENTION_DAYS`) — giving the "immutable, queryable" audit the Security table requires, now with full orchestration-level replay.
- **Tenant isolation.** `workspace_id` rides on `WorkflowParams` and every Activity input; Activities scope all DB access by it; one Temporal namespace per deployment. (Per-workspace namespaces are a future hardening, §12.)
- **Network & transport.** The Temporal frontend (7233) is never host-published; it sits on the internal `backend`/`data` networks. `TEMPORAL_TLS_*` enables mTLS to the frontend for multi-host setups. The Temporal Web UI is reachable only through Caddy's admin-gated `/_temporal` route. Temporal's Postgres credentials are separate logical DBs on the hardened, unpublished `db` service (F14).
- **At-least-once safety.** Activities are idempotent (idempotency keys on writes; `maximum_attempts=1` for the agent which owns its own checkpoint); the persist-transition unique key prevents double audit rows even under Activity redelivery (AC11).

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (~6–8 focused engineer-days). The state vocabulary, DSL, guards, effects, and projection already exist (F07/F06/F08); F25's volume is in (a) re-expressing the FSM as a *deterministic* Temporal workflow with Updates/Signals/Queries, (b) wrapping effects as idempotent Activities without rewriting them, (c) the codec, (d) the worker + compose/helm wiring, and (e) the Temporal-specific test harness (time-skipping env + replay + cross-engine parity).

Key risks:
- **Workflow determinism violations** (high). Any hidden non-determinism (a stray `datetime.now()`, dict ordering, importing an IO module into the workflow) breaks replay and corrupts in-flight runs. *Mitigation:* the strict pure/IO guard split (§3.3), `determinism.py` import-free of IO, and the mandatory `Replayer` test (AC15) plus the xfail canary.
- **Activity idempotency under at-least-once** (high). Temporal will retry Activities; non-idempotent effects (double PR, double approval, double audit row) are the classic failure. *Mitigation:* idempotency keys on every write Activity, `maximum_attempts=1` for `run_agent` (delegating retry to the agent's own checkpoint), and AC11 asserting single side effects.
- **Two-system source-of-truth drift** (med). Temporal history vs the Postgres projection can diverge if `persist_transition` fails silently. *Mitigation:* `persist_transition` has infinite retry + unique-key idempotency and runs as the *first* Activity of each transition; the projection is treated as a derived view, Temporal history as truth; a reconciliation note in `docs/self-hosting`.
- **Temporal SDK/version coupling** (med). Workflow Update (used for synchronous `transition`) and `WorkflowEnvironment` time-skipping APIs are version-sensitive. *Mitigation:* pin `temporalio` exactly; isolate all Temporal types behind `forge_workflow.temporal`.
- **Operational overhead** (med, accepted by design). Temporal adds a server + worker + two DBs — the very overhead the research report says V1 FSM avoids. *Mitigation:* it is **opt-in** behind `WORKFLOW_ENGINE_BACKEND` + a Compose profile; default stays FSM; docs frame Temporal as the scale-up path.
- **Long agent Activities vs timeouts** (med). A multi-hour `run_agent` can hit start-to-close/heartbeat timeouts. *Mitigation:* heartbeating + generous `AGENT_ACTIVITY_TIMEOUT`, short `heartbeat_timeout` to detect true stalls, and LangGraph checkpoint replay on re-entry.
- **In-flight migration of existing FSM runs** (low). *Mitigation:* explicitly out of scope — only *new* runs use the selected backend; existing `postgres_fsm` runs finish on the FSM (§12).

## 10. Key files / paths (exact)

Create:
- `packages/workflow-engine/forge_workflow/temporal/__init__.py`
- `packages/workflow-engine/forge_workflow/temporal/engine.py` (`TemporalWorkflowEngine`)
- `packages/workflow-engine/forge_workflow/temporal/workflows.py` (`FeatureWorkflow` + Update/Signal/Query)
- `packages/workflow-engine/forge_workflow/temporal/activities.py` (all `@activity.defn` effect wrappers + `persist_transition` + `load_guard_inputs`)
- `packages/workflow-engine/forge_workflow/temporal/determinism.py` (`TransitionEvaluator`, pure)
- `packages/workflow-engine/forge_workflow/temporal/converter.py` (`RedactingEncryptionCodec`)
- `packages/workflow-engine/forge_workflow/temporal/client.py` (`get_temporal_client`)
- `packages/workflow-engine/forge_workflow/temporal/worker.py` (`build_temporal_worker`)
- `packages/workflow-engine/forge_workflow/temporal/config.py` (`TemporalSettings`)
- `packages/workflow-engine/forge_workflow/temporal/ids.py`
- `packages/workflow-engine/tests/temporal/conftest.py`
- `packages/workflow-engine/tests/temporal/test_determinism.py`
- `packages/workflow-engine/tests/temporal/test_codec.py`
- `packages/workflow-engine/tests/temporal/test_feature_workflow.py`
- `packages/workflow-engine/tests/temporal/test_replay.py`
- `packages/workflow-engine/tests/temporal/test_engine_parity.py`
- `packages/workflow-engine/tests/temporal/test_projection_and_migration.py`
- `apps/worker/forge_worker/temporal_main.py` (Temporal worker process entrypoint)
- `packages/db/migrations/versions/<rev>_f25_temporal_engine.py`
- `apps/api/tests/test_workflow_engine_selection.py`

Edit (extend):
- `apps/api/forge_api/deps.py` (`get_workflow_engine` backend switch, `get_temporal_client`)
- `apps/api/forge_api/services/temporal_health.py` + `/readyz` wiring (new file + edit of health router)
- `apps/api/forge_api/cli.py` (add `temporal bootstrap` / `temporal replay`)
- `packages/db/forge_db/models/runs.py` (add `engine_backend`, `temporal_workflow_id`, `temporal_run_id` to `WorkflowRun`)
- `packages/db/forge_db/models/enums.py` (add `EngineBackend` StrEnum: `postgres_fsm`,`temporal`)
- `deploy/docker-compose.yml` (fill `temporal`/`temporal-ui`/`temporal-worker` under `profiles: [temporal]`)
- `deploy/caddy/Caddyfile` (admin-gated `/_temporal*` route)
- `deploy/.env.example`, `deploy/.env.production.example` (add `WORKFLOW_ENGINE_BACKEND`, `TEMPORAL_*`, `AGENT_ACTIVITY_TIMEOUT`)
- `deploy/tests/test_compose_contract.py` (extend to cover the `temporal` profile — AC18)
- `packages/workflow-engine/pyproject.toml` (add `temporalio` dependency, pinned)
- `apps/web/app/(dashboard)/projects/[projectId]/tasks/[taskId]/runs/[runId]/page.tsx` (OPTIONAL — F10 Run Trace header: one-line `engine_backend` badge + Temporal Web UI deep link, §3.4)

Reused unchanged (dependencies, not edited): `packages/contracts/forge_contracts/workflow.py` (Protocol + DTOs), `forge_workflow/states.py`, `forge_workflow/dsl.py`, `forge_workflow/definitions/default_feature.yaml`, `packages/agent-runtime/forge_agent/runtime.py`.

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` → **Technology Stack**: "Workflow engine (V2) = Temporal (durable top-level) + LangGraph (agent routing) — June 2026: best production combination for AI systems"; "Background jobs: … Temporal activities (V2)".
- `docs/FORGE_SPEC.md` → **Workflow Engine → Architecture** table ("Top-level task workflows = Temporal (V2) … Durable execution, guaranteed completion"; "Agent-level routing = LangGraph StateGraph") and the **Default Feature Workflow States** + **Workflow DSL** (the exact states/retry/escalation F25 drives durably).
- `docs/FORGE_SPEC.md` → **Phased Roadmap → Phase 2 (V2)**: "Temporal workflow engine integration".
- `docs/FORGE_SPEC.md` → **docker-compose.yml Service List**: the "Optional (V2): temporal — Temporal workflow server" the compose profile fills.
- `docs/forge-research-report.md` → **"Workflow Engine: LangGraph, Temporal, or Hybrid"**: Temporal for durable top-level orchestration of long-running tasks that "must not be lost on restart"; LangGraph for agent-level routing; "Temporal provides a self-hosted service guide and a local development server, which makes it appropriate as the optional workflow backbone."
- Temporal vs LangGraph (June 2026, the hybrid recommendation): https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines
- Temporal self-hosting guide (auto-setup, namespaces, retention, Postgres persistence): https://docs.temporal.io/self-hosted-guide
- LangGraph (the agent layer Temporal wraps, unchanged): https://langchain-ai.github.io/langgraph/
- Sibling slices: `docs/implementation-slices/v1/F07-feature-workflow-fsm.md` (§12 names "Temporal migration (V2) … behind the `WorkflowEngine` Protocol so the swap is contained" — F25 is that swap), `docs/implementation-slices/v1/F06-single-execution-agent.md` (§12 "Temporal-backed durable agent execution — V2"), `docs/implementation-slices/v1/F14-docker-compose-selfhost.md` (the `temporal` profile stub).

## 12. Out of scope / future

- **Retiring Celery.** F25 makes Temporal Activities the V2 path for *workflow effects*; Celery remains for FSM-backed runs and other background jobs (indexers, syncers). A full Celery→Temporal migration of all background work is a later slice.
- **Migrating in-flight FSM runs to Temporal.** Only *new* runs use the selected backend; existing `postgres_fsm` runs complete on the FSM. Live cut-over of running workflows is not supported.
- **Incident workflow on Temporal.** The `alert_received → … → postmortem_created` definition (`docs/implementation-slices/v2/F17-incident-workflows.md`) is a separate `@workflow.defn`; F25 ships only `FeatureWorkflow`. The DSL/evaluator are reused when it lands.
- **Temporal Cloud / managed Temporal.** F25 targets self-hosted Temporal (auto-setup image, shared Postgres). Temporal Cloud connection (TLS + API key) is a config-only follow-up.
- **Per-workspace namespaces / multi-namespace isolation.** F25 uses one `forge` namespace with `workspace_id` scoping in Activities; namespace-per-tenant is future hardening.
- **Codec server for the Temporal Web UI.** F25 ships the encrypting `PayloadCodec` on client+worker; a trusted codec-server endpoint so operators can decrypt payloads in the Web UI (with auth) is deferred.
- **Helm Temporal subchart.** The V2 Helm chart slice authors the production K8s Temporal deployment; F25 only adds the env keys and references it.
- **Container/Firecracker sandbox for `run_agent`.** The agent still runs in a git worktree (V1 sandbox); stronger per-activity isolation is the V2/V3 sandbox slice.
- **Multi-repo task execution** and **supervised multi-agent** orchestration — separate roadmap slices; F25 keeps `execution_mode=single_agent` semantics identical to F07.
