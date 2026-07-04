# F07 — Default Feature Workflow State Machine (Postgres FSM)

> Phase: v1 · Spec module(s): Workflow Engine (default feature states, transitions, retry/escalation policy, workflow DSL) · Status target: **Done** = a `default_feature` workflow can be started for a Task, driven through every state from `created` → `closed` via events, with retries, escalation, human-gate guards, preconditions, an append-only transition audit, and a parsed-from-YAML DSL — all atomic, idempotent, and green under `ruff` + types + `pytest`.

---

## 1. Intent — what & why

Forge's top-level orchestration in V1 is a **deterministic, Postgres-backed finite state machine** (no Temporal yet — that is V2). It is the durable control plane that moves a feature Task through the Spec-Driven Development lifecycle and the implement→verify→PR→merge flow:

```
created -> spec_drafting -> clarification -> spec_review -> spec_approved
-> plan_drafting -> plan_review -> task_generation -> task_ready
-> executing -> verifying -> pr_opened -> awaiting_review -> merged -> closed
Error paths: -> needs_human_input | -> failed | -> cancelled
```

Why a hand-rolled FSM rather than LangGraph or Temporal at this layer:

- **Separation of concerns (per spec "Workflow Engine" table).** The *top-level task workflow* is durable state in Postgres; *agent-level routing* (plan→act→observe) is LangGraph inside the agent runtime (`v1/F06-single-execution-agent`). The FSM never calls an LLM — routing is by **explicit policy/guards, not LLM judgement** (core design principle).
- **Low operational overhead for V1** (research report: "a lightweight alternative workflow state machine backed by Postgres is a valid V1 approach that avoids Temporal's operational overhead").
- **Human-in-the-loop is first-class.** `spec_review`, `plan_review`, `awaiting_review`, and `needs_human_input` are real states with guard-gated exits, so approval/escalation/rollback are structural, not bolted on.
- **Replayability & audit.** Every transition is an immutable row, giving the "replayable workflow runs with step-level inspection" the observability section requires.

This slice owns: the state/event enums, the YAML **workflow DSL** parser + validator, the guard/precondition/effect registries, the `PostgresWorkflowEngine`, the `WorkflowRun`/`WorkflowTransition` persistence + migration, the `/workflow/*` API handlers, and the Celery effect-dispatch + delayed-event worker tasks. It does **not** implement the effects themselves (drafting specs, running the agent, opening PRs) — those are dispatched by name to other slices.

---

## 2. User-facing behavior / journeys

The FSM is mostly machine-facing, surfaced through the board timeline and run-trace viewer, but it has concrete user-observable behavior:

1. **Start a feature workflow.** A member with a Task in status `ready_for_agent` triggers a run (board action or `POST /workflow/runs`). A `WorkflowRun` is created in state `created`, immediately advances to `spec_drafting`, and the `generate_spec_draft` effect (skill `spec-analyst`) is dispatched. The unified timeline shows a "Workflow started" event.
2. **Spec gate.** When the draft + clarification complete, the run enters `spec_review` and an `ApprovalRequest(kind=spec)` is created. The board shows a pending approval. The run cannot leave `spec_review` until a human approves (`spec_approved` event with guard `approval_granted:spec`) — satisfying "No implementation run without an approved spec for feature-class work."
3. **Plan gate (policy-conditional).** After `spec_approved`, the plan is generated. If the Task's `requires_approval.plan` is true, the run waits in `plan_review` for a human; otherwise it auto-advances via the `plan_not_required` guard.
4. **Execute → verify → retry.** After `task_ready`, the run enters `executing` only if preconditions hold (`repo_target_set`, `policy_loaded`, `skill_profile_set`, `knowledge_synced`); otherwise the event is rejected with the exact unmet preconditions. On `checks_failed`, the run loops `verifying → executing` up to **3** times with **exponential backoff** (30s, 60s, 120s); when the budget is exhausted it moves to `needs_human_input` and notifies.
5. **Confidence / policy escalation.** If the agent reports confidence below **0.72**, the run pauses (`needs_human_input`, "pause and notify"); on a policy conflict it escalates to admin (`needs_human_input` + admin approval request).
6. **PR + merge gate.** On `checks_passed` the run opens a PR (`pr_opened` → `awaiting_review`) and creates an `ApprovalRequest(kind=pr)`. It reaches `merged` only when **all** of `review_approved_by_human`, `ci_status_green`, `spec_validated` hold — "Human approval is required before PR merge — always."
7. **Resume / cancel.** From `needs_human_input` a human can `resume` (returns to the paused-from state) or `cancel`. Any non-terminal state accepts `cancel` (→ `cancelled`) and `fail` (→ `failed`).
8. **Invalid action feedback.** Sending an event that has no rule from the current state returns HTTP 409 with a machine-readable `allowed_events` list; a guard failure returns 409 with the failing guard. No silent no-ops.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

Baseline `workflow_run`, `agent_run`, `approval_request`, `task` tables are created in the foundation slice (**`cross-cutting/F00-foundation`**, `packages/db`, Task 0.2). This slice adds an Alembic migration `xxxx_f07_workflow_fsm` that **extends `workflow_run`** and **creates `workflow_transition`**. File: `packages/db/forge_db/models/workflow.py` (extend) + `packages/db/migrations/versions/<rev>_f07_workflow_fsm.py`.

