# F21 — Saved Workflow Automations (rule engine)

> Phase: v2 · Spec module(s): Native Project Board → Board Features → **Automations** ("Rule-based: when status = merged → close linked spec task"); Core Data Model (Task, SpecDocument, WorkflowRun); Workflow Engine (state vocabulary, human gates); Security (immutable audit, policy evaluation, RBAC). · Status target: **Done** = a user can save a project/workspace-scoped automation rule of the form **WHEN** `<trigger>` **IF** `<condition>` **THEN** `<actions[]>`; the rule fires deterministically (at-least-once, idempotent per source event) when a matching board/workflow domain event occurs; actions mutate board/workflow state as the `system:automation` actor under RBAC/policy bounds; every firing is recorded in an append-only `automation_execution` audit row; infinite/cascading loops are structurally prevented; the canonical rule "when a task's workflow reaches `merged` → close the tasks linked to the same spec" works end to end; and the whole slice is green under `ruff` + type check + `pytest` (engine unit suite, API integration suite, web component/e2e suite).

---

## 1. Intent — what & why

The board is Forge's task-as-control-plane. Today (v1) every status change, merge, and approval is hand-driven or hard-coded into the workflow FSM. F21 adds the **user-configurable** layer on top: declarative **automation rules** that react to domain events and perform bounded board/workflow mutations — the Linear/Plane "Automations" capability the spec calls out explicitly ("Automations | Rule-based: when status = merged → close linked spec task").

Design intent and constraints:

- **Declarative, not code.** Rules are data (`WHEN trigger IF condition THEN actions`), persisted as rows and expressible/round-trippable as YAML (community artifact under `examples/automations/`), so non-engineers can build them and teams can version them. This mirrors the spec's "Workflow DSL: declarative — custom workflows without code changes" extension point.
- **Deterministic & policy-bounded — never an unsupervised agent.** The rule engine is pure Python predicate evaluation + a fixed action catalog. It makes **no LLM call** and never expands its own scope. Critically, an automation **cannot** perform any human-gated action: it can never approve a spec/plan/PR, merge, or deploy (those remain human-only per "Human approval is required before PR merge — always").
- **Auditable & loop-safe.** Every firing writes an append-only audit row (the Security section's immutable audit log requirement). Cascades (rule A's action triggers rule B's trigger …) are bounded by a propagated depth counter and causation-chain cycle detection, so a misconfigured pair of rules cannot loop forever.
- **Reuse, don't reinvent.** Triggers are derived from the **already-append-only event logs** built in v1 — board `activity_events` (F01) and `workflow_transition` rows (F07) — so no new event-sourcing substrate is invented; conditions reuse the F01 filter-predicate shape; actions call existing board-core/workflow services through injected Protocols.

This slice owns: the `automation_rule` + `automation_execution` data model, the rule/condition/action schemas + validators, the pure `AutomationEngine` (trigger match → condition eval → action plan), the loop guard, the `AutomationDispatcher` wiring from board/workflow event logs, the Celery evaluation + reconciliation-sweep tasks, the concrete `ActionExecutor` (board-core + workflow-engine adapter), the `/automations` API (CRUD + dry-run + catalog + execution history), and the project-settings Automations UI.

---

## 2. User-facing behavior / journeys

1. **Create the canonical rule.** In Project Settings → Automations, user clicks "New automation". **WHEN**: picks `Workflow state changed` → `to = merged`. **IF** (optional): leaves empty (always). **THEN**: adds action `Close tasks linked to the same spec`. Names it "Close spec tasks on merge", clicks **Test** (dry-run against a chosen merged task) to preview which tasks would be closed, then **Save & enable**. The rule appears in the list with an enabled toggle.
2. **Rule fires automatically.** Later, an agent's PR for `CORE-42` is merged; the workflow FSM transitions that task's run to `merged`. Within ~1s the automation evaluates, finds the two other tasks linked to `SPEC-17`, and moves each to the project's default *Done* status as actor `system:automation`. Each closed task's unified timeline (F01) shows a `status_changed` event attributed to the automation, with `automation_rule_id` in the payload.
3. **Field-based automations.** User builds "When priority set to `urgent` AND label `customer-impact` present → assign to on-call team and add comment `@oncall please triage`". Trigger `task_priority_changed`, condition `priority == urgent AND labels contains customer-impact`, actions `set_assignee(team on-call default)` + `add_comment(templated)`.
4. **SLA escalation.** "When SLA breached → set priority `urgent` and add label `sla-breach`." Trigger `task_sla_breached` (emitted by F01's `scan_sla_breaches`).
5. **Condition gate visible.** When a trigger matches but the IF condition fails, nothing mutates; the rule's execution log shows a `conditions_failed` entry with the evaluated snapshot, so the user can debug why a rule "didn't fire".
6. **Disable / edit safely.** Toggling a rule off stops future firings immediately (in-flight evaluations record `skipped_disabled`). Editing a rule bumps its `version`; past executions retain the `rule_version` they ran under for audit fidelity.
7. **Loop protection is observable.** If a user builds two rules that ping-pong a status, the engine stops the cascade at `MAX_DEPTH` and records `skipped_loop` rows naming the causation chain, instead of looping forever. The editor also surfaces a **static warning** at save time when a rule's action can re-trigger its own trigger without a narrowing condition.
8. **Guardrail feedback.** If a user adds a `send_workflow_event` action targeting a human-gate event (e.g. `review_approved`), save fails with `422 action_forbidden_event` and a message explaining automations cannot perform human-gated approvals.
9. **Execution history.** Each rule has an "Activity" tab: a reverse-chronological audit feed of every firing (matched / conditions-failed / succeeded / partial-failure / failed / skipped), with the triggering entity, planned vs. executed actions, per-action result, and latency.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

New SQLAlchemy 2.x models in `packages/db/forge_db/models/automation.py`, plus the trigger/action/status enums appended to `packages/db/forge_db/models/enums.py`. One Alembic migration `packages/db/migrations/versions/<rev>_f21_automations.py` (depends on the F01 board migration and the F07 workflow migration). Models inherit the existing `WorkspaceScopedModel` (UUID PK + timestamps + `workspace_id`) and use `json_type()` / `enum_type()` from `forge_db/base.py` for cross-dialect JSON + VARCHAR-backed enums (SQLite unit tests, Postgres prod).

| Table | Key columns | Notes |
|---|---|---|
| `automation_rule` | `id` (UUID PK), `workspace_id` (FK→workspace CASCADE), `project_id` (UUID FK→project **NULLABLE**, CASCADE; `NULL` = workspace-wide), `name` (text), `description` (text null), `enabled` (bool default `true`), `trigger_type` (`enum_type(AutomationTriggerType)`), `trigger_config` (`json_type()` default `{}`), `condition` (`json_type()` default `{}` — a `ConditionGroup` tree; empty = always true), `actions` (`json_type()` default `[]` — ordered `ActionSpec[]`), `run_order` (int default `100`; lower runs first among rules on the same trigger), `created_by` (UUID FK→user SET NULL), `version` (int default `1`), `created_at`/`updated_at` | Indexes: `ix_automation_rule_workspace_id` (mixin), composite **partial** `ix_automation_rule_dispatch (workspace_id, project_id, trigger_type) WHERE enabled` for fast trigger lookup. `ck_automation_rule_actions_nonempty` (`actions` length ≥ 1, enforced in service + JSON-shape check). |
| `automation_execution` | `id` (UUID PK), `workspace_id` (FK→workspace CASCADE), `rule_id` (UUID FK→automation_rule **SET NULL** on delete — audit survives rule deletion), `rule_version` (int — snapshot of the rule version that ran), `trigger_type` (`enum_type(AutomationTriggerType)`), `trigger_event_id` (UUID — the source `activity_events.id` or `workflow_transition.id`), `trigger_source` (`enum_type(AutomationTriggerSource)`: `board_activity`/`workflow_transition`), `entity_type` (`enum_type(AutomationEntityType)`: `task`/`epic`/`incident`), `entity_id` (UUID), `status` (`enum_type(AutomationExecutionStatus)`), `condition_result` (bool null), `actions_planned` (`json_type()` default `[]`), `action_results` (`json_type()` default `[]` — per-action `{type, status, detail}`), `depth` (int — causation depth at which this fired), `causation_chain` (`json_type()` default `[]` — list of upstream rule ids), `error` (text null), `latency_ms` (int null), `idempotency_key` (text), `created_at` | **Append-only** (no UPDATE/DELETE in the repository; hardened DB-side via `attach_immutability_trigger(automation_execution)` from `cross-cutting/F39-audit-log`, the same opt-in pattern F01/F06/F07 adopt for their per-domain append-only tables). Unique: `uq_automation_execution_idempotency_key` on `(idempotency_key)` where `idempotency_key = "{rule_id}:{trigger_event_id}"` — guarantees **a rule fires at most once per source event** even under Celery at-least-once redelivery. Indexes: `ix_automation_execution_rule_id_created` (history view), `ix_automation_execution_entity (entity_type, entity_id, created_at)` (timeline join). |

No changes to existing tables. The slice **reads** `task.spec_id`, `task.status_id`, `task_statuses.category` (F01), and `workflow_transition.to_state` / `workflow_run.task_id` (F07). The `task` mutations are performed through board-core services, not raw writes.

Migration is reversible (`downgrade` drops both tables + enums). Unit tests run the models on SQLite (`JSON` variant); the partial unique/partial index behavior is asserted against the Postgres test container in the integration suite.

### 3.2 Backend (FastAPI routes + services/packages)

Two backend units:

**(A) Pure engine package — `packages/automation-engine/forge_automation/`** (no FastAPI, no Celery imports; depends only on `forge_contracts`, `forge_db` models/enums, and `pydantic`). Mirrors the `forge_workflow` decoupling pattern (engine is pure; side effects go through injected Protocols).

```
packages/automation-engine/forge_automation/
├── __init__.py
├── enums.py            # AutomationTriggerType, AutomationActionType, AutomationExecutionStatus,
│                       #   AutomationTriggerSource, AutomationEntityType, ConditionOp
├── schemas.py          # Pydantic v2: TriggerSpec, ConditionGroup/Condition, ActionSpec (discriminated),
│                       #   AutomationRuleSpec, AutomationTriggerEnvelope, EntitySnapshot, ActionResult
├── conditions.py       # evaluate_condition(group, snapshot) -> bool ; FIELD_RESOLVERS whitelist
├── triggers.py         # trigger_type_for(event) mapping; trigger_matches(spec, envelope) gate
├── validators.py       # validate_rule(spec, ctx) -> list[RuleWarning]; raises RuleValidationError
├── engine.py           # AutomationEngine.evaluate(envelope, rules, executor) -> list[ExecutionResult]
├── loop_guard.py       # LoopGuard: MAX_DEPTH, causation-chain cycle detection
├── executor.py         # ActionExecutor Protocol + ActionContext + RecordingActionExecutor (test double)
└── errors.py           # RuleValidationError, ActionForbiddenError, UnknownTriggerError, LoopAbortedError
```

The **engine is side-effect-free except via `ActionExecutor`**: `AutomationEngine.evaluate(envelope, rules, executor)` selects matching enabled rules (ordered by `run_order`), runs the loop guard, evaluates trigger config + condition, plans the `ActionSpec[]`, and calls `executor.execute(action, ctx)` for each. It returns `ExecutionResult[]` (one per evaluated rule) for the caller to persist. The engine never opens a DB session or enqueues anything itself.

**(B) API + persistence — `apps/api/forge_api/`**:

- `forge_api/routers/automations.py` (mounted under `/api/v1`, all routes auth-required, `workspace_id` resolved from path/principal, RBAC per route):

| Method & path | Handler | RBAC | Returns |
|---|---|---|---|
| `POST /projects/{project_id}/automations` | `create_rule(body: AutomationRuleCreate)` | member+ | `AutomationRuleRead` 201 (+ `warnings[]`) |
| `GET /projects/{project_id}/automations` | `list_rules` | viewer+ | `list[AutomationRuleRead]` |
| `GET /automations/{rule_id}` | `get_rule` | viewer+ | `AutomationRuleRead` |
| `PATCH /automations/{rule_id}` | `update_rule(body: AutomationRuleUpdate)` | member+ | `AutomationRuleRead` (409 on stale `version`) |
| `POST /automations/{rule_id}/enable` / `.../disable` | `set_enabled` | member+ | `AutomationRuleRead` |
| `DELETE /automations/{rule_id}` | `delete_rule` | admin+ | 204 (executions retained via `rule_id` SET NULL) |
| `POST /automations/{rule_id}/test` | `dry_run(body: DryRunRequest)` | member+ | `DryRunResult` (condition result + **planned** actions, **no** execution) |
| `GET /automations/{rule_id}/executions` | `executions(cursor, limit)` | viewer+ | `Page[AutomationExecutionRead]` |
| `GET /automations/catalog` | `catalog` | viewer+ | `AutomationCatalog` (trigger types, per-trigger config schema, condition fields, action types + arg schemas — drives the UI builder) |

  Exception handlers map `RuleValidationError`→422 (`{error, issues[]}`), `ActionForbiddenError`→422 (`{error:"action_forbidden_event", ...}`), version conflict→409 (`{error:"version_conflict", current_version}`), cross-workspace→404.

- `forge_api/services/automations.py` — persistence/service layer (takes `AsyncSession` + `Principal`): rule CRUD with validation (`forge_automation.validators.validate_rule`), reference checks (status_ids/label_ids/team/spec belong to project), enable/disable, dry-run (builds an `EntitySnapshot` from a real task id, runs `AutomationEngine.evaluate` with a `RecordingActionExecutor` so nothing mutates), and execution-history reads.

- `forge_api/services/action_executor.py` — the **concrete** `ActionExecutor` implementing the engine Protocol by delegating to **board-core services** (`board_core.services.tasks` for status/priority/assignee/label, `board_core.services.timeline` for comment posting, `board_core.services.statuses` for category→default-status resolution) and the **workflow engine** (`forge_workflow.engine.WorkflowEngine.transition` for `send_workflow_event`, **rejecting `HUMAN_GATE_EVENTS`**), and the notifications port (`v1/F16-slack-notifications`) for `send_notification`. All mutations pass `actor=ActorRef(kind="system", label=f"system:automation:{rule_id}")` and propagate `automation_depth = ctx.depth + 1` + `causation_chain` into emitted events' payloads (so downstream triggers carry the loop metadata). Each persisted `automation_execution` additionally fans out a redacted `AuditEvent` through `cross-cutting/F39-audit-log`'s `AuditSink` with a `detail_ref={table:"automation_execution", id:<row id>}` so automation firings appear in the central immutable audit log (board-core's own writes already fan out their per-mutation `AuditEvent`s).

**Trigger wiring (event log → engine).** `forge_contracts` gains an `AutomationDispatcher` Protocol + `AutomationTriggerEnvelope` DTO (so board-core/workflow-engine depend on contracts, **not** on automation-engine). After a relevant domain event is committed:
- F01 `board_core.events.publish(...)` (already runs post-commit for SSE) additionally calls `dispatcher.dispatch(envelope)` for board `activity_events` whose `event_type` maps to a trigger.
- F07 engine, after committing a `workflow_transition`, calls `dispatcher.dispatch(envelope)` for state changes (carries `to_state`).
The concrete `CeleryAutomationDispatcher` enqueues `forge_worker.tasks.automations.evaluate_trigger(envelope)`. In unit tests, a `NullAutomationDispatcher` records calls. The envelope carries `trigger_event_id`, `trigger_source`, `entity_type`/`entity_id`, the change payload (e.g. `from_status_id`/`to_status_id`/`to_state`), `workspace_id`/`project_id`, and the inbound `depth`/`causation_chain` (0/[] for human/agent-originated events).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

`apps/worker/forge_worker/tasks/automations.py` (reuses the v1 Celery app + Redis broker). **No LangGraph** — the rule engine is deterministic and contains no agent loop.

- `evaluate_trigger(envelope: dict)` — the core consumer. In one DB session: (1) load enabled rules matching `(workspace_id, project_id|NULL, trigger_type)` via the partial dispatch index; (2) build the `EntitySnapshot` (current task fields + labels + status category + spec link); (3) call `AutomationEngine.evaluate(envelope, rules, executor=ActionExecutor(session, principal=SystemPrincipal))`; (4) persist one `automation_execution` per evaluated rule. Idempotency: each execution insert uses `idempotency_key = "{rule_id}:{trigger_event_id}"`; a `unique_violation` is caught and the rule is skipped (already processed) — this makes redelivery safe. Action-induced events are dispatched **post-commit** through the same `CeleryAutomationDispatcher`, with incremented `depth`.
- `sweep_unprocessed_triggers()` — Celery Beat, every 60s. Durability backstop for any `dispatch` enqueue lost between commit and broker (the post-commit enqueue is best-effort). Scans `activity_events` + `workflow_transition` created in the last 15 min whose event type is trigger-eligible and that have **no** `automation_execution` row for any currently-enabled matching rule, and re-dispatches them. Re-dispatch is safe because the `(rule_id, trigger_event_id)` unique key dedupes. This gives at-least-once delivery with bounded latency without inventing a new outbox table.

Loop/cascade control lives in `forge_automation.loop_guard.LoopGuard`: an evaluation is aborted (status `skipped_loop`) when `envelope.depth >= MAX_DEPTH` (default 5, configurable via `FORGE_AUTOMATION_MAX_DEPTH`) **or** the firing rule id is already present in `envelope.causation_chain` for the same `entity_id` (direct/indirect self-cycle). Action-emitted events carry `depth+1` and `causation_chain + [rule_id]`.

### 3.4 Frontend / UI (Next.js routes/components, if any)

**App:** `apps/web` (App Router, TS, Tailwind, shadcn/ui, TanStack Query). Shared inputs reuse `packages/ui-kit` and the F01 board filter primitives.

Routes:
- `app/[workspace]/projects/[projectKey]/settings/automations/page.tsx` — rule list with enable/disable toggles, run-order, "New automation" CTA, and an empty state ("Automate repetitive board actions — create your first rule").
- `app/[workspace]/projects/[projectKey]/settings/automations/[ruleId]/page.tsx` — rule editor + "Activity" (execution log) tab.

Components (`apps/web/components/automations/`):
- `AutomationRuleList.tsx`, `AutomationRuleRow.tsx` (toggle, last-fired summary).
- `AutomationRuleEditor.tsx` — the **WHEN / IF / THEN** builder driven by `GET /automations/catalog`:
  - `TriggerPicker.tsx` (+ per-trigger `TriggerConfigForm.tsx`, e.g. status/category picker, `to_state` picker for workflow).
  - `ConditionBuilder.tsx` — reuses the F01 `FilterBar` predicate primitives (all/any groups, whitelisted fields).
  - `ActionListEditor.tsx` + `ActionForm.tsx` per `AutomationActionType` (status picker, assignee/team picker, label picker, comment template editor with `{{task.key}}` token help, "close linked spec tasks" config).
- `DryRunPanel.tsx` — task picker + "Test" → renders condition result + planned actions from `POST /test`.
- `AutomationExecutionLog.tsx` — virtualized audit feed (status chip, entity link, planned vs executed actions, latency), cursor-paginated.
- `StaticWarningBanner.tsx` — shows save-time loop/reference warnings returned by the API.

Data: `apps/web/lib/automations/{api,queries,mutations,types}.ts` (typed client over §4 contracts; optimistic enable/disable toggle with rollback). No SSE needed for the editor; the F01 board stream already surfaces automation-driven status changes on task views.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new services. Reuses v1 `db` (Postgres+pgvector), `redis` (Celery broker + dispatch), `api`, `worker`, `web`. Changes:
- `worker` must run **Celery Beat** so `sweep_unprocessed_triggers` fires (the F01 board slice already requires Beat for `scan_sla_breaches`; this slice adds one more beat entry — no new process).
- Bundle `examples/automations/*.yaml` (community artifacts) in the repo; a test asserts each example parses to a valid `AutomationRuleSpec` with the same validator the API uses.
- Migration `<rev>_f21_automations` runs via the existing `forge-cli db migrate`.
- New env knob `FORGE_AUTOMATION_MAX_DEPTH` (default 5) documented in `.env.example` / `deploy/.env.production.example`.
- Helm (v2): no new chart resources; the new beat schedule entry ships with the existing `worker` deployment.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Enums** (`forge_automation/enums.py`; the DB-persisted ones are mirrored verbatim into `forge_db/models/enums.py`):
```python
from enum import StrEnum

class AutomationTriggerType(StrEnum):
    TASK_CREATED = "task_created"
    TASK_STATUS_CHANGED = "task_status_changed"
    TASK_ASSIGNED = "task_assigned"
    TASK_PRIORITY_CHANGED = "task_priority_changed"
    TASK_LABEL_ADDED = "task_label_added"
    TASK_LABEL_REMOVED = "task_label_removed"
    TASK_SLA_BREACHED = "task_sla_breached"
    WORKFLOW_STATE_CHANGED = "workflow_state_changed"   # config: {to_state: "merged"} etc.
    PR_MERGED = "pr_merged"                              # from F03 pr_event (action=merged)
    APPROVAL_RESOLVED = "approval_resolved"             # from approval_event

class AutomationActionType(StrEnum):
    SET_STATUS = "set_status"
    SET_PRIORITY = "set_priority"
    SET_ASSIGNEE = "set_assignee"
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    ADD_COMMENT = "add_comment"
    SET_FIELD = "set_field"                              # sprint_id|milestone_id|due_date|estimate
    CLOSE_LINKED_SPEC_TASKS = "close_linked_spec_tasks"
    SEND_WORKFLOW_EVENT = "send_workflow_event"          # non-human-gate events ONLY
    SEND_NOTIFICATION = "send_notification"              # F16 Slack/email notifications port
    CREATE_TASK = "create_task"                          # templated, minimal

class AutomationExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    CONDITIONS_FAILED = "conditions_failed"
    NO_OP = "no_op"                # matched + condition true but action had nothing to do
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"
    SKIPPED_LOOP = "skipped_loop"
    SKIPPED_DISABLED = "skipped_disabled"

class AutomationTriggerSource(StrEnum):
    BOARD_ACTIVITY = "board_activity"          # source = activity_events row
    WORKFLOW_TRANSITION = "workflow_transition"

class AutomationEntityType(StrEnum):
    TASK = "task"; EPIC = "epic"; INCIDENT = "incident"

class ConditionOp(StrEnum):
    EQ="eq"; NE="ne"; IN="in"; NOT_IN="not_in"; LT="lt"; LTE="lte"; GT="gt"; GTE="gte"
    CONTAINS="contains"; NOT_CONTAINS="not_contains"; IS_NULL="is_null"; IS_NOT_NULL="is_not_null"; CHANGED="changed"
```

**Condition DSL + evaluator** (`forge_automation/schemas.py` + `conditions.py`) — same *shape* as the F01 filter predicate but evaluated **in-memory** against an `EntitySnapshot` (the engine never builds SQL):
```python
class Condition(BaseModel):
    field: str                # must be in CONDITION_FIELDS (whitelist)
    op: ConditionOp
    value: Any = None

class ConditionGroup(BaseModel):
    match: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(default_factory=list)
    groups: list["ConditionGroup"] = Field(default_factory=list)
    # empty group (no conditions, no groups) == always True

class EntitySnapshot(BaseModel):
    entity_type: AutomationEntityType
    entity_id: UUID
    fields: dict[str, Any]              # status_id, status_category, priority, assignee_id,
                                        #   kind, epic_id, sprint_id, milestone_id, team_id,
                                        #   estimate, due_date, spec_id, labels: list[UUID-or-name]
    change: dict[str, Any] = {}         # trigger-local context: from_status_id, to_status_id,
                                        #   from_status_category, to_status_category, to_state,
                                        #   from_priority, to_priority, label_id, approval_kind, approval_status

# Whitelisted condition fields -> resolver against snapshot.fields / snapshot.change.
CONDITION_FIELDS: frozenset[str] = frozenset({
    "status_id","status_category","priority","assignee_id","kind","epic_id","sprint_id",
    "milestone_id","team_id","estimate","due_date","spec_id","labels","has_spec",
    "to_status_category","to_status_id","from_status_id","to_state","approval_kind","approval_status",
})

def evaluate_condition(group: ConditionGroup, snapshot: EntitySnapshot) -> bool:
    """Pure boolean eval. 'changed' op uses snapshot.change.* presence.
    Resolves the special assignee value '@trigger_actor'. Raises ValueError on
    a field not in CONDITION_FIELDS or an op/value mismatch (e.g. IN without a list)."""
```

**Action specs** (discriminated union on `type`):
```python
class _ActionBase(BaseModel):
    type: AutomationActionType

class SetStatusAction(_ActionBase):
    type: Literal[AutomationActionType.SET_STATUS]
    status_id: UUID | None = None          # exactly one of status_id / status_category
    status_category: StatusCategory | None = None   # resolves to the project's default status in that category

class SetPriorityAction(_ActionBase):
    type: Literal[AutomationActionType.SET_PRIORITY]; priority: Priority

class SetAssigneeAction(_ActionBase):
    type: Literal[AutomationActionType.SET_ASSIGNEE]
    assignee_id: UUID | None = None        # None = unassign
    team_default: bool = False             # assign to team's default/on-call assignee

class LabelAction(_ActionBase):
    type: Literal[AutomationActionType.ADD_LABEL, AutomationActionType.REMOVE_LABEL]
    label_id: UUID

class AddCommentAction(_ActionBase):
    type: Literal[AutomationActionType.ADD_COMMENT]
    body_template: str = Field(max_length=4000)   # supports {{task.key}}, {{task.title}}, {{rule.name}}

class SetFieldAction(_ActionBase):
    type: Literal[AutomationActionType.SET_FIELD]
    field: Literal["sprint_id","milestone_id","due_date","estimate"]; value: Any

class CloseLinkedSpecTasksAction(_ActionBase):
    type: Literal[AutomationActionType.CLOSE_LINKED_SPEC_TASKS]
    scope: Literal["project","workspace"] = "project"
    target_status_category: StatusCategory = StatusCategory.COMPLETED
    exclude_trigger_task: bool = True

class SendWorkflowEventAction(_ActionBase):
    type: Literal[AutomationActionType.SEND_WORKFLOW_EVENT]
    event: WorkflowEventType            # validator REJECTS HUMAN_GATE_EVENTS at save time

class SendNotificationAction(_ActionBase):
    type: Literal[AutomationActionType.SEND_NOTIFICATION]
    channel: Literal["slack","email"]; target: str; message_template: str

class CreateTaskAction(_ActionBase):
    type: Literal[AutomationActionType.CREATE_TASK]
    title_template: str; kind: TaskKind = TaskKind.CHORE; copy_links: bool = False

ActionSpec = Annotated[
    SetStatusAction | SetPriorityAction | SetAssigneeAction | LabelAction | AddCommentAction
    | SetFieldAction | CloseLinkedSpecTasksAction | SendWorkflowEventAction
    | SendNotificationAction | CreateTaskAction,
    Field(discriminator="type"),
]

class TriggerSpec(BaseModel):
    type: AutomationTriggerType
    config: dict[str, Any] = {}          # validated per-type by validate_rule (e.g. WORKFLOW_STATE_CHANGED requires to_state)

class AutomationRuleSpec(BaseModel):     # the YAML/JSON-portable rule body
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    enabled: bool = True
    trigger: TriggerSpec
    condition: ConditionGroup = ConditionGroup()
    actions: list[ActionSpec] = Field(min_length=1)
    run_order: int = 100
```

**Trigger envelope + dispatcher Protocol** (in `forge_contracts/automation.py` so board/workflow can depend without importing the engine):
```python
class AutomationTriggerEnvelope(BaseModel):
    trigger_type: AutomationTriggerType
    trigger_source: AutomationTriggerSource
    trigger_event_id: UUID               # activity_events.id | workflow_transition.id
    workspace_id: UUID
    project_id: UUID | None
    entity_type: AutomationEntityType
    entity_id: UUID
    change: dict[str, Any] = {}          # to_status_id/category, to_state, label_id, approval_*, actor_ref
    depth: int = 0
    causation_chain: list[UUID] = []     # upstream rule ids (loop detection)

class AutomationDispatcher(Protocol):
    def dispatch(self, envelope: AutomationTriggerEnvelope) -> None: ...

class NullAutomationDispatcher:          # test double: records envelopes, no enqueue
    dispatched: list[AutomationTriggerEnvelope]
```

**Action executor Protocol** (`forge_automation/executor.py`):
```python
@dataclass
class ActionContext:
    rule_id: UUID
    rule_name: str
    snapshot: EntitySnapshot
    envelope: AutomationTriggerEnvelope
    depth: int                  # = envelope.depth (children emitted at depth+1)
    causation_chain: list[UUID]

class ActionExecutor(Protocol):
    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult: ...

class ActionResult(BaseModel):
    type: AutomationActionType
    status: Literal["ok","no_op","error","forbidden"]
    detail: dict[str, Any] = {}          # e.g. {"closed_task_ids":[...]} or {"error":"status_transition_forbidden"}

class RecordingActionExecutor:           # dry-run / test double: plans, never mutates
    planned: list[ActionSpec]
    def execute(self, action, ctx) -> ActionResult: ...   # always status="ok" (simulated)
```

**Engine entrypoint** (`forge_automation/engine.py`):
```python
@dataclass
class ExecutionResult:
    rule_id: UUID
    rule_version: int
    status: AutomationExecutionStatus
    condition_result: bool | None
    actions_planned: list[ActionSpec]
    action_results: list[ActionResult]
    depth: int
    causation_chain: list[UUID]
    error: str | None = None

class AutomationEngine:
    def __init__(self, loop_guard: LoopGuard) -> None: ...
    def evaluate(
        self,
        envelope: AutomationTriggerEnvelope,
        rules: list[AutomationRuleSpecWithMeta],   # spec + (id, version, enabled, run_order)
        executor: ActionExecutor,
        snapshot: EntitySnapshot,
    ) -> list[ExecutionResult]:
        """For each enabled rule (run_order asc): loop-guard -> trigger_matches(config) ->
        evaluate_condition -> plan actions -> executor.execute each. Returns one
        ExecutionResult per evaluated rule. Pure: all side effects go through `executor`;
        all persistence is done by the caller from the returned results."""
```

**Validation** (`forge_automation/validators.py`):
```python
class RuleValidationError(Exception):
    def __init__(self, issues: list[dict[str, str]]) -> None: ...   # [{"path","code","message"}]

@dataclass
class RuleWarning:
    code: str            # e.g. "possible_self_trigger", "broad_workspace_scope"
    message: str

def validate_rule(spec: AutomationRuleSpec, ctx: RuleRefContext) -> list[RuleWarning]:
    """Hard-fails (raises RuleValidationError) on: trigger.config missing required keys for the
    trigger type; SEND_WORKFLOW_EVENT.event in HUMAN_GATE_EVENTS (-> ActionForbiddenError); a
    SET_STATUS with neither/both of status_id|status_category; condition field not in
    CONDITION_FIELDS; referenced status_id/label_id/team_id/sprint_id not in the project.
    Returns non-fatal warnings, notably 'possible_self_trigger' when an action's effect can
    re-fire the rule's own trigger with no narrowing condition."""
```

**Representative API models** (`apps/api/forge_api/schemas/automations.py`):
```python
class AutomationRuleCreate(AutomationRuleSpec): ...        # project_id from path
class AutomationRuleUpdate(BaseModel):                     # partial; version required
    name: str | None = None; description: str | None = None; enabled: bool | None = None
    trigger: TriggerSpec | None = None; condition: ConditionGroup | None = None
    actions: list[ActionSpec] | None = None; run_order: int | None = None
    version: int

class AutomationRuleRead(AutomationRuleSpec):
    id: UUID; project_id: UUID | None; workspace_id: UUID
    version: int; created_by: UUID | None; created_at: datetime; updated_at: datetime
    warnings: list[RuleWarning] = []
    model_config = ConfigDict(from_attributes=True)

class DryRunRequest(BaseModel):
    task_id: UUID                          # build snapshot from a real task
    change: dict[str, Any] = {}            # optional simulated trigger context (e.g. to_state=merged)

class DryRunResult(BaseModel):
    trigger_matched: bool; condition_result: bool
    planned_actions: list[ActionSpec]; notes: list[str] = []

class AutomationExecutionRead(BaseModel):
    id: UUID; rule_id: UUID | None; rule_version: int
    trigger_type: AutomationTriggerType; entity_type: AutomationEntityType; entity_id: UUID
    status: AutomationExecutionStatus; condition_result: bool | None
    actions_planned: list[dict]; action_results: list[ActionResult]
    depth: int; causation_chain: list[UUID]; error: str | None
    latency_ms: int | None; created_at: datetime
    model_config = ConfigDict(from_attributes=True)
```

**YAML rule schema (community artifact `examples/automations/close-spec-tasks-on-merge.yaml`)** — parses to `AutomationRuleSpec`:
```yaml
name: Close spec tasks on merge
description: When a task's workflow reaches merged, close sibling tasks linked to the same spec.
enabled: true
trigger:
  type: workflow_state_changed
  config:
    to_state: merged
condition:
  match: all
  conditions:
    - { field: has_spec, op: eq, value: true }
actions:
  - type: close_linked_spec_tasks
    scope: project
    target_status_category: completed
    exclude_trigger_task: true
run_order: 100
```

---

## 5. Dependencies — features/slices that must exist first

Hard (build/runtime) dependencies:
- `cross-cutting/F00-foundation` — `forge_db` (`Base`, `WorkspaceScopedModel`, `TimestampMixin`, `json_type`/`enum_type`, Alembic env + naming convention, `forge-cli db migrate`), `forge_contracts` package, `apps/api` (`forge_api`) FastAPI skeleton + session DI, `apps/worker` (`forge_worker`) Celery app + Redis broker + Celery Beat, `apps/web` Next.js shell. (This cross-cutting Phase-0 substrate has no dedicated numbered feature file yet; sibling slices spell it variously as `cross-cutting/F00-foundation` / `cross-cutting/F00-platform-foundation` / `v1/F00-foundation-substrate` — reconcile to the final foundation slug when it lands.)
- `cross-cutting/F37-auth-secrets-byok` — `Principal` (workspace_id, user_id, role ∈ {admin, member, viewer, agent-runner}), `get_principal` + `require_role(...)` dependency, and a `SystemPrincipal` (internal service token) for automation-actor service calls; also `forge_auth.redaction.SecretRedactor` used to scrub envelopes / `action_results` before persistence.
- `cross-cutting/F39-audit-log` — the canonical `AuditEvent` DTO + `AuditSink` Protocol (in `packages/contracts`) that automation firings + rule mutations fan out to, and the reusable `attach_immutability_trigger(automation_execution)` helper that gives the audit table DB-level UPDATE/DELETE protection (the same pattern F01/F06/F07/F09 adopt). Lands-with F21.
- `v1/F01-project-board` — `task`/`task_statuses`/`labels`/`activity_events` tables; board-core services (`board_core.services.tasks`, `statuses`, `timeline`) the `ActionExecutor` calls; the `board_core.events.publish` post-commit hook the dispatcher attaches to; the filter-predicate UI primitives (`FilterBar`) the `ConditionBuilder` reuses; `StatusCategory`, `Priority`, `TaskKind` enums.
- `v1/F07-feature-workflow-fsm` — `workflow_run`/`workflow_transition`; `forge_workflow.engine.WorkflowEngine.transition` (target of `send_workflow_event`); `WorkflowState`, `WorkflowEventType`, **`HUMAN_GATE_EVENTS`** (the forbidden-action allow-list); the post-commit hook in the FSM engine that emits the `workflow_state_changed` envelope.

Soft (degrade-gracefully) dependencies — the slice ships these action/trigger paths but they activate when the producing slice lands:
- `v1/F02-spec-engine` — `task.spec_id` linkage that `close_linked_spec_tasks` resolves (without it the action records `no_op:no_spec_link`).
- `v1/F03-github-app` — emits the `pr_event(action=merged)` activity event behind the `PR_MERGED` trigger.
- `cross-cutting/F36-human-approval-system` — source of the `approval_event` (with `approval_kind`/`approval_status`) behind the `APPROVAL_RESOLVED` trigger and the matching condition fields (absent → that trigger type is simply never produced; it remains a valid catalog entry). Note this trigger fires on approval *resolution*, never letting an automation *perform* an approval — that path stays blocked by the `HUMAN_GATE_EVENTS` guard.
- `v1/F16-slack-notifications` — the Slack/email notifications port behind `SEND_NOTIFICATION` (absent → action records `error:notifications_unavailable`, never fakes success).
- `v1/F10-run-trace-viewer` — surfaces `automation_execution` rows alongside run traces (read-only consumer; not required to build F21).

---

## 6. Acceptance criteria (numbered, testable)

1. **Create + validate.** `POST /projects/{id}/automations` with a valid `AutomationRuleSpec` returns 201 + `AutomationRuleRead` (`version=1`, `enabled=true`) and persists one `automation_rule` row; an empty `actions` list returns 422.
2. **Reference + shape validation.** Creating a rule whose `SET_STATUS.status_id` (or any referenced `label_id`/`team_id`) does not belong to the project returns 422 `RuleValidationError`; a `SET_STATUS` with both `status_id` and `status_category` (or neither) returns 422.
3. **Human-gate guard.** Creating/patching a rule with a `SEND_WORKFLOW_EVENT` whose `event ∈ HUMAN_GATE_EVENTS` (e.g. `review_approved`, `spec_approved`) returns 422 `action_forbidden_event`; a non-gate event (e.g. `close`) is accepted.
4. **Canonical rule fires.** Given the example rule and a task `T` linked to `SPEC-17` with two sibling tasks also linked to `SPEC-17`, dispatching a `workflow_state_changed{to_state:merged}` envelope for `T` evaluates the rule, moves both siblings (not `T`, since `exclude_trigger_task`) to the project's default `completed` status via board-core, and writes one `automation_execution(status=succeeded)` whose `action_results[0].detail.closed_task_ids` lists exactly those two ids.
5. **Condition gate.** With condition `has_spec == true`, a `merged` envelope for a task with `spec_id IS NULL` yields `automation_execution(status=conditions_failed)` and **zero** task mutations.
6. **Trigger config match.** A `WORKFLOW_STATE_CHANGED{to_state:merged}` rule does **not** fire on a `to_state=verifying` envelope (no execution row for that rule); it fires on `to_state=merged`.
7. **Idempotency / at-least-once.** Dispatching the *same* envelope (same `trigger_event_id`) twice produces exactly **one** `automation_execution` per rule (the second insert hits the `(rule_id, trigger_event_id)` unique key and is skipped) and the side effects are applied once.
8. **Loop guard — depth.** Two rules that each set status (A→done triggers B→inprogress triggers A→done …) on the same task halt: once `depth >= MAX_DEPTH`, the next evaluation records `skipped_loop` and performs no mutation; total executions are bounded by `MAX_DEPTH`.
9. **Loop guard — self cycle.** A rule whose own action re-satisfies its own trigger on the same entity within one causation chain is aborted with `skipped_loop` (rule id already in `causation_chain`) rather than firing a second time.
10. **Disabled rules.** A `disabled` rule is never selected by `evaluate_trigger` (no execution row); enabling it via `POST .../enable` makes subsequent matching events fire it.
11. **Optimistic concurrency.** Two `PATCH /automations/{id}` with the same stale `version` → first 200, second 409 `version_conflict` (with `current_version`); a successful patch bumps `version`.
12. **Dry-run is side-effect-free.** `POST /automations/{id}/test` returns `DryRunResult` with `trigger_matched`, `condition_result`, and `planned_actions`, and creates **no** `automation_execution` row and performs **no** task mutation (verified by snapshotting task state before/after).
13. **Audit completeness.** Every firing (succeeded / conditions_failed / partial_failure / failed / skipped_loop) writes an append-only `automation_execution` row with `rule_version`, `trigger_event_id`, `depth`, `causation_chain`, planned vs executed actions, and `latency_ms`; rows are never updated/deleted by the service; on Postgres a direct UPDATE/DELETE against `automation_execution` raises (F39 immutability trigger); and each **state-mutating** firing emits a redacted canonical `AuditEvent` through the F39 `AuditSink` with a `detail_ref` pointing back to the execution row.
14. **Actor + provenance.** Tasks mutated by an automation emit board `activity_events` with `actor_kind=system` and `payload.automation_rule_id == rule.id` and `payload.automation_depth == depth+1`; the task timeline (F01) shows the change attributed to the automation.
15. **Partial failure isolation.** If one action in a rule fails (e.g. a `set_status` blocked by the project's transition policy) while others succeed, the execution is `partial_failure`, the failing `ActionResult.status=error` carries the reason, and the succeeding actions still applied.
16. **RBAC.** `viewer` gets 403 on create/patch/enable/disable/delete and 200 on list/get/executions; `member` can create/edit/enable/disable; only `admin` can `DELETE`; cross-workspace rule access returns 404.
17. **Delete preserves audit.** `DELETE /automations/{id}` removes the rule but retains its `automation_execution` rows with `rule_id` set NULL and the original `rule_version` intact.
18. **Sweeper backstop.** Given a trigger-eligible `activity_events`/`workflow_transition` row with no matching execution (simulating a lost enqueue), invoking `sweep_unprocessed_triggers` re-dispatches it and the rule then fires exactly once (idempotent with any later redelivery).
19. **Catalog drives UI.** `GET /automations/catalog` returns every `AutomationTriggerType` with its required config keys, the `CONDITION_FIELDS` list, and every `AutomationActionType` with its arg schema — sufficient for the editor to render WHEN/IF/THEN without hard-coded field lists.
20. **YAML round-trip.** Each `examples/automations/*.yaml` parses to a valid `AutomationRuleSpec` under the same `validate_rule` the API uses (drift guard); the canonical example matches the rule in AC4.
21. **Frontend optimistic toggle + builder.** In the Automations settings page, toggling a rule's enabled switch updates instantly and rolls back on a server error (toast); the editor's "Test" button renders the dry-run result; building a `SEND_WORKFLOW_EVENT` with a human-gate event surfaces the 422 message inline (component/e2e tests).

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Framework: `pytest` + `pytest-asyncio`. Engine unit tests use **SQLite in-memory** (JSON variant) + `RecordingActionExecutor`/`NullAutomationDispatcher` + a `FakeClock`; integration tests use the **Postgres test container** + Celery eager mode. Write tests first; no module is done until `ruff check`, type check, and `pytest` for `packages/automation-engine` + the API automations suite are green.

**Engine unit (`packages/automation-engine/tests/`, no DB):**
- `test_conditions.py`: each `ConditionOp` evaluates correctly against an `EntitySnapshot`; `all`/`any` nesting; `changed` op via `snapshot.change`; `@trigger_actor`; unknown field / op-value mismatch raises `ValueError` (AC5, AC19).
- `test_triggers.py`: `trigger_type_for(event)` maps board `activity_events` event types + `workflow_transition` to the right `AutomationTriggerType`; `trigger_matches` honors `to_state`/`to_status_category` config (AC6).
- `test_validators.py`: missing trigger config key; human-gate `SEND_WORKFLOW_EVENT` raises `ActionForbiddenError`; SET_STATUS neither/both; unknown reference; `possible_self_trigger` warning emitted (AC2, AC3, AC20 warnings).
- `test_loop_guard.py`: depth cutoff at `MAX_DEPTH` (AC8); self-cycle via `causation_chain` membership (AC9).
- `test_engine.py`: `evaluate` orders rules by `run_order`; condition-fail path returns `conditions_failed` with no executor calls (AC5); happy path plans + executes actions and returns one `ExecutionResult` per rule; `partial_failure` when one `ActionResult.status=error` (AC15); disabled rules excluded (AC10).

**API integration (`apps/api/tests/automations/`, httpx.AsyncClient + Postgres container + factory-boy):**
- `test_rules_crud.py`: create/list/get/patch/enable/disable/delete; empty actions 422 (AC1); reference validation 422 (AC2); version conflict 409 (AC11); delete preserves executions with null rule_id (AC17).
- `test_human_gate_guard.py`: human-gate `send_workflow_event` 422; non-gate accepted (AC3).
- `test_canonical_close_spec.py`: seed spec + 3 linked tasks; dispatch merged envelope; assert two siblings closed, audit row + `closed_task_ids` (AC4); `spec_id NULL` → `conditions_failed`, no mutation (AC5).
- `test_idempotency.py`: double-dispatch same `trigger_event_id` → one execution per rule, side effect once (AC7).
- `test_loop_integration.py`: two ping-pong status rules; assert bounded executions + `skipped_loop` rows (AC8, AC9).
- `test_dry_run.py`: `/test` returns planned actions; before/after task snapshot identical; no execution row (AC12).
- `test_actor_provenance.py`: automation-driven status change emits `activity_events` with `actor_kind=system` + `payload.automation_rule_id`/`automation_depth` (AC14).
- `test_audit.py`: a state-mutating firing fans out a redacted `AuditEvent` through a recording `AuditSink` with `detail_ref→automation_execution`; rule create/enable/disable/delete each emit an `AuditEvent`; a direct UPDATE/DELETE on `automation_execution` raises on the Postgres container (F39 immutability trigger), and the repository exposes no update/delete path (AC13).
- `test_partial_failure.py`: a transition-policy-blocked `set_status` among other actions → `partial_failure` with per-action error, others applied (AC15).
- `test_rbac.py`: viewer/member/admin matrix + cross-workspace 404 (AC16).
- `test_sweeper.py`: orphan trigger row → `sweep_unprocessed_triggers` re-dispatches → rule fires once (AC18).
- `test_catalog.py`: catalog completeness (AC19).
- `test_yaml_examples.py`: every `examples/automations/*.yaml` parses + validates; canonical equals AC4 rule (AC20).

**Frontend (`apps/web/tests/automations/`, Vitest + Testing Library + MSW; Playwright for e2e):**
- `AutomationRuleEditor.test.tsx`: catalog-driven WHEN/IF/THEN render; human-gate event selection surfaces inline 422 (AC3, AC21).
- `AutomationRuleRow.test.tsx`: optimistic enable toggle + MSW-error rollback + toast (AC21).
- `DryRunPanel.test.tsx`: "Test" renders condition result + planned actions (AC12 surface).
- `automations.spec.ts` (Playwright): create the canonical rule end-to-end against MSW; assert it appears enabled in the list (AC4 surface).

**Key fixtures:** `automation_rule_factory`, `seed_spec_linked_tasks` (1 spec + 3 tasks sharing `spec_id`, project with a default `completed` status), `make_envelope(trigger_type, **change)`, `recording_executor`, `null_dispatcher`, `fake_clock`, `principal(role)` auth override, `system_principal`.

---

## 8. Security & policy considerations

- **No scope expansion, ever.** The automation runs as a `system` actor with a **fixed action catalog** bounded to board/workflow mutations within the rule's workspace/project. It cannot read/write repos, call MCP, deploy, change RBAC, or delete projects — there is no action type for any of these. This realizes "The agent never self-assigns permissions or expands its own scope" for the rule engine.
- **Human gates are inviolable.** `validate_rule` + the executor both reject any `SEND_WORKFLOW_EVENT` in `HUMAN_GATE_EVENTS`; an automation can never approve a spec/plan/PR, merge, or resume a paused run. This is double-enforced (save-time validation + run-time guard) so a rule crafted via direct DB insert still cannot bypass it.
- **Policy still applies to effects.** Actions execute through board-core/workflow services, so status-transition policy (F01) and workflow guards (F07) are honored — a blocked transition becomes a recorded `partial_failure`, not a forced write.
- **Immutable audit.** `automation_execution` is append-only at the repository layer (insert + select only; no UPDATE/DELETE) and hardened DB-side via `attach_immutability_trigger(automation_execution)` from `cross-cutting/F39-audit-log` (Postgres BEFORE UPDATE/DELETE block; skipped on the SQLite unit dialect where the repository enforces append-only). It captures actor, rule version, trigger event id, planned vs executed actions, depth, and causation chain — the per-domain detail feed for "why did this task close?" forensics. Separately, every **security-relevant** action — rule create/edit/enable/disable/delete and each automation **firing that mutates state** — also emits a redacted canonical `AuditEvent` through F39's `AuditSink` (with `detail_ref` back to the `automation_rule`/`automation_execution` row), so automations land in the **one** cross-workspace, tamper-evident, queryable audit log the spec Security section mandates ("Audit log — every agent action … immutable, queryable").
- **Loop / runaway protection.** `MAX_DEPTH` + causation-chain cycle detection bound cascades; combined with the `(rule_id, trigger_event_id)` idempotency key, a misconfiguration cannot fan out unboundedly or double-apply. The sweeper re-dispatch is safe precisely because of that idempotency key.
- **Tenant isolation.** Rule lookup, snapshot building, and all effects are scoped by `workspace_id` from the principal/envelope; cross-workspace access returns 404 (no existence leak). The dispatcher only enqueues envelopes for the originating workspace.
- **Input validation & injection safety.** Condition `field`s are whitelisted (`CONDITION_FIELDS`) and evaluated in-memory against a typed snapshot — no SQL is built from user input. Comment/notification templates are rendered with a fixed, escaped token set (`{{task.key}}`, `{{task.title}}`, `{{rule.name}}`) — no arbitrary attribute access or code execution.
- **Secret redaction.** Trigger envelopes and execution `action_results`/`detail` pass through the shared redaction utility before persistence so tokens/keys in any upstream payload never land in the audit rows.
- **RBAC on authoring.** Create/edit/enable/disable require `member`+; delete requires `admin`; `viewer` is read-only; an `agent-runner` token cannot author rules (prevents an agent from writing itself an automation that performs actions it is otherwise gated from).
- **Rate / abuse bounds.** Per-workspace cap on enabled rules and a per-minute evaluation budget (Redis-backed, reusing the C01 limiter) prevent a pathological rule set from saturating the worker.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (~3 engineer-weeks: ~1.5 backend engine + dispatcher + executor + API, ~1 frontend WHEN/IF/THEN builder + execution log, ~0.5 tests/wiring/examples). The pure engine is small; the surface area is the action executor breadth, the catalog-driven UI builder, and the loop/idempotency correctness tests.

Key risks:
- **Infinite/cascading loops** (High → mitigated): the headline failure mode. Mitigation: depth counter + causation-chain cycle detection + `(rule_id, trigger_event_id)` idempotency + save-time `possible_self_trigger` warning; covered by `test_loop_guard.py` and `test_loop_integration.py`.
- **Lost triggers (post-commit enqueue gap)** (Med): a crash between commit and broker enqueue could drop a firing. Mitigation: the `sweep_unprocessed_triggers` reconciliation backstop + idempotency key make delivery at-least-once without duplicate effects; covered by `test_sweeper.py`.
- **Action-executor coupling** (Med): the executor touches many board-core/workflow surfaces. Mitigation: the engine is pure behind the `ActionExecutor` Protocol; unimplemented soft-dep actions (`send_notification`, `close_linked_spec_tasks` without F02) record explicit `error`/`no_op` and never fake success.
- **Human-gate bypass** (Med → mitigated): an automation must never approve/merge. Mitigation: double-enforced reject (validator + executor) against `HUMAN_GATE_EVENTS`; covered by `test_human_gate_guard.py`.
- **Condition/field drift vs. board model** (Low/Med): whitelist must track F01 task fields. Mitigation: catalog endpoint is generated from `CONDITION_FIELDS`/action schemas and asserted in `test_catalog.py`; a single source of truth.
- **Concurrent firings on one entity** (Low/Med): two events for the same task racing. Mitigation: each fires its own idempotent execution; board-core's own optimistic `version` (F01) resolves conflicting writes (loser retried by the executor or recorded as `partial_failure`).

---

## 10. Key files / paths (exact)

Create — engine package:
- `packages/automation-engine/pyproject.toml`
- `packages/automation-engine/forge_automation/{__init__,enums,schemas,conditions,triggers,validators,engine,loop_guard,executor,errors}.py`
- `packages/automation-engine/forge_automation/py.typed`
- `packages/automation-engine/tests/{conftest,test_conditions,test_triggers,test_validators,test_loop_guard,test_engine}.py`

Create — DB + migration:
- `packages/db/forge_db/models/automation.py` (registers `automation_rule` + `automation_execution`; the latter calls `attach_immutability_trigger(automation_execution)` from `cross-cutting/F39-audit-log`)
- `packages/db/migrations/versions/<rev>_f21_automations.py` (creates both tables + enums; applies the F39 immutability trigger DDL to `automation_execution`)

Create — API:
- `apps/api/forge_api/routers/automations.py`
- `apps/api/forge_api/schemas/automations.py`
- `apps/api/forge_api/services/{automations,action_executor}.py`
- `apps/api/tests/automations/test_*.py`

Create — worker:
- `apps/worker/forge_worker/tasks/automations.py` (`evaluate_trigger`, `sweep_unprocessed_triggers`, beat schedule entry, `CeleryAutomationDispatcher`)

Create — frontend:
- `apps/web/app/[workspace]/projects/[projectKey]/settings/automations/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/settings/automations/[ruleId]/page.tsx`
- `apps/web/components/automations/{AutomationRuleList,AutomationRuleRow,AutomationRuleEditor,TriggerPicker,TriggerConfigForm,ConditionBuilder,ActionListEditor,ActionForm,DryRunPanel,AutomationExecutionLog,StaticWarningBanner}.tsx`
- `apps/web/lib/automations/{api,queries,mutations,types}.ts`
- `apps/web/tests/automations/*.{test.tsx,spec.ts}`

Create — community artifacts:
- `examples/automations/{close-spec-tasks-on-merge,escalate-on-sla-breach,assign-urgent-customer-impact}.yaml`

Edit (extend / wire):
- `packages/db/forge_db/models/enums.py` (append `AutomationTriggerType`, `AutomationActionType`, `AutomationExecutionStatus`, `AutomationTriggerSource`, `AutomationEntityType` to module + `__all__`)
- `packages/db/forge_db/models/__init__.py` (register `automation_rule` + `automation_execution` on `Base.metadata` + `__all__`; update the v2 table-set expectations in `packages/db/tests/test_models.py` / `test_migration.py` that assert the exact model set, per the F39 convention)
- `packages/contracts/forge_contracts/automation.py` (new module: `AutomationTriggerEnvelope`, `AutomationDispatcher` Protocol, `NullAutomationDispatcher`)
- `packages/board-core/src/board_core/events.py` (post-commit `dispatcher.dispatch(envelope)` hook for trigger-eligible activity events)
- `packages/workflow-engine/forge_workflow/engine.py` (post-commit `dispatcher.dispatch(envelope)` for `workflow_state_changed`)
- `apps/api/forge_api/main.py` (mount automations router) + `forge_api/deps.py` (`get_automation_dispatcher`, `SystemPrincipal`)
- `.env.example`, `deploy/.env.production.example` (`FORGE_AUTOMATION_MAX_DEPTH`)

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md → **Native Project Board → Board Features → Automations** ("Rule-based: when status = merged → close linked spec task") — the authoritative feature definition for this slice.
- FORGE_SPEC.md → Core Data Model (`Task.spec_id`, `SpecDocument`, `WorkflowRun.task_id`) — what `close_linked_spec_tasks` resolves.
- FORGE_SPEC.md → Workflow Engine (state vocabulary incl. `merged`/`closed`; human-gate semantics) — source of `WORKFLOW_STATE_CHANGED`/`PR_MERGED` triggers and the `HUMAN_GATE_EVENTS` prohibition.
- FORGE_SPEC.md → Human Approval System ("PR approval | Always required before merge") — why automations are barred from human-gate events.
- FORGE_SPEC.md → Security (immutable, queryable audit log; policy evaluation on every action; RBAC; secret redaction) — drives §8 and the `automation_execution` audit design.
- FORGE_SPEC.md → OSS Strategy → Extension Points ("Workflow DSL: declarative — custom workflows without code changes") — rationale for YAML-portable, data-driven rules + `examples/automations/`.
- FORGE_SPEC.md → Phased Roadmap → Phase 2 (V2): "Saved workflow automations (rule engine)" — the roadmap line this slice implements.
- forge-research-report.md → Symphony "task-as-control-plane" (workflow files define how work moves through statuses) — automations are the user-authored layer of that control plane: https://openai.com/index/open-source-codex-orchestration-symphony/
- Sibling slices (sources of truth for reused contracts): `docs/implementation-slices/v1/F01-project-board.md` (board entities, `board_core` services, filter-predicate shape, `activity_events`, `board_core.events.publish`), `docs/implementation-slices/v1/F07-feature-workflow-fsm.md` (`workflow_transition`, `forge_workflow.engine.WorkflowEngine`, `HUMAN_GATE_EVENTS`, dispatcher pattern), `docs/implementation-slices/cross-cutting/F39-audit-log.md` (`AuditEvent`/`AuditSink`, `attach_immutability_trigger`), `docs/implementation-slices/cross-cutting/F37-auth-secrets-byok.md` (`Principal`/RBAC, `SecretRedactor`), `docs/implementation-slices/v1/F16-slack-notifications.md` (notifications port behind `SEND_NOTIFICATION`).
- Linear / Plane.so automations (UX reference for WHEN/IF/THEN builders): https://linear.app/ , https://plane.so/

---

## 12. Out of scope / future

- **Time/schedule-based triggers** ("every Monday", "3 days before due date", cron rules) — this slice is event-driven only. A `SCHEDULED` trigger type backed by Celery Beat is a natural follow-up; the `AutomationRuleSpec` shape already accommodates it.
- **Cross-entity / aggregate conditions** ("when all subtasks done", "when 3+ tasks in epic blocked") — v1 conditions evaluate a single triggering entity's snapshot; rollup conditions are future.
- **Branching / multi-step workflows inside a rule** (if/else action trees, delays-between-actions) — actions are a flat ordered list here; richer action graphs belong to the V3 workflow visual editor.
- **LLM-assisted actions** (e.g. "summarize and comment", "auto-generate postmortem body") — deliberately excluded; the rule engine stays deterministic and policy-bounded. Agent-class work goes through the spec/workflow path, not automations.
- **External-side-effect actions** (call a webhook, create a Jira issue, trigger a deploy) — gated to future once the V2 PM adapters / integration egress + per-action policy exist; the current catalog is intentionally board/workflow-internal only.
- **Automation marketplace / sharing across workspaces** — the V3 "integration marketplace for community MCP connectors and skill profiles" can extend to shareable rule templates; v2 ships only the in-repo `examples/automations/` artifacts.
- **Visual rule editor / drag-and-drop graph** — v2 ships a form-based WHEN/IF/THEN builder; the V3 workflow visual editor is the home for graphical authoring.