**`workflow_run` (added/confirmed columns):**

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID` PK | (baseline) |
| `task_id` | `UUID` FK→`task.id` | (baseline) indexed |
| `workspace_id` | `UUID` FK→`workspace.id` | (baseline) tenant scoping |
| `current_state` | `VARCHAR(32)` | enum `WorkflowState`; **CHECK** constraint on the 18 values; indexed |
| `execution_mode` | `VARCHAR(32)` | enum `ExecutionMode` (`single_agent` default) |
| `definition_name` | `VARCHAR(64)` | e.g. `default_feature` |
| `definition_version` | `VARCHAR(16)` | e.g. `1` |
| `retry_count` | `INTEGER` NOT NULL default `0` | execute-loop budget counter |
| `paused_from_state` | `VARCHAR(32)` NULL | state to return to on `resume` |
| `scheduled_resume_at` | `TIMESTAMPTZ` NULL | set when a delayed event is scheduled |
| `failure_reason` | `TEXT` NULL | set on `failed`/escalation |
| `last_event` | `VARCHAR(48)` NULL | last accepted event type |
| `version` | `INTEGER` NOT NULL default `0` | optimistic-lock counter (bumped each transition) |
| `created_at`/`updated_at` | `TIMESTAMPTZ` | (baseline) |

Constraint: **partial unique index** `uq_workflow_run_active_task` on `(task_id)` WHERE `current_state NOT IN ('closed','failed','cancelled')` — at most one live run per Task.

**`workflow_transition` (new, append-only):**

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID` PK | |
| `workflow_run_id` | `UUID` FK→`workflow_run.id` | indexed |
| `sequence` | `INTEGER` NOT NULL | monotonic per run, starts at 1 |
| `from_state` | `VARCHAR(32)` NOT NULL | |
| `to_state` | `VARCHAR(32)` NOT NULL | |
| `event` | `VARCHAR(48)` NOT NULL | `WorkflowEventType` |
| `guard_results` | `JSONB` NOT NULL default `{}` | `{guard_name: bool}` |
| `effects_dispatched` | `JSONB` NOT NULL default `[]` | list of effect names |
| `record` | `VARCHAR(64)` NULL | audit label e.g. `approval_event` |
| `actor` | `VARCHAR(128)` NOT NULL | `system` \| `user:<uuid>` \| `agent:<uuid>` |
| `payload` | `JSONB` NOT NULL default `{}` | **secret-redacted** before persist |
| `idempotency_key` | `VARCHAR(128)` NULL | |
| `created_at` | `TIMESTAMPTZ` NOT NULL | |

Constraints: `UNIQUE(workflow_run_id, sequence)`; partial `UNIQUE(workflow_run_id, idempotency_key)` WHERE `idempotency_key IS NOT NULL`. **No `ON UPDATE`/`ON DELETE` cascades that would mutate rows** — table is insert-only (enforced in the repository layer, documented as immutable). The DB-level UPDATE/DELETE-blocking trigger is **deferred to `cross-cutting/F39-audit-log`**, which exports a reusable `attach_immutability_trigger(table)` helper that `workflow_transition` adopts in its migration once F39 lands (until then, append-only is enforced in `WorkflowRunRepository`).

Migration must be reversible (`downgrade` drops `workflow_transition` and the added `workflow_run` columns/indexes). Unit tests run against SQLite (column subset, JSON instead of JSONB via SQLAlchemy `JSON`); the migration + partial indexes are verified against the Postgres test container in Phase 2.

### 3.2 Backend (FastAPI routes + services/packages)

Package: `packages/workflow-engine/forge_workflow/`. API handlers fill the Phase-0 stub `apps/api/forge_api/routers/workflow.py` (router already mounted in `main.py`).

Module layout:

```
packages/workflow-engine/forge_workflow/
├── __init__.py
├── states.py            # WorkflowState, WorkflowEventType, TERMINAL_STATES, HUMAN_GATE_EVENTS
├── dsl.py               # WorkflowDefinition, TransitionRule, RetryPolicy, EscalationPolicy, load_definition()
├── guards.py            # GuardRegistry + built-in guards; GuardContext
├── effects.py           # EffectRegistry, WorkflowEffectDispatcher Protocol, NullEffectDispatcher
├── engine.py            # PostgresWorkflowEngine (implements WorkflowEngine), transition algorithm
├── repository.py        # WorkflowRunRepository (SQLAlchemy; row-lock, append transition)
├── errors.py            # InvalidTransitionError, GuardFailedError, PreconditionError, TerminalStateError, UnknownDefinitionError, DuplicateRunError
└── definitions/
    └── default_feature.yaml   # bundled canonical workflow DSL (see §4)
```

`apps/api/forge_api/routers/workflow.py` handlers (all auth-required; principal from `deps.get_principal`):

| Method & path | Handler | RBAC | Returns |
|---|---|---|---|
| `POST /workflow/runs` | `start_run(body: StartRunRequest)` | member+ | `WorkflowRunDTO` 201 |
| `GET /workflow/runs/{run_id}` | `get_run` | viewer+ | `WorkflowRunDTO` |
| `GET /workflow/runs?task_id=` | `list_runs` | viewer+ | `list[WorkflowRunDTO]` |
| `POST /workflow/runs/{run_id}/events` | `send_event(body: WorkflowEvent)` | member+ (human-gate events: member/admin only; `system`/`agent` actor events only via service token) | `WorkflowRunDTO` |
| `GET /workflow/runs/{run_id}/transitions` | `history` | viewer+ | `list[WorkflowTransitionDTO]` |
| `GET /workflow/definitions/{name}` | `get_definition` | viewer+ | `WorkflowDefinitionDTO` (states + edges, for UI graph) |

Error mapping (FastAPI exception handlers registered for the workflow errors): `InvalidTransitionError`→409 (`{detail, current_state, allowed_events}`), `GuardFailedError`→409 (`{detail, failed_guards}`), `PreconditionError`→409 (`{detail, unmet_preconditions}`), `TerminalStateError`→409, `UnknownDefinitionError`→404, `DuplicateRunError`→409.

The engine is constructed in `deps.py` as a request-scoped dependency `get_workflow_engine(session, dispatcher)` where the API wires `CeleryEffectDispatcher` (real) and tests wire `NullEffectDispatcher`.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

`apps/worker/forge_worker/tasks/workflow.py` (Celery app from Phase 0). The FSM dispatches **effects** and **delayed events** through Celery; effect *bodies* live in other slices and are looked up by name.

- `run_effect(run_id: str, effect: str, skill: str | None, payload: dict)` — Celery task that resolves `effect` in a worker-side `EffectRegistry` and calls the registered handler (e.g. `generate_spec_draft`→spec-engine, `start_agent_run`→agent-runtime, `run_checks`→agent-runtime, `open_pr_with_spec_traceability`→integration-sdk, `pause_and_notify`/`escalate_to_admin`→integration-sdk Slack/email + approval creation). On completion, the handler is expected to call back `POST /workflow/runs/{id}/events` (or directly enqueue `deliver_event`) with the follow-up event and an `idempotency_key`. For V1, unregistered effects are **parked** (logged `# PARKED: effect <name> not yet wired`) and the run remains in-state — never faked.
- `deliver_event(run_id: str, event: dict)` — Celery task that calls `engine.transition(run_id, WorkflowEvent(**event))` in its own DB session/transaction. Used for delayed/backoff re-entry and for cross-slice callbacks.
- Backoff: when the engine matches the `verifying --checks_failed--> executing` rule with guard `retry_budget_remaining`, it increments `retry_count`, computes `delay = retry_policy.initial_delay_seconds * 2**(retry_count-1)`, and schedules the executing effect via `dispatcher.dispatch(..., delay_seconds=delay)` (Celery `apply_async(countdown=delay)`), setting `scheduled_resume_at`.

LangGraph: **N/A here** — agent routing is owned by `v1/F06-single-execution-agent`. The FSM only emits the `start_agent_run` effect and consumes `agent_completed` / `agent_low_confidence` / `policy_conflict` events the agent runtime produces.

### 3.4 Frontend / UI (Next.js routes/components)

Minimal in this slice; the board/run-trace UI (`v1/F01-project-board`, `v1/F10-run-trace-viewer`) renders the data. This slice guarantees the read contracts those consume:

- `GET /workflow/runs/{id}` + `GET /workflow/runs/{id}/transitions` feed the **unified timeline** and **run-trace viewer**.
- `GET /workflow/definitions/{name}` returns the state graph (nodes + edges) so the timeline can render the current position and the (future) workflow visualizer (V3) can draw it.

No new pages/components are authored here. **N/A for net-new UI** — UI work is in `v1/F01-project-board` / `v1/F10-run-trace-viewer`; this slice only ships their JSON contracts.

### 3.5 Infra / deploy (compose, helm, caddy)

No new services. Reuses Phase-0 `deploy/docker-compose.yml`: `db` (Postgres+pgvector), `redis` (Celery broker/result), `worker` (runs `run_effect`/`deliver_event`), `api`. This slice adds:

- The bundled DSL `forge_workflow/definitions/default_feature.yaml` is packaged with the wheel (declared in the package `pyproject.toml` `[tool.setuptools.package-data]` / `include`), so it ships in the `api` and `worker` images with no volume mount.
- The community example `examples/workflows/default_feature.yaml` is **authored and owned by this slice** (the OSS Strategy "Example workflow DSL configs" launch artifact for the feature workflow); it must stay byte-identical-in-meaning to the bundled DSL, and a drift-guard test (AC 20) asserts both parse to equal `WorkflowDefinition` objects via the same engine.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

These are the Phase-0 contracts in `packages/contracts/forge_contracts/workflow.py`, defined in `cross-cutting/F00-foundation` (Task 0.3) **shaped for this slice** (the same convention every V1 slice follows: foundation freezes the DTOs/Protocols in the exact shape the owning slice consumes). The `WorkflowEngine` Protocol below is the authoritative shape — `start(...) -> WorkflowRunDTO`, `transition(...) -> WorkflowState`, plus the read methods `get_run`/`list_runs`/`history`/`definition`.

```python
# forge_workflow/states.py
from enum import Enum

class WorkflowState(str, Enum):
    created = "created"
    spec_drafting = "spec_drafting"
    clarification = "clarification"
    spec_review = "spec_review"
    spec_approved = "spec_approved"
    plan_drafting = "plan_drafting"
    plan_review = "plan_review"
    task_generation = "task_generation"
    task_ready = "task_ready"
    executing = "executing"
    verifying = "verifying"
    pr_opened = "pr_opened"
    awaiting_review = "awaiting_review"
    merged = "merged"
    closed = "closed"
    needs_human_input = "needs_human_input"
    failed = "failed"
    cancelled = "cancelled"

TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.closed, WorkflowState.failed, WorkflowState.cancelled}
)

class WorkflowEventType(str, Enum):
    start_workflow = "start_workflow"
    spec_draft_completed = "spec_draft_completed"
    clarification_completed = "clarification_completed"
    spec_approved = "spec_approved"
    spec_changes_requested = "spec_changes_requested"
    plan_requested = "plan_requested"
    plan_draft_completed = "plan_draft_completed"
    plan_approved = "plan_approved"
    plan_changes_requested = "plan_changes_requested"
    tasks_generated = "tasks_generated"
    execute = "execute"
    agent_completed = "agent_completed"
    agent_low_confidence = "agent_low_confidence"
    policy_conflict = "policy_conflict"
    checks_passed = "checks_passed"
    checks_failed = "checks_failed"
    pr_ready = "pr_ready"
    review_approved = "review_approved"
    review_changes_requested = "review_changes_requested"
    close = "close"
    resume = "resume"
    fail = "fail"
    cancel = "cancel"

# events that mutate human-gated states and require a human (member/admin) actor
HUMAN_GATE_EVENTS: frozenset[WorkflowEventType] = frozenset({
    WorkflowEventType.spec_approved, WorkflowEventType.spec_changes_requested,
    WorkflowEventType.plan_approved, WorkflowEventType.plan_changes_requested,
    WorkflowEventType.review_approved, WorkflowEventType.review_changes_requested,
    WorkflowEventType.resume, WorkflowEventType.cancel,
})
```

```python
# forge_workflow/dsl.py  (Pydantic v2)
from pydantic import BaseModel, Field, model_validator

class RetryPolicy(BaseModel):
    max_retries: int = 3
    backoff: str = "exponential"          # "exponential" | "fixed"
    initial_delay_seconds: int = 30

class EscalationPolicy(BaseModel):
    confidence_threshold: float = 0.72
    on_low_confidence: str = "pause_and_notify"
    on_policy_conflict: str = "escalate_to_admin"

class TransitionRule(BaseModel):
    from_state: WorkflowState = Field(alias="from")
    on: WorkflowEventType
    to: WorkflowState
    guards: list[str] = []                # ALL must pass; "name" or "name:arg"
    preconditions: list[str] = []         # checked first; unmet -> PreconditionError
    effects: list[str] = []               # dispatched after commit, in order
    record: str | None = None             # audit label
    skill: str | None = None              # skill profile hint for the first effect
    priority: int = 0                     # higher evaluated first for same (from,on)

class WorkflowDefinition(BaseModel):
    name: str
    version: str = "1"
    default_mode: str = "single_agent"
    optional_modes: list[str] = []
    transitions: list[TransitionRule]
    retry_policy: RetryPolicy = RetryPolicy()
    escalation_policy: EscalationPolicy = EscalationPolicy()

    @model_validator(mode="after")
    def _validate_graph(self) -> "WorkflowDefinition":
        """Raises DSLValidationError if: a from/to references an unknown state;
        a guard/precondition/effect name is not in the registries; any non-terminal
        state has no outgoing rule (except needs_human_input which must reach resume/cancel);
        two rules share (from,on,priority) with identical guards (nondeterministic)."""
        ...

    def rules_for(self, state: WorkflowState, event: WorkflowEventType) -> list[TransitionRule]:
        """Matching rules sorted by priority desc (first whose guards pass wins)."""
        ...

def load_definition(source: str | dict | Path, *,
                    guard_registry: "GuardRegistry",
                    effect_registry: "EffectRegistry") -> WorkflowDefinition:
    """Parse YAML/dict/file into a validated WorkflowDefinition. Validates names against
    the supplied registries. Raises DSLValidationError on any structural problem.

    The nested spec/DSL `modes:` block is flattened on load: `modes.default` -> `default_mode`
    and `modes.optional` -> `optional_modes` (a Pydantic `mode="before"` validator does the
    remap so the bundled YAML can use the spec's `modes:` shape verbatim). After parsing, the
    engine injects the universal `cancel`/`fail` edges for every non-terminal state."""
    ...
```

```python
# forge_workflow/guards.py
from dataclasses import dataclass

@dataclass
class GuardContext:
    run: "WorkflowRunDTO"
    event: "WorkflowEvent"
    definition: "WorkflowDefinition"
    session: "Session"          # read-only use for approval/task lookups

Guard = Callable[[GuardContext, str | None], bool]   # (ctx, arg) -> bool

class GuardRegistry:
    def register(self, name: str, fn: Guard) -> None: ...
    def get(self, name: str) -> Guard: ...            # KeyError -> caught by DSL validator
    def has(self, name: str) -> bool: ...

# Built-in guards registered by default_registry():
#   approval_granted:<kind>    -> latest ApprovalRequest(run, kind) status == approved
#   plan_not_required          -> task.requires_approval.plan is False
#   retry_budget_remaining     -> run.retry_count < definition.retry_policy.max_retries
#   retry_budget_exhausted     -> run.retry_count >= definition.retry_policy.max_retries
#   all_checks_passed          -> all(event.payload["checks"].values())
#   any_check_failed           -> not all(event.payload["checks"].values())
#   confidence_below_threshold -> (event.confidence ?? 1.0) < escalation.confidence_threshold
#   merge_ready                -> review_approved_by_human AND ci_status_green AND spec_validated
#                                 (all read from event.payload booleans)
```

```python
# forge_workflow/effects.py
from typing import Protocol, Any
from datetime import datetime

class WorkflowEffectDispatcher(Protocol):
    def dispatch(self, run_id: UUID, effect: str, *,
                 skill: str | None = None,
                 payload: dict[str, Any] | None = None,
                 delay_seconds: float = 0.0) -> None: ...
    def schedule_event(self, run_id: UUID, event: "WorkflowEvent", *, eta: datetime) -> None: ...

class NullEffectDispatcher:
    """Test double: records (run_id, effect, skill, payload, delay) calls; no side effects."""
    calls: list[tuple[UUID, str, str | None, dict, float]]
    scheduled: list[tuple[UUID, "WorkflowEvent", datetime]]

class CeleryEffectDispatcher:
    """Enqueues forge_worker.tasks.workflow.run_effect / deliver_event."""
    def __init__(self, celery_app): ...
```

```python
# forge_contracts/workflow.py  (DTOs added/confirmed)
class WorkflowEvent(BaseModel):
    type: WorkflowEventType
    payload: dict[str, Any] = {}
    actor: str = "system"                 # "system" | "user:<uuid>" | "agent:<uuid>"
    confidence: float | None = None
    idempotency_key: str | None = None

class StartRunRequest(BaseModel):
    task_id: UUID
    definition_name: str = "default_feature"

class WorkflowRunDTO(BaseModel):
    id: UUID
    task_id: UUID
    workspace_id: UUID
    definition_name: str
    definition_version: str
    current_state: WorkflowState
    execution_mode: str
    retry_count: int
    paused_from_state: WorkflowState | None
    scheduled_resume_at: datetime | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

class WorkflowTransitionDTO(BaseModel):
    id: UUID
    sequence: int
    from_state: WorkflowState
    to_state: WorkflowState
    event: WorkflowEventType
    guard_results: dict[str, bool]
    effects_dispatched: list[str]
    record: str | None
    actor: str
    created_at: datetime

# forge_contracts: WorkflowEngine Protocol (refined)
class WorkflowEngine(Protocol):
    def start(self, task_id: UUID, definition_name: str = "default_feature") -> WorkflowRunDTO: ...
    def transition(self, run_id: UUID, event: WorkflowEvent) -> WorkflowState: ...
    def get_run(self, run_id: UUID) -> WorkflowRunDTO: ...
    def list_runs(self, *, task_id: UUID | None = None) -> list[WorkflowRunDTO]: ...
    def history(self, run_id: UUID) -> list[WorkflowTransitionDTO]: ...
    def definition(self, name: str) -> WorkflowDefinition: ...
```

**`transition` algorithm (engine.py, single DB transaction):**

1. `SELECT ... FOR UPDATE` the `workflow_run` row (row-level lock; serializes concurrent events).
2. If `idempotency_key` set and a `workflow_transition` with `(run_id, key)` exists → commit nothing, return current state (idempotent replay).
3. If `current_state ∈ TERMINAL_STATES` and event ∉ {} → raise `TerminalStateError`.
4. `rules = definition.rules_for(current_state, event.type)`; if empty → raise `InvalidTransitionError(allowed_events=...)`.
5. For each rule (priority desc): evaluate `preconditions` (unmet → `PreconditionError`), then `guards`; first rule whose **all** guards pass is selected. If a rule matches on event but no rule's guards pass → `GuardFailedError(failed_guards=...)`.
6. Apply state change: `current_state = rule.to`; bump `version`; set `last_event`. Special handling: retry rule → `retry_count += 1` and compute backoff delay; `needs_human_input` entry → set `paused_from_state = from_state`; `resume` → `to = paused_from_state` and clear it; `fail`/escalation → set `failure_reason`.
7. Insert `workflow_transition` (next `sequence`, redacted payload, guard_results, effect names, record).
8. Commit. **After commit**, dispatch `rule.effects` via the dispatcher (with `delay_seconds` for retry). Effects are dispatched post-commit so an enqueue failure cannot roll back a recorded transition (at-least-once + idempotent consumers cover the gap).

**Canonical bundled DSL — `forge_workflow/definitions/default_feature.yaml`:**

```yaml
name: default_feature
version: "1"
modes:
  default: single_agent
  optional: [supervised_multi_agent]

retry_policy:
  max_retries: 3
  backoff: exponential
  initial_delay_seconds: 30

escalation_policy:
  confidence_threshold: 0.72
  on_low_confidence: pause_and_notify
  on_policy_conflict: escalate_to_admin

transitions:
  - {from: created,        on: start_workflow,          to: spec_drafting,    effects: [generate_spec_draft],          skill: spec-analyst}
  - {from: spec_drafting,  on: spec_draft_completed,    to: clarification,    effects: [run_clarification]}
  - {from: clarification,  on: clarification_completed, to: spec_review,      effects: [create_spec_approval]}
  - {from: spec_review,    on: spec_approved,           to: spec_approved,    guards: [approval_granted:spec], record: approval_event}
  - {from: spec_review,    on: spec_changes_requested,  to: spec_drafting,    effects: [generate_spec_draft], skill: spec-analyst}
  - {from: spec_approved,  on: plan_requested,          to: plan_drafting,    effects: [generate_plan]}
  - {from: plan_drafting,  on: plan_draft_completed,    to: plan_review,      effects: [create_plan_approval]}
  - {from: plan_review,    on: plan_approved,           to: task_generation,  guards: [approval_granted:plan], effects: [generate_tasks], record: approval_event}
  - {from: plan_review,    on: plan_approved,           to: task_generation,  guards: [plan_not_required],     effects: [generate_tasks], priority: 1}
  - {from: plan_review,    on: plan_changes_requested,  to: plan_drafting,    effects: [generate_plan]}
  - {from: task_generation, on: tasks_generated,        to: task_ready}
  - {from: task_ready,     on: execute,                 to: executing,
     preconditions: [repo_target_set, policy_loaded, skill_profile_set, knowledge_synced],
     effects: [start_agent_run]}
  - {from: executing,      on: agent_completed,         to: verifying,        effects: [run_checks]}
  - {from: executing,      on: agent_low_confidence,    to: needs_human_input, guards: [confidence_below_threshold], effects: [pause_and_notify]}
  - {from: executing,      on: policy_conflict,         to: needs_human_input, effects: [escalate_to_admin]}
  - {from: verifying,      on: checks_passed,           to: pr_opened,        guards: [all_checks_passed], effects: [open_pr_with_spec_traceability]}
  - {from: verifying,      on: checks_failed,           to: executing,        guards: [any_check_failed, retry_budget_remaining], effects: [start_agent_run], priority: 1}
  - {from: verifying,      on: checks_failed,           to: needs_human_input, guards: [any_check_failed, retry_budget_exhausted], effects: [pause_and_notify]}
  - {from: pr_opened,      on: pr_ready,                to: awaiting_review,  effects: [create_pr_approval]}
  - {from: awaiting_review, on: review_approved,        to: merged,           guards: [merge_ready]}
  - {from: awaiting_review, on: review_changes_requested, to: executing,      effects: [start_agent_run]}
  - {from: merged,         on: close,                   to: closed}
  - {from: needs_human_input, on: resume,              to: __paused_from__}   # engine substitutes paused_from_state
  - {from: needs_human_input, on: cancel,              to: cancelled}
  # universal error edges (engine injects these for every non-terminal state):
  #   * --cancel--> cancelled
  #   * --fail--> failed
```

The engine injects `cancel`/`fail` edges for all non-terminal states at load time (so the YAML stays compact); `__paused_from__` is a sentinel resolved at transition time.

**Spec-DSL → canonical-DSL key mapping.** `docs/FORGE_SPEC.md` → Workflow Engine shows an *illustrative* DSL using `action`/`when`/`condition`/`checks`. This slice defines one canonical, unambiguous schema and maps the spec's keys onto it (a reviewer reading the spec can confirm semantic equivalence):

| Spec DSL key | Canonical key | Notes |
|---|---|---|
| `from` | `from` (→ `from_state`) | unchanged; Pydantic alias |
| `to` | `to` | unchanged |
| (event) implied by `action`/`when` | `on` | the canonical schema makes the triggering **event** explicit and required |
| `action: X` | `effects: [X]` | side effects dispatched after commit; a list (the spec's single `action` becomes a one-element list) |
| `skill: X` | `skill: X` | unchanged; hint for the first effect |
| `when: pred` / `when: [a,b,c]` | `guards: [...]` | a list of named predicates, **all** must pass; the merge edge's `when: [review_approved_by_human, ci_status_green, spec_validated]` maps to the single composite guard `merge_ready` (which reads those three payload booleans) |
| `condition: pred` | `guards: [pred]` | the spec's secondary `condition` (e.g. `retry_budget_remaining`) folds into `guards` |
| `preconditions: [...]` | `preconditions: [...]` | unchanged; checked before guards, unmet → `PreconditionError` |
| `record: X` | `record: X` | unchanged; audit label |
| `checks: [lint, type_check, tests, coverage]` | (none) | the spec's `checks` list on the `run_checks` action is **not** an FSM concern: which checks run is owned by the skill profile's `verification_steps` and resolved inside the `run_checks` effect body (`v1/F08-plan-execute-verify-pr-approval`); the FSM only branches on the resulting `payload.checks` map via `all_checks_passed`/`any_check_failed` |
| `modes: {default, optional}` | `default_mode` / `optional_modes` | flattened on load (see `load_definition`) |
| `retry_policy` / `escalation_policy` | identical | parsed verbatim into `RetryPolicy`/`EscalationPolicy` |

---

## 5. Dependencies — features/slices that must exist first

Hard (build-time) dependencies — must exist before this slice compiles/tests:

- `cross-cutting/F00-foundation` — the foundation slice supplies four things this slice builds on:
  - **packages/contracts** (Task 0.3) — `WorkflowEngine` Protocol, `WorkflowEvent`/`WorkflowRunDTO`/`WorkflowTransitionDTO` DTOs, `WorkflowState`/`WorkflowEventType` enums, `ExecutionMode`.
  - **packages/db** (Task 0.2) — baseline `WorkflowRun`, `Task`, `ApprovalRequest`, `AgentRun`, `Workspace` models + the Alembic baseline this migration stacks on.
  - **apps/api** (Task 0.4) — mounted `/workflow` router stub, `deps.get_principal`, session DI, RBAC role ranks.
  - **apps/worker / deploy** (Task 0.6) — Celery app + Redis broker for `CeleryEffectDispatcher` (unit tests bypass via `NullEffectDispatcher`).

Soft (integration-time, Phase 2) dependencies — provide the **effect bodies** and **guard data**; the FSM dispatches/reads them by name and degrades to "parked" if absent:

- `cross-cutting/F36-human-approval-system` — `create_spec_approval`/`create_plan_approval`/`create_pr_approval` effects (call `ApprovalService.create`) and the `approval_granted:*` guards (read gate status); reject/changes routing back to drafting/`cancelled`. (F36 later migrates the baseline `approval_request.kind` column this slice's guards read to `gate_type`; the guard string `approval_granted:<kind>` is reconciled there.)
- `v1/F02-spec-engine` — `generate_spec_draft`/`run_clarification`/`generate_plan`/`generate_tasks` effects (wrap F02's `spec.generate_*` Celery tasks); the `spec_validated` payload signal (from `ValidationReport`); and the defense-in-depth gates `assert_implementation_allowed` (called by the `start_agent_run` path) / `assert_merge_allowed` that F02 exposes for redundant spec-gating.
- `v1/F06-single-execution-agent` — `start_agent_run` effect (→ `create_and_enqueue`); emits the `agent_completed`/`agent_low_confidence`/`policy_conflict` events the FSM consumes; supplies the four `execute` preconditions (`repo_target_set`/`policy_loaded`/`skill_profile_set`/`knowledge_synced`). **Integration peer:** F06 must also run standalone without the FSM.
- `v1/F08-plan-execute-verify-pr-approval` — **integration peer.** F08 registers the verify→PR→merge handlers/guards *into* this FSM: the `run_checks` and `open_pr_with_spec_traceability` effect bodies, and the data behind `all_checks_passed`/`merge_ready`/`spec_validated`/`ci_status_green`. F08 lists F07 as a hard dependency and further extends `workflow_run`. (F08 in turn pulls `v1/F03-github-app` for PR/CI, `v1/F04-repo-policy` for the check commands, and `v1/F11-skill-profiles` for `verification_steps`/coverage.)
- `v1/F16-slack-notifications` — `pause_and_notify` and `escalate_to_admin` effect bodies (Slack + email); the FSM dispatches them by name and degrades to in-app/email if F16 lags.
- `v1/F10-run-trace-viewer` — consumes `workflow_run` + `workflow_transition` (via `GET /workflow/runs/{id}` and `GET .../transitions`) for the unified timeline and run-trace viewer.
- `cross-cutting/F39-audit-log` — central immutable audit: consumes `workflow_transition` rows (and optionally a canonical `AuditEvent` emitted per accepted transition via the injected `AuditSink`) into the cross-workspace audit log, and provides `attach_immutability_trigger(workflow_transition)` — the DB-level UPDATE/DELETE block this slice defers.
- `cross-cutting/F37-auth-secrets-byok` — provides `forge_auth.redaction.SecretRedactor` (used to redact `WorkflowEvent.payload` before it is persisted to `workflow_transition.payload`), the `Principal`/role ranks consumed by `send_event` RBAC, and the internal service token that authorizes `system`/`agent` actor events.
- `cross-cutting/F38-observability-cost-metrics` — consumes per-phase transition timing to compute the workflow-quality metrics the spec lists (mean time to merge, mean time to task completion, retry rate); no build-time coupling.

---

## 6. Acceptance criteria (numbered, testable)

1. `start(task_id)` creates a `WorkflowRun` in state `created`, immediately auto-issues the `start_workflow` event, ends in `spec_drafting`, dispatches the `generate_spec_draft` effect (skill `spec-analyst`), and writes exactly one `workflow_transition` (`created→spec_drafting`, `sequence=1`).
2. Starting a second run for a Task that already has a non-terminal run raises `DuplicateRunError` (and the partial unique index rejects it at the DB level).
3. The full happy path drives `created → spec_drafting → clarification → spec_review → spec_approved → plan_drafting → plan_review → task_generation → task_ready → executing → verifying → pr_opened → awaiting_review → merged → closed` given the corresponding events with passing guards, producing **14** ordered transition rows (15 states ⇒ 14 edges) with strictly increasing `sequence` 1..14.
4. `spec_review` does **not** advance on `spec_approved` unless guard `approval_granted:spec` passes; with no approved `ApprovalRequest(kind=spec)` it raises `GuardFailedError(failed_guards=["approval_granted:spec"])` and leaves state unchanged.
5. Plan gate is policy-conditional: when `task.requires_approval.plan is False`, `plan_approved` advances via the `plan_not_required` guard with no human approval; when `True`, it requires `approval_granted:plan`.
6. `task_ready --execute-->` is blocked with `PreconditionError(unmet_preconditions=[...])` listing exactly the unmet items when any of `repo_target_set`/`policy_loaded`/`skill_profile_set`/`knowledge_synced` is false; it advances only when all four hold.
7. Retry loop: from `verifying`, three consecutive `checks_failed` events return to `executing` with `retry_count` = 1,2,3 and dispatch delays of 30, 60, 120 seconds (exponential, `initial_delay_seconds * 2**(retry_count-1)`); the **fourth** `checks_failed` matches the `retry_budget_exhausted` rule → `needs_human_input` with a `pause_and_notify` effect.
8. `checks_passed` from `verifying` requires `all_checks_passed` (all values in `payload.checks` true); a mix of pass/fail does not match `checks_passed` and `any_check_failed` is what `checks_failed` evaluates.
9. Low-confidence escalation: `agent_low_confidence` with `confidence=0.5` (< 0.72) from `executing` → `needs_human_input` (`pause_and_notify`); with `confidence=0.9` the guard fails and state is unchanged.
10. `policy_conflict` from `executing` → `needs_human_input` with `escalate_to_admin` effect and `failure_reason` recorded.
11. `awaiting_review --review_approved--> merged` only when `merge_ready` holds (all of `review_approved_by_human`, `ci_status_green`, `spec_validated` true in payload); any false → `GuardFailedError`.
12. `resume` from `needs_human_input` returns the run to `paused_from_state` and clears it; `cancel`/`fail` from any non-terminal state move to `cancelled`/`failed`.
13. Sending any event whose `(current_state, type)` has no rule returns `InvalidTransitionError` with a populated `allowed_events` list; events into a terminal state raise `TerminalStateError`.
14. Idempotency: replaying an event with the same `idempotency_key` for the same run performs no second transition and returns the current (post-first-apply) state; a different key is processed normally.
15. Every accepted transition writes an append-only `workflow_transition` row with redacted `payload` (no value matching the secret pattern survives); transition rows are never updated or deleted by the engine.
16. `load_definition` parses `default_feature.yaml` into a `WorkflowDefinition`; validation **fails loudly** (`DSLValidationError`) for: unknown state in `from`/`to`, unregistered guard/precondition/effect name, a non-terminal state with no outgoing rule, and two same-`(from,on,priority)` rules with identical guards.
17. `default_feature.yaml`'s `retry_policy` (max 3 / exponential / 30s) and `escalation_policy` (0.72 / pause_and_notify / escalate_to_admin) parse to the exact spec values.
18. API: `POST /workflow/runs` → 201 + `WorkflowRunDTO`; `POST /workflow/runs/{id}/events` returns the updated DTO; guard/precondition/invalid errors map to HTTP 409 with the documented bodies; unknown definition → 404; all routes reject unauthenticated requests (401) and viewers attempting `send_event` (403).
19. Concurrency: two simultaneous `transition` calls on the same run serialize via the row lock; exactly one wins per `(from,on)` and the loser re-reads the new state (no lost update; `version` increments once per applied transition).
20. The bundled `forge_workflow/definitions/default_feature.yaml` and `examples/workflows/default_feature.yaml` parse to equal `WorkflowDefinition` objects (drift guard).

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Test framework: `pytest` + `pytest-asyncio`. Unit tests use **SQLite in-memory** (JSON columns) + `NullEffectDispatcher` + a `FakeClock`; integration tests use the **Postgres test container** + a recording Celery (eager mode). Write tests first; no module is "done" until `ruff check`, type check, and `pytest` for `packages/workflow-engine` are green.

**Key fixtures (`packages/workflow-engine/tests/conftest.py`):**
- `definition` — `load_definition(default_feature.yaml, default_registries())`.
- `engine(session, dispatcher, clock)` — `PostgresWorkflowEngine` with `NullEffectDispatcher` and injected `FakeClock`.
- `seeded_task(session, **flags)` — Task + Workspace; flags set `requires_approval.plan`, precondition signals, etc.
- `approval(session, run, kind, status)` — inserts an `ApprovalRequest`.
- `drive(engine, run, *events)` — helper that applies a list of `(event_type, payload)` and returns the final state + transition list.
- `secret_payload` — a payload containing a fake API key to assert redaction.

**Unit — DSL (`test_dsl.py`):**
- `test_loads_default_feature_definition` — name/version/retry/escalation values (AC 16, 17).
- `test_rejects_unknown_state`, `test_rejects_unregistered_guard`, `test_rejects_unregistered_effect`, `test_rejects_unreachable_nonterminal_state`, `test_rejects_nondeterministic_rules` — each raises `DSLValidationError` (AC 16).
- `test_rules_for_priority_ordering` — higher priority returned first; the two `plan_review/plan_approved` rules ordered correctly (AC 5).
- `test_alias_from_parsing` — `from` maps to `from_state` (Pydantic alias).

**Unit — guards (`test_guards.py`):**
- `approval_granted:spec` true/false against seeded `ApprovalRequest`.
- `plan_not_required` reads task flag.
- `retry_budget_remaining`/`retry_budget_exhausted` at counts 0,2,3.
- `all_checks_passed`/`any_check_failed` over `{lint:true,type_check:true,tests:false,coverage:true}`.
- `confidence_below_threshold` at 0.5 and 0.9.
- `merge_ready` over all-true vs one-false payloads.

**Unit — engine (`test_engine.py`):**
- `test_start_creates_run_and_first_transition` (AC 1).
- `test_duplicate_active_run_rejected` (AC 2).
- `test_full_happy_path` — drives all 14 transitions (15 states), asserts ordered `sequence` 1..14 (AC 3).
- `test_spec_gate_blocks_without_approval` (AC 4).
- `test_plan_gate_required_vs_optional` (AC 5).
- `test_execute_preconditions` — each missing flag listed; all-present advances (AC 6).
- `test_retry_backoff_then_escalate` — uses `FakeClock`; asserts `NullEffectDispatcher` delays = [30,60,120] then `needs_human_input` on 4th fail (AC 7).
- `test_checks_passed_requires_all` (AC 8).
- `test_low_confidence_escalation` and `test_policy_conflict_escalation` (AC 9, 10).
- `test_merge_gate` (AC 11).
- `test_resume_returns_to_paused_from` and `test_cancel_fail_from_any_state` (parametrized over several non-terminal states) (AC 12).
- `test_invalid_transition_lists_allowed_events`, `test_terminal_state_rejects_events` (AC 13).
- `test_idempotent_event_replay` (AC 14).
- `test_transition_payload_redacted` and `test_transitions_are_append_only` (assert no UPDATE/DELETE path) (AC 15).
- `test_effects_dispatched_after_commit` — dispatcher not called if the transition raises mid-apply.

**Integration — Postgres + API (`tests/integration/test_workflow_api.py`, Phase 2):**
- `test_migration_upgrade_downgrade` — `alembic upgrade head` then `downgrade -1` clean against the container.
- `test_partial_unique_active_run` — DB rejects a second active run (AC 2 at DB level).
- `test_concurrent_transitions_serialize` — two threads/sessions issue `checks_failed`; assert exactly one applied, `version` incremented once, loser sees new state (AC 19).
- `test_api_start_and_event_roundtrip` — `POST /workflow/runs` → events → `GET .../transitions`; 409 bodies for guard/precondition/invalid; 404 unknown definition; 401 unauth; 403 viewer `send_event` (AC 18).
- `test_celery_backoff_schedules_countdown` — `CeleryEffectDispatcher` in eager mode records `countdown` values [30,60,120].
- `test_bundled_and_example_dsl_match` (AC 20).

---

## 8. Security & policy considerations

- **Authn/authz on every event.** All `/workflow/*` routes require an authenticated principal (no anonymous API). `send_event` is RBAC-gated: `HUMAN_GATE_EVENTS` may only be sent by `member`/`admin` principals (an `agent-runner`/service token cannot approve its own spec/plan/PR or resume a pause — prevents an agent escalating its own scope). `system`/`agent` actor events arrive only via the internal service token used by the worker. The `actor` is recorded verbatim on the transition row.
- **Human gates are structural, not advisory.** `spec_review`, `plan_review`, `awaiting_review` cannot be left except through guard-checked human-approval events; the merge guard requires human approval **and** green CI **and** spec validation — enforcing "Human approval is required before PR merge — always" and the spec-gating rules.
- **Preconditions enforce the non-negotiable.** `task_ready→executing` requires repo target, policy, skill profile, and synced knowledge — implementing "Every task must know its repo target, policy profile, and skill profile BEFORE execution." A run physically cannot execute an agent without them.
- **Deterministic routing.** Guards/effects are named, registered Python predicates resolved at load — the FSM contains **no LLM call** and makes no inference; this satisfies "Supervisor makes routing decisions via explicit policy, not LLM judgement."
- **Immutable audit.** Every transition is an append-only `workflow_transition` row (actor, event, guard results, effects, timestamp) — feeds the immutable, queryable audit log the Security section requires; the engine never updates/deletes these rows. `cross-cutting/F39-audit-log` consumes these rows into the central audit surface and supplies the DB-level immutability trigger (`attach_immutability_trigger`); until then append-only is enforced in `WorkflowRunRepository`.
- **Secret redaction.** Event `payload` is passed through the canonical `forge_auth.redaction.SecretRedactor` (owned by `cross-cutting/F37-auth-secrets-byok`) before being persisted to `workflow_transition.payload`, so tokens/keys never land in the audit trail, traces, or `GET /transitions` output.
- **Idempotency / at-least-once safety.** `idempotency_key` + the row lock prevent duplicate Celery delivery from double-advancing a run. Effects dispatch **after** commit, so a queue failure cannot fabricate a transition; consumers must be idempotent.
- **Tenant isolation.** All reads/writes are scoped by `workspace_id`; the engine derives workspace from the Task and asserts the principal belongs to it (403 cross-workspace).

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: M** (~3–4 focused engineer-days). The state graph, DSL, guards, and engine algorithm are well-specified; the volume is in tests and the migration, not novel design.

Key risks:
- **Transition atomicity & concurrency** (med). Correct `SELECT FOR UPDATE` + post-commit dispatch is subtle; the concurrency integration test (AC 19) is the guard. Mitigation: keep all state mutation in one transaction, dispatch effects only after commit, add the `version` optimistic counter as a second line of defense.
- **Idempotency under Celery at-least-once** (med). Mitigation: unique `(run_id, idempotency_key)` index + early idempotent return; require callers (worker callbacks) to always set a key.
- **DSL faithfulness to the spec's illustrative DSL** (low). The spec's example uses `action`/`when`/`condition`; this slice defines a canonical `on:`/`guards:`/`effects:` form and documents the mapping. Risk is reviewer confusion, not correctness; AC 20 + the mapping table mitigate.
- **Effect-handler coupling** (low, deferred). Effect bodies live in other slices; until wired they "park". Mitigation: `NullEffectDispatcher` for tests; unregistered effects log a parked marker and never fake success.
- **Retry backoff timing** (low). Mitigation: inject `FakeClock`; assert exact delay sequence rather than wall-clock.

---

## 10. Key files / paths (exact)

Create:
- `packages/workflow-engine/forge_workflow/__init__.py`
- `packages/workflow-engine/forge_workflow/states.py`
- `packages/workflow-engine/forge_workflow/dsl.py`
- `packages/workflow-engine/forge_workflow/guards.py`
- `packages/workflow-engine/forge_workflow/effects.py`
- `packages/workflow-engine/forge_workflow/engine.py`
- `packages/workflow-engine/forge_workflow/repository.py`
- `packages/workflow-engine/forge_workflow/errors.py`
- `packages/workflow-engine/forge_workflow/definitions/default_feature.yaml`
- `packages/workflow-engine/tests/conftest.py`
- `packages/workflow-engine/tests/test_dsl.py`
- `packages/workflow-engine/tests/test_guards.py`
- `packages/workflow-engine/tests/test_engine.py`
- `packages/workflow-engine/tests/integration/test_workflow_api.py`
- `packages/db/migrations/versions/<rev>_f07_workflow_fsm.py`
- `apps/worker/forge_worker/tasks/workflow.py`
- `examples/workflows/default_feature.yaml` (community example — authored and owned by this slice; drift-locked to the bundled DSL via AC 20)

Edit (fill stubs / extend):
- `apps/api/forge_api/routers/workflow.py` (replace 501 stubs with real handlers + exception handlers)
- `apps/api/forge_api/deps.py` (add `get_workflow_engine`)
- `packages/db/forge_db/models/workflow.py` (extend `WorkflowRun`; add `WorkflowTransition`)
- `packages/contracts/forge_contracts/workflow.py` (add DTOs; refine `WorkflowEngine` Protocol)
- `packages/workflow-engine/pyproject.toml` (package-data include for `definitions/*.yaml`)

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md → **Workflow Engine** section: state list, Incident states, the YAML DSL, retry_policy, escalation_policy (the authoritative source for §4).
- FORGE_SPEC.md → Technology Stack: "Workflow engine (V1) = Postgres FSM + LangGraph for agent routing — low operational overhead for V1."
- FORGE_SPEC.md → Core Data Model: `WorkflowRun { task_id, current_state, execution_mode, AgentRun[] }`.
- FORGE_SPEC.md → Human Approval System (gate types) and Spec Gating Rules (drives the spec/plan/PR guards).
- forge-research-report.md → "Workflow Engine: LangGraph, Temporal, or Hybrid": top-level durable state in Postgres for V1; LangGraph for agent-level routing; "a lightweight alternative workflow state machine backed by Postgres is a valid V1 approach that avoids Temporal's operational overhead."
- Temporal vs LangGraph (June 2026): https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines
- LangGraph production guide 2026 (ship the smallest useful loop first, eval-first): https://www.reactify-solutions.com/articles/langgraph-production-agents-2026
- Symphony (workflow-files define how work moves through statuses): https://openai.com/index/open-source-codex-orchestration-symphony/
- GitHub Spec Kit (phase-gated SDD that the states mirror): https://github.com/github/spec-kit

## 12. Out of scope / future

- **Temporal migration (V2).** This FSM is the V1 durable layer; V2 replaces the top-level engine with Temporal while keeping the same state vocabulary and guard semantics. Design the engine behind the `WorkflowEngine` Protocol so the swap is contained.
- **Incident workflow** (V2) — `alert_received → … → postmortem_created` is a separate definition loaded by the same DSL parser; not built here.
- **Effect bodies** — drafting specs/plans/tasks (`v1/F02-spec-engine`), running the agent (`v1/F06-single-execution-agent`), running checks + opening PRs (`v1/F08-plan-execute-verify-pr-approval`, which uses `v1/F03-github-app`), sending Slack/email (`v1/F16-slack-notifications`). This slice only dispatches them by name.
- **Approval resolution + UI** — creating/approving `ApprovalRequest` rows and the approval UI are `cross-cutting/F36-human-approval-system` (inbox/review shell) surfaced on the board (`v1/F01-project-board`); this slice only *reads* approval status in guards and *requests* approvals via effects.
- **Workflow visual editor** (V3) — `GET /workflow/definitions/{name}` already exposes the graph data it will consume.
- **Saved workflow automations / rule engine** (V2) — "when status = merged → close linked spec task" lives in the board automations slice, not the FSM.
- **DB-level immutability trigger** for `workflow_transition` — hardening deferred to `cross-cutting/F39-audit-log` (its `attach_immutability_trigger` helper); this slice enforces append-only in the repository layer until then.
