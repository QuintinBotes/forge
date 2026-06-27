# F36 — Human Approval System (gates + approval UI)

> Phase: cross-cutting · Spec module(s): Review & Approval Layer, Human Approval System (Approval Gate Types + "Approval UI Must Show"), Workflow Engine (`spec_review`/`plan_review`/`awaiting_review`/`awaiting_approval` states, `approval_granted:<gate>` guards, `escalation_policy`), Security (RBAC, audit, secret redaction, tenant isolation) · Status target: **Done** = there is **one** canonical approval-gate primitive and service that every gate type in the spec routes through — `spec`, `plan`, `pr`, `deploy`, `incident_remediation`, `policy_override`; any slice can `ApprovalService.create(...)` a gate and register a `GateContextProvider` (the "9 must-show items") plus an optional `GateResolutionHook` (the side effect on approve); a unified, gate-type-aware **Approval UI** (inbox + review shell) renders the 9 must-show items for any gate and exposes Approve / Reject / Request changes / Escalate; authorization is enforced server-side and uniformly (agents can never approve, viewers can never approve, `policy_override` is admin-only, no human self-approval of one's own run when configured, per-repo `review_rules`); the **deploy** and **policy_override** gate primitives (which no other slice owns) ship here; F08's `pr` gate is re-homed to resolve through this service with byte-identical external behavior (regression-locked against F08's tests); every create/decision/resolution is immutable-audited and emits `approval.requested`/`approval.resolved` on the activity bus. Lint + types + `pytest` green on `packages/approval-sdk` (`forge_approval`), the `apps/api` approvals router, and `apps/web` approval components, with ≥80% coverage on `forge_approval`.

---

## 1. Intent — what & why

Forge's second core design principle is **"Human-in-the-loop by design — review, approval, escalation, and rollback are first-class workflow states."** The spec enumerates six approval **gate types** and a nine-item **"Approval UI Must Show"** checklist. Before this slice, those gates are scattered: F07 created the baseline `approval_request` table and the `approval_granted:<kind>` guards; F08 built a concrete, **pr-specific** `ApprovalService.resolve`, merge gate, and review page; F17 creates an `incident_remediation` gate and its own remediation UI; F29 emits a `policy_override` gate. Each reinvents authorization, the decision lifecycle, the "must-show" payload, and the UI.

Before this slice, the `policy_override` situation is also already produced in V1 by `v1/F06-single-execution-agent` (its `act` node interrupts on a `restricted_action` whose policy `Decision.requires_approval` is true) with no canonical gate to route through; F29 later generalizes that trigger.

F36 is the **cross-cutting slice that owns the Human Approval System as one thing.** It extracts and generalizes what F08 prototyped into a shared primitive so that:

1. **One gate primitive, six gate types.** A single `approval_request` schema and a single `GateType` enum (`spec | plan | pr | deploy | incident_remediation | policy_override`) back every gate. Creating a gate is `ApprovalService.create(gate_type=…, subject=…)` everywhere.
2. **One authorization policy.** "Agents never approve, viewers never approve, `policy_override` is admin-only, repo `review_rules` apply to `pr`, deploy permissions apply to `deploy`, optional no-self-approval" lives in **one** `ApprovalAuthorizer` enforced server-side — not re-implemented per gate (closes Build-Prompt constraint #2 "the agent never self-assigns permissions" and #5 "human approval is required before PR merge — always").
3. **One context contract.** Each gate type registers a `GateContextProvider` that returns the spec's nine "must-show" items in a uniform `ApprovalContext` envelope; the UI renders whichever sections are populated.
4. **One resolution contract.** Each gate registers a `GateResolutionHook` whose `on_resolved(...)` performs the gate-specific side effect (merge a PR, advance the FSM, run a runbook, grant a single-use policy override) and returns blocking reasons if the approval is recorded but the downstream effect cannot complete yet (e.g. CI still red).
5. **One UI.** A single, keyboard-first **Approval Inbox** and **Approval Review shell** serve all gates; F08's pr panels and F17's runbook panel slot in by gate type via a panel registry.
6. **Two gate primitives ship here (no slice owns their primitive).** The **deploy** gate (env promotion request blocked by `deploy_rules`) and the **policy_override** gate (out-of-policy / `requires_approval` tool call) have no slice that owns their *primitive*. F36 provides their `GateContextProvider`s, `GateResolutionHook`s, the single-use `PolicyOverrideGrant`, and the frozen `create`/`consume` contracts. Their **producers** live in other slices and only call `ApprovalService.create(...)`: the `policy_override` gate is already produced in **V1** by `v1/F06-single-execution-agent` (act-node interrupt on a `requires_approval` restricted action) and generalized by `v3/F29-advanced-policy-engine` (conditional `require_approval`); the `deploy` gate's producer is the env-promotion control plane in `v3/F31-deployment-gates`. F36 ships both gates with synthetic triggers + tests so the primitive is provably complete before its producers land.

**F36 does NOT** own the gate-specific *content*: it does not compute spec traceability (F02/F08), does not run the merge gate or open PRs (F08), does not draft runbooks (F17), and does not evaluate conditional policy (F04/F29). It owns the **frame** — the table, the service, the authorization, the registry, the events, and the UI shell — into which those slices plug.

---

## 2. User-facing behavior / journeys

**J1 — Approval inbox (any member/admin).**
A reviewer opens **Approvals** (top-level nav, or `g a` from anywhere). They see a single keyboard-navigable inbox of every gate awaiting them across projects — one row per pending `approval_request` they are authorized to act on: gate-type badge (`Spec`, `Plan`, `PR`, `Deploy`, `Incident`, `Policy override`), subject title, project, risk level (with a red marker for `critical`), age, and who/what requested it. Filters: gate type, project, risk, "assigned to me" vs "all I can act on". A pending-count badge (`/approvals/count`) shows on the nav item.

**J2 — Unified review (reviewer).**
Selecting a row opens the **Approval Review** shell for that gate. Regardless of gate type it renders the spec's nine must-show items, hiding sections that don't apply to the gate:
1. **Goal & requirements** the gate addresses (task goal + spec requirement refs).
2. **Changed files** with diff preview + syntax highlighting (pr/deploy).
3. **Verification results** — lint/type/test/coverage (pr).
4. **Spec traceability** — which acceptance criteria are satisfied and how (pr).
5. **Knowledge provenance** — retrieved chunks that informed the work.
6. **Confidence** score + rationale.
7. **Risks flagged** — policy warnings, security findings, uncertain areas, blast radius (always shown; central panel for `policy_override`/`incident`).
8. **Full run trace** (embedded F10 timeline).
9. **Actions** — Approve / Reject / Request changes (and **Escalate** for `incident_remediation`/`policy_override`).

**J3 — Approve a PR (reviewer).**
On a `pr` gate the reviewer presses `a` (Approve). F36 authorizes (member/admin, not the agent that produced it, repo `review_rules` satisfied), records the decision, and invokes the pr `GateResolutionHook` (owned by F08 — runs the merge gate, merges if green, advances the FSM). The shell shows "Approved — merged" on success, or "Approved but not merged: CI is failing (1/3 checks)" with the blocking reasons when the downstream gate is not yet satisfiable. The decision is final and audited; the inbox row disappears.

**J4 — Deploy gate (reviewer).**
An agent (or a v3 promotion workflow) requests promotion to a `restricted_environment` while `deploy_rules.allow_agent_deploy: false`. F36 blocks the action and creates a `deploy` gate carrying target environment, source commit, change summary, verification status, and a **risk flag** for the restricted env. A reviewer with deploy permission approves; F36 emits `deploy.approved`. (The actual promotion is executed downstream; the gate only authorizes it.)

**J5 — Policy-override gate (admin only).**
The agent attempts an out-of-policy / `requires_approval` tool call (F04/F29 tool gate). The call is **paused**, not executed; F36 creates a `policy_override` gate whose central content is: the exact attempted action, the policy rule(s) that blocked it, severity, and the agent's rationale. Only an **admin** can resolve it. On Approve, F36 mints a **single-use, short-TTL** `PolicyOverrideGrant` bound to the exact action fingerprint and emits `policy_override.granted`; the paused tool call resumes **once** and the grant is consumed. On Reject, the call is denied and the run routes to `needs_human_input`. The grant **never broadens future scope** (Build-Prompt constraint #2).

**J6 — Spec / plan gate (reviewer).**
On entering `spec_review`/`plan_review` the FSM effect (F07/F02) calls `ApprovalService.create(gate_type=spec|plan, …)`. The reviewer approves in the same shell; the resolution hook emits the `spec_approved`/`plan_approved` FSM event (so the F07 `approval_granted:<gate>` guard passes) and the workflow advances. `Request changes` emits `spec_changes_requested`/`plan_changes_requested`, returning the workflow to drafting.

**J7 — Request changes / Reject / Escalate.**
`Request changes` records `changes_requested` and routes the subject's workflow back to drafting/needs-human-input (gate-specific). `Reject` records `rejected` and routes to `cancelled` (or denies the action). `Escalate` (incident/policy_override) re-targets the gate to admins and notifies — the request stays pending but its `risk_level` is raised and required role becomes admin.

**J8 — Notification surfaces (passive).**
Every `approval.requested` and `approval.resolved` event is consumed by F16 (Slack) and the board timeline (F01); a reviewer can also resolve from Slack, which calls the same `ApprovalService.resolve` — so authorization and audit are identical regardless of surface.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

The baseline `approval_request` table is created in the foundation slice (**`cross-cutting/F00-foundation`**, `packages/db`, Task 0.2 — the same baseline `workflow_run`/`agent_run`/`task`/`users`/`workspaces` tables F06/F07/F08 build on) with a `kind` column (`spec|plan|pr|deploy`). F36 **owns the canonical schema** and migrates it into the generalized form below, adds two child tables, and reconciles the early `kind` naming to `gate_type`. SQLAlchemy models live in `packages/db/forge_db/models/approval.py`; migration in `packages/db/migrations/versions/<rev>_f36_approval_framework.py`.

**`approval_request` (generalized — supersedes the F08 pr-only columns):**

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` FK→`workspaces.id` NOT NULL | tenant isolation; **every** query filters on it |
| `project_id` | `uuid` FK→`projects.id` NULL | inbox grouping; null for workspace-level gates |
| `gate_type` | `VARCHAR(32)` NOT NULL | `spec`\|`plan`\|`pr`\|`deploy`\|`incident_remediation`\|`policy_override` (renamed from baseline `kind`) |
| `status` | `VARCHAR(24)` NOT NULL default `pending` | `pending`\|`approved`\|`rejected`\|`changes_requested`\|`expired` |
| `subject_type` | `VARCHAR(24)` NOT NULL | `workflow_run`\|`agent_run`\|`step`\|`incident`\|`deployment` |
| `subject_id` | `uuid` NOT NULL | polymorphic subject key |
| `workflow_run_id` | `uuid` FK→`workflow_run.id` NULL | set for workflow-bound gates (spec/plan/pr/incident) — lets F07 guards resolve |
| `agent_run_id` | `uuid` FK→`agent_run.id` NULL | set for agent-produced gates (pr/policy_override) |
| `required_approvals` | `int` NOT NULL default 1 | from `review_rules.min_approvals`; V1 = 1 |
| `risk_level` | `VARCHAR(16)` NOT NULL default `info` | `info`\|`warning`\|`critical`; drives inbox sort + UI emphasis |
| `gate_payload` | `JSONB` NOT NULL default `{}` | **secret-redacted** gate-specific summary snapshot (deploy target, override action fingerprint, etc.) |
| `context_ref` | `TEXT` NULL | optional MinIO key for a large pre-rendered context snapshot |
| `requested_by` | `uuid` FK→`users.id` NULL | null when agent/system requested |
| `requested_actor` | `VARCHAR(128)` NOT NULL | `agent:<id>`\|`user:<id>`\|`system` (used for no-self-approval) |
| `decision_note` | `TEXT` NULL | decisive resolver note (denormalized convenience) |
| `resolver_user_id` | `uuid` FK→`users.id` NULL | decisive resolver |
| `expires_at` | `TIMESTAMPTZ` NULL | optional SLA; sweeper marks `expired` |
| `requested_at` | `TIMESTAMPTZ` default now() | |
| `resolved_at` | `TIMESTAMPTZ` NULL | |

Indexes: `(workspace_id, status)`, `(workflow_run_id)`, `(project_id, status)`. **Partial unique** (generalizes F08's one-pending-pr-per-run): `CREATE UNIQUE INDEX uq_pending_gate ON approval_request (subject_type, subject_id, gate_type) WHERE status='pending'` — at most one open gate of a type per subject.

**`approval_decision` (new — per-approver audit / multi-approver forward-compat):**

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `approval_request_id` | `uuid` FK→`approval_request.id` ON DELETE CASCADE | indexed |
| `approver_user_id` | `uuid` FK→`users.id` NOT NULL | who voted |
| `decision` | `VARCHAR(24)` NOT NULL | `approve`\|`reject`\|`request_changes`\|`escalate` |
| `note` | `TEXT` NULL | |
| `created_at` | `TIMESTAMPTZ` default now() | |

Unique `(approval_request_id, approver_user_id)` — one vote per approver. **Append-only:** `approval_decision` adopts F39's reusable `attach_immutability_trigger(approval_decision)` (DB-level UPDATE/DELETE block) and repository-level append-only enforcement, exactly as `v1/F07-feature-workflow-fsm`'s `workflow_transition` does — giving a tamper-evident per-approver decision trail. The `approval_request.status` is **derived** from decisions (V1: a single decisive vote resolves; `required_approvals>1` aggregation is structurally supported, see §4).

**`policy_override_grant` (new — backs the `policy_override` resolution hook):**

| column | type | notes |
|---|---|---|
| `id` | `uuid` PK | |
| `approval_request_id` | `uuid` FK→`approval_request.id` | |
| `agent_run_id` | `uuid` FK→`agent_run.id` NOT NULL | |
| `action_fingerprint` | `TEXT` NOT NULL | stable hash of the exact tool call being permitted |
| `granted_by` | `uuid` FK→`users.id` NOT NULL | admin |
| `consumed` | `boolean` NOT NULL default false | **single-use** |
| `expires_at` | `TIMESTAMPTZ` NOT NULL | short TTL (e.g. 15 min) |
| `created_at` | `TIMESTAMPTZ` default now() | |

Partial unique: `CREATE UNIQUE INDEX uq_active_override ON policy_override_grant (agent_run_id, action_fingerprint) WHERE consumed=false`.

**Migration tasks:** rename `approval_request.kind`→`gate_type`; add the new columns (backfill `workspace_id`/`subject_type`/`subject_id` for any existing pr rows from their `workflow_run`); create `approval_decision` + `policy_override_grant`; replace F08's `uq_pending_pr_approval` with the generic `uq_pending_gate`; attach F39's immutability trigger to `approval_decision`. **Audit** is written through the canonical cross-cutting immutable audit log (`cross-cutting/F39-audit-log`): every `create`/decision/resolution emits a redacted `AuditEvent` to the injected `AuditSink` (`audit_log` table). For workflow-bound gates (`spec`/`plan`/`pr`/`incident_remediation`), the resolution *additionally* surfaces in F07's append-only `workflow_transition` rows (`record: approval_event`) when the gate's resolution hook emits the FSM event — F36 does not write `workflow_transition` directly.

### 3.2 Backend (FastAPI routes + services/packages)

**New package `packages/approval-sdk/forge_approval/`** — the canonical Review & Approval Layer (the spec lists this as a Product-Scope module but no package; F36 introduces it as a low-level shared package depended on by `workflow-engine`, `spec-engine`, `board-core`, `policy-sdk`, and the apps — it depends on none of them, so gate-owning packages register providers/hooks at the composition root with no cycles):

- `models.py` — `GateType`, `GateStatus`, `ApprovalAction`, `ApprovalRequest` (domain), `ApprovalSummary`, `ApprovalDecisionRecord`, `RiskFlag`, `ApprovalContext` (the nine-item envelope), `ApprovalResolution`, `ResolutionOutcome`, `Principal` (§4).
- `registry.py` — `GateContextProvider` Protocol, `GateResolutionHook` Protocol, `GateRegistry` (register/lookup by gate type).
- `authorizer.py` — `ApprovalAuthorizer` + `AuthorizationError`: the single server-side authorization policy (RBAC, agent-block, no-self-approval, per-gate minimum role matrix, repo `review_rules`, deploy perms).
- `requirements.py` — `GateRequirementResolver.is_required(gate_type, task, policy, skill) -> bool`: centralizes "is this gate required?" from `Task.requires_approval{spec,plan,pr,deploy}`, `review_rules.approval_required_for_merge`, `skill.human_review_required` / `requires_human_approval_before_action`, `deploy_rules`. (F07's `plan_not_required` guard delegates here.)
- `service.py` — `ApprovalService` (§4): `create`, `list`, `get`, `get_context`, `resolve`, `count`. Orchestrates repo + authorizer + registry + event bus + audit + redaction.
- `events.py` — typed `ApprovalRequestedEvent` / `ApprovalResolvedEvent` (published on the F01 activity bus).
- `repository.py` — `ApprovalRepository` Protocol (persistence boundary; SQLAlchemy impl in apps).
- `redaction.py` — thin `redact(payload)` wrapper that delegates to the canonical `forge_auth.redaction.SecretRedactor` (owned by `cross-cutting/F37-auth-secrets-byok`); applied to `gate_payload`/`context`/`context_ref` snapshots before persist, audit, event emission, or transmission. F36 does not define its own redaction patterns.
- `providers/` — F36-owned providers for the two gates no other slice owns the primitive for: `deploy.py` (`DeployGateProvider` + `DeployResolutionHook`) and `policy_override.py` (`PolicyOverrideGateProvider` + `PolicyOverrideResolutionHook`, mints `PolicyOverrideGrant`). (pr/spec/plan/incident providers live in their owning slices and are registered at startup.)

**App `apps/api/app/`:**
- `routers/approvals.py` — the generic, gate-agnostic REST surface (§4). Replaces/absorbs F08's `/approvals*` endpoints.
- `services/approval_service.py` — thin app-layer adapter constructing `forge_approval.ApprovalService` with the SQLAlchemy `ApprovalRepository`, the request `Principal`, and the process-wide `GateRegistry`. (F08's pr-specific service becomes the `pr` `GateResolutionHook` registered into this registry.)
- `models/approval.py` (re-export from `packages/db`), `schemas/approval.py` (API request/response Pydantic).
- `bootstrap/gate_registry.py` — composition root: registers all available providers/hooks at app start (`pr`→F08, `spec`/`plan`→F02, `incident_remediation`→F17, `deploy`/`policy_override`→F36). Missing providers degrade gracefully (the gate can still be created and shown read-only; resolve returns `not_implemented` if no hook).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

F36 adds **no LangGraph code** and owns no agent loop. It adds:

- `apps/worker/forge_worker/tasks/approvals.py`:
  - `expire_pending_approvals()` — periodic sweep marking gates past `expires_at` as `expired`, emitting `approval.resolved(status=expired)` and routing the subject's workflow to `needs_human_input` where applicable.
  - `consume_override_grant(agent_run_id, action_fingerprint)` — implements the `PolicyOverrideGate.consume(...)` contract (§4); invoked by the agent-runtime `resume` path (`v1/F06-single-execution-agent`, and later `v3/F29-advanced-policy-engine`) when a paused/interrupted tool call resumes: atomically checks-and-consumes a non-expired `policy_override_grant` (returns the single-use allow, or denies). This is the *consumption* side of J5; the *grant* is created synchronously in the resolution hook.
- The activity-bus producers are F36's `ApprovalService` itself (synchronous emit on create/resolve, fanned onto the `notifications` queue F16 consumes). No new queue; reuses the default Celery app from `cross-cutting/F00-foundation`.

### 3.4 Frontend / UI (Next.js routes/components)

**App `apps/web/`** — one approval surface for all gates:

- Route `app/(dashboard)/approvals/page.tsx` — **Approval Inbox**. Keyboard-first (`j/k` navigate, `Enter` open, `f` filter), TanStack Query + Table, gate-type/project/risk/"mine" filters, pending-count badge. Empty state guides to "no approvals waiting on you."
- Route `app/(dashboard)/approvals/[approvalId]/page.tsx` — **Approval Review shell**. Fetches `ApprovalContext`; renders the nine must-show panels via a **panel registry** keyed by `gate_type`. Hides empty sections.
- `components/approvals/ApprovalShell.tsx` — the layout + action bar; `components/approvals/panel-registry.ts` — maps `gate_type → ordered panel components`.
- F36-owned **generic panels** (render for any gate from `ApprovalContext`): `GoalPanel.tsx` (item 1), `ConfidencePanel.tsx` (6), `RiskFlagsPanel.tsx` (7), `RunTraceEmbed.tsx` (8, reuses F10 `RunTraceTimeline`), `KnowledgeProvenancePanel.tsx` (5), `ActionBar.tsx` (9).
- **Gate-specific panels are slotted from their owning slices:** F08's `DiffViewer`, `VerificationPanel`, `TraceabilityMatrix` register under `pr` (and `deploy` reuses `DiffViewer`); F17's `RunbookPanel` registers under `incident_remediation`; F36 ships `DeployPlanPanel.tsx` and `PolicyOverridePanel.tsx`.
- `lib/api/approvals.ts` — hooks `useApprovalInbox(filters)`, `useApproval(id)`, `useApprovalContext(id)`, `useApprovalCount()`, mutation `useResolveApproval()` with **optimistic update + rollback** (per board UX standards) — on a `merged=false` / blocked outcome it rolls back and surfaces the `blocking_reasons` banner.
- Keyboard shortcuts on the shell: `a` approve, `r` request changes, `x` reject, `e` escalate (when offered), `g a` jump to inbox.
- **F08's `tasks/[taskId]/review/page.tsx` is refactored to a thin redirect** to `/approvals/{pr_approval_id}` (its panels are re-registered, not deleted) — regression-locked against F08's existing component tests.

### 3.5 Infra / deploy (compose, helm, caddy)

- **No new compose service.** Add a Celery beat schedule entry for `expire_pending_approvals` (e.g. every 60s) to the existing `worker` service.
- **MinIO** reuses the existing artifact bucket for optional `context_ref` snapshots (large pre-rendered contexts); no new bucket required.
- **Caddy/Helm:** N/A — no new public route beyond `/approvals` served by the existing `web`/`api` services.

---

## 4. Public interfaces / contracts

**Core enums + models** (`packages/approval-sdk/forge_approval/models.py`):

```python
from enum import Enum
from uuid import UUID
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel

class GateType(str, Enum):
    SPEC = "spec"
    PLAN = "plan"
    PR = "pr"
    DEPLOY = "deploy"
    INCIDENT_REMEDIATION = "incident_remediation"
    POLICY_OVERRIDE = "policy_override"

class GateStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    EXPIRED = "expired"

class ApprovalAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    ESCALATE = "escalate"

class Role(str, Enum):            # mirrors Security RBAC roles
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
    AGENT_RUNNER = "agent-runner"

class Principal(BaseModel):
    kind: Literal["user", "agent", "system"]
    id: UUID | None                # user id or agent id; None for system
    role: Role | None              # workspace/project RBAC role for users
    workspace_id: UUID

class RiskFlag(BaseModel):
    severity: Literal["info", "warning", "critical"]
    category: str                  # policy | security | confidence | blast_radius | coverage | restricted_env
    message: str
    source: str | None = None

class ApprovalContext(BaseModel):  # the spec's nine "must-show" items (Optional => UI hides empty)
    approval_id: UUID
    gate_type: GateType
    goal: str                                       # 1
    requirements: list[dict] = []                   # 1 (RequirementRef from F02)
    diff: dict | None = None                        # 2 (DiffSummary; pr/deploy)
    verification: dict | None = None                # 3 (VerificationReport summary; pr)
    traceability: list[dict] | None = None          # 4 (TraceabilityRow[]; pr)
    knowledge_refs: list[dict] | None = None        # 5 (KnowledgeRef[])
    confidence: dict | None = None                  # 6 ({score, rationale})
    risk_flags: list[RiskFlag] = []                 # 7 (always populated; central for override/incident)
    run_trace_ref: dict | None = None               # 8 ({workflow_run_id, agent_run_id})
    available_actions: list[ApprovalAction]         # 9
    gate_payload: dict[str, Any] = {}               # gate-specific extras (deploy plan, override action, runbook)

class ApprovalRequest(BaseModel):
    id: UUID
    workspace_id: UUID
    project_id: UUID | None
    gate_type: GateType
    status: GateStatus
    subject_type: str
    subject_id: UUID
    workflow_run_id: UUID | None
    agent_run_id: UUID | None
    required_approvals: int
    risk_level: Literal["info", "warning", "critical"]
    gate_payload: dict[str, Any]
    requested_by: UUID | None
    requested_actor: str
    requested_at: datetime
    resolved_at: datetime | None
    resolver_user_id: UUID | None

class ApprovalSummary(BaseModel):
    id: UUID
    gate_type: GateType
    status: GateStatus
    title: str
    project_id: UUID | None
    risk_level: str
    requested_actor: str
    requested_at: datetime

class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalAction
    note: str | None = None

class ApprovalDecisionRecord(BaseModel):
    approver_user_id: UUID
    decision: ApprovalAction
    note: str | None
    created_at: datetime

class ResolutionOutcome(BaseModel):
    completed: bool                       # did the downstream side effect finish?
    blocking_reasons: list[str] = []      # non-empty => approval recorded but effect deferred (e.g. CI red)
    follow_up_state: str | None = None    # resulting FSM/workflow state, if any
    details: dict[str, Any] = {}

class ApprovalResolution(BaseModel):
    approval_id: UUID
    status: GateStatus                    # resulting gate status
    outcome: ResolutionOutcome            # gate-specific result (folds F08's merge result in via details)
```

**Registry contracts** (`registry.py`):

```python
from typing import Protocol, ClassVar

class GateContextProvider(Protocol):
    gate_type: ClassVar[GateType]
    async def build_context(self, request: ApprovalRequest, *, session) -> ApprovalContext: ...
    def available_actions(self, request: ApprovalRequest) -> list[ApprovalAction]: ...

class GateResolutionHook(Protocol):
    gate_type: ClassVar[GateType]
    # Called AFTER the decision row is persisted and authorization has passed.
    async def on_resolved(self, request: ApprovalRequest, decision: ApprovalDecisionRequest,
                          actor: Principal, *, session) -> ResolutionOutcome: ...

class GateRegistry:
    def register_provider(self, provider: GateContextProvider) -> None: ...
    def register_hook(self, hook: GateResolutionHook) -> None: ...
    def provider(self, gate_type: GateType) -> GateContextProvider: ...   # raises if missing
    def hook(self, gate_type: GateType) -> GateResolutionHook | None: ...  # None => emit-only gate
```

**Authorization** (`authorizer.py`) — the single policy:

```python
class AuthorizationError(Exception):
    def __init__(self, reason: str): ...

# Minimum role required to RESOLVE each gate (escalate raises the bar to ADMIN):
GATE_MIN_ROLE: dict[GateType, Role] = {
    GateType.SPEC: Role.MEMBER,
    GateType.PLAN: Role.MEMBER,
    GateType.PR: Role.MEMBER,
    GateType.DEPLOY: Role.MEMBER,                 # + deploy permission check
    GateType.INCIDENT_REMEDIATION: Role.MEMBER,   # admin for forbidden/over-blast (escalated)
    GateType.POLICY_OVERRIDE: Role.ADMIN,         # always admin (escalate_to_admin)
}

class ApprovalAuthorizer:
    def __init__(self, policy_reader, *, forbid_self_approval: bool = False): ...
    def check(self, actor: Principal, request: ApprovalRequest,
              decision: ApprovalDecisionRequest) -> None:
        """Raise AuthorizationError unless ALL hold:
        - actor.kind == 'user'                      (agents/system never resolve)
        - actor.role != VIEWER
        - actor.role >= GATE_MIN_ROLE[gate_type]    (POLICY_OVERRIDE => ADMIN)
        - PR: repo review_rules satisfied (required_reviewers / approval_required_for_merge)
        - DEPLOY: actor has deploy permission for the target environment (deploy_rules)
        - not self-approval: actor 'user:<id>' must differ from request.requested_actor
          when forbid_self_approval, AND must never equal the agent that produced the run
        """
```

**Service** (`service.py`):

```python
class ApprovalService:
    def __init__(self, repo: "ApprovalRepository", registry: GateRegistry,
                 authorizer: ApprovalAuthorizer,
                 events: "ActivityBus",        # publishes approval.requested/approval.resolved (F01 bus / F16 queue)
                 audit: "AuditSink",           # cross-cutting/F39 — redacted AuditEvent per create/decision/resolve
                 redactor: "SecretRedactor"):  # cross-cutting/F37 — redacts gate_payload/context pre-persist/emit
        ...

    async def create(self, *, workspace_id: UUID, gate_type: GateType,
                     subject_type: str, subject_id: UUID,
                     workflow_run_id: UUID | None = None, agent_run_id: UUID | None = None,
                     project_id: UUID | None = None,
                     requested_by: UUID | None = None, requested_actor: str = "system",
                     required_approvals: int = 1, risk_level: str = "info",
                     gate_payload: dict | None = None,
                     expires_at: datetime | None = None) -> ApprovalRequest:
        """Idempotent on the partial-unique (subject_type, subject_id, gate_type) while pending:
        returns the existing pending gate instead of creating a duplicate. Redacts gate_payload,
        persists, writes audit, emits approval.requested."""

    async def list(self, *, workspace_id: UUID, actor: Principal,
                   status: GateStatus | None = None, gate_type: GateType | None = None,
                   project_id: UUID | None = None, mine: bool = False) -> list[ApprovalSummary]:
        """Workspace-scoped; 'mine' filters to gates the actor is authorized to resolve."""

    async def get(self, approval_id: UUID, *, workspace_id: UUID) -> ApprovalRequest: ...
    async def get_context(self, approval_id: UUID, *, workspace_id: UUID) -> ApprovalContext:
        """Delegates to registry.provider(gate_type).build_context(...)."""

    async def resolve(self, approval_id: UUID, decision: ApprovalDecisionRequest,
                      actor: Principal, *, workspace_id: UUID) -> ApprovalResolution:
        """1. load (workspace-scoped; cross-workspace => NotFound, not Forbidden).
        2. reject if already resolved (409).
        3. authorizer.check(...).
        4. persist approval_decision row (unique per approver).
        5. derive status: approve+ (count>=required_approvals & no reject) => approved;
           reject => rejected; request_changes => changes_requested; escalate keeps pending,
           raises risk_level + min role to admin.
        6. if resolving: invoke registry.hook(gate_type).on_resolved(...) -> ResolutionOutcome
           (e.g. pr hook runs merge gate; may return blocking_reasons with status still approved).
        7. write audit, emit approval.resolved.
        Idempotent on (approval_id, approver_user_id)."""

    async def count(self, *, workspace_id: UUID, actor: Principal,
                    status: GateStatus = GateStatus.PENDING) -> int: ...
```

**Requirement resolver** (`requirements.py`):

```python
class GateRequirementResolver:
    @staticmethod
    def is_required(gate_type: GateType, *, task, policy, skill) -> bool:
        """pr  -> task.requires_approval.pr or review_rules.approval_required_for_merge (default True)
        spec  -> task.requires_approval.spec (feature-class default True)
        plan  -> task.requires_approval.plan
        deploy-> task.requires_approval.deploy or target in deploy_rules.restricted_environments
                 or not deploy_rules.allow_agent_deploy
        incident_remediation -> skill.requires_human_approval_before_action (always True for incident-response)
        policy_override -> always True"""
```

**REST API (all under `/api/v1`, all authenticated):**

```
GET    /approvals?status=&gate_type=&project_id=&mine=true   -> list[ApprovalSummary]   # inbox
GET    /approvals/count?status=pending&mine=true             -> {"count": int}          # nav badge
GET    /approvals/{approval_id}                              -> ApprovalRequest
GET    /approvals/{approval_id}/context                      -> ApprovalContext          # the 9 items
GET    /approvals/{approval_id}/decisions                   -> list[ApprovalDecisionRecord]
POST   /approvals/{approval_id}/decision                    -> ApprovalResolution
```

`POST .../decision` body = `ApprovalDecisionRequest`. Errors: `403` AuthorizationError (with `reason`), `404` cross-workspace/missing, `409` already-resolved or duplicate vote.

**Policy-override consumption contract (consumed by the F06 tool gate):**

```python
class PolicyOverrideGate(Protocol):
    async def consume(self, *, agent_run_id: UUID, action_fingerprint: str) -> bool:
        """Atomically check-and-consume a non-expired grant. True => allow this single call;
        False => no valid grant (deny / keep paused). Never grants future scope."""
```

**Activity-bus events** (`events.py`) — consumed by F16/F01:

```python
class ApprovalRequestedEvent(BaseModel):
    approval_id: UUID; workspace_id: UUID; project_id: UUID | None
    gate_type: GateType; subject_type: str; subject_id: UUID
    risk_level: str; requested_actor: str; requested_at: datetime

class ApprovalResolvedEvent(BaseModel):
    approval_id: UUID; workspace_id: UUID; gate_type: GateType
    status: GateStatus; resolver_user_id: UUID | None
    outcome: ResolutionOutcome; resolved_at: datetime
```

These are the **only two consumer-facing event types** F36 introduces (F16/F01 subscribe to exactly these). The gate-specific semantics referenced in the journeys — `deploy.approved` (J4) and `policy_override.granted` (J5) — are **not** separate consumer events: they ride on `approval.resolved` (`gate_type=deploy|policy_override`, `status=approved`) plus the gate's `GateResolutionHook` domain effect (the `DeployResolutionHook` and `PolicyOverrideResolutionHook` may emit an internal domain signal that the downstream producer workflow — `v3/F31-deployment-gates` / `v3/F29-advanced-policy-engine` — consumes). F36 adds no new event type to the F16 notification contract.

---

## 5. Dependencies — features/slices that must exist first

- `v1/F07-feature-workflow-fsm` — **hard.** Provides the `approval_granted:<kind>` guard that reads gate status (reconciled to `gate_type` here), the `EffectRegistry` whose `create_spec_approval`/`create_plan_approval` effect bodies *call* `ApprovalService.create`, the `needs_human_input`/`cancelled` routing on reject/changes, and the append-only `workflow_transition` audit rows (`record: approval_event`) that a resolution's FSM event lands in. (The baseline `approval_request`/`workflow_run`/`agent_run` tables come from `cross-cutting/F00-foundation`, not F07.)
- `v1/F08-plan-execute-verify-pr-approval` — **hard peer.** Reference implementation F36 generalizes: the pr `GateResolutionHook` (`MergeGateEvaluator` + merge + FSM advance), the pr `GateContextProvider` (diff/verification/traceability/provenance), and the pr review panels. F36 must regression-lock F08's external behavior; F08's `ApprovalService`/`/approvals*` are absorbed.
- `v1/F02-spec-engine` — **hard.** Provides the `spec`/`plan` `GateContextProvider`s (requirements, traceability source `SpecDocument`) and wires the `spec`/`plan` resolution hooks to emit `spec_approved`/`plan_approved` FSM events.
- `v1/F04-repo-policy` — **hard.** Supplies `review_rules` (`required_reviewers`, `approval_required_for_merge`, `min_approvals`) and `deploy_rules` consumed by `ApprovalAuthorizer` and `GateRequirementResolver`; `Task.requires_approval{spec,plan,pr,deploy}`.
- `v1/F11-skill-profiles` — **hard.** `skill.human_review_required` / `requires_human_approval_before_action` / `max_blast_radius` feed `GateRequirementResolver` and the risk flags.
- `cross-cutting/F00-foundation` — **hard.** Baseline `approval_request`/`workflow_run`/`agent_run`/`task`/`users`/`workspaces` tables, `packages/contracts`, `packages/db` async session, the RBAC role scaffold, MinIO `ArtifactStore` (for `context_ref` snapshots), and the default Celery app the beat task reuses. (Referenced variously as `cross-cutting/F00-foundation` / `v1/F00-foundation-substrate` across sibling slices; reconcile when the foundation slice lands.)
- `cross-cutting/F37-auth-secrets-byok` — **hard.** Provides `forge_auth.redaction.SecretRedactor` (the single redaction filter `redaction.py` wraps), the `require_role(...)` RBAC dependency + role ranks, and the authenticated request `Principal` the API layer maps to `forge_approval.Principal` before calling the service. (`forge_approval` *defines* the `Principal`/`Role` domain models the SDK consumes — per `v1/F16-slack-notifications` §4 which imports `from forge_approval.models import Principal, Role` — and F37 supplies the auth context populating them.)
- `cross-cutting/F39-audit-log` — **hard.** The canonical immutable audit log: the frozen `AuditEvent` contract + `AuditSink` Protocol + `SqlAuditWriter` (`audit_log` table) that F36 writes every create/decision/resolution through, and the reusable `attach_immutability_trigger(approval_decision)` helper for the append-only decision table.
- `v1/F06-single-execution-agent` — **soft peer (V1 policy_override producer/consumer).** Its `act`-node interrupt on a `requires_approval` restricted action is the V1 trigger that calls `ApprovalService.create(gate_type=policy_override, …)`; its `resume` path calls the `PolicyOverrideGate.consume(...)` contract frozen here. F36 ships the gate + grant + consume contract; F06 supplies the producing/consuming call-sites.
- `v1/F10-run-trace-viewer` — **soft.** Its `RunTraceTimeline` React component is embedded in the run-trace panel (item 8); shell ships with a stub link if F10 lags.
- `v1/F16-slack-notifications` — **soft (downstream consumer).** Consumes `approval.requested`/`approval.resolved` and resolves via the same `ApprovalService.resolve` (mapping Slack identity → `forge_approval.Principal`); F36 has no runtime dependency on it.
- `v1/F01-project-board` — **soft.** The activity bus + unified timeline (which renders `approval.*` events), and the top-level nav slot for the inbox.

**Downstream consumers (NOT dependencies — they depend on F36):** `v2/F17-incident-workflows` (registers the `incident_remediation` `GateContextProvider` + `GateResolutionHook` and slots `RemediationPlanPanel` into the panel registry), `v3/F29-advanced-policy-engine` (registers a `policy_override` `GateContextProvider` projecting conditional matches into "Risks flagged" and emits `policy_override` gates via the F36 primitive), and `v3/F31-deployment-gates` (the env-promotion control plane that produces `deploy` gates and registers the real deploy resolution hook, setting `subject_type='deployment'`).

---

## 6. Acceptance criteria (numbered, testable)

1. `ApprovalService.create` succeeds for **all six** `GateType` values, persisting an `approval_request` with correct `gate_type`, `subject_type/id`, `workspace_id`, and a secret-redacted `gate_payload`, and emits exactly one `approval.requested` event.
2. Creating a second gate of the same `(subject_type, subject_id, gate_type)` while one is `pending` returns the **existing** request (idempotent), enforced by `uq_pending_gate`; no duplicate row, no second event.
3. `GET /approvals/{id}/context` returns an `ApprovalContext` built by the **registered provider** for that gate type; populated sections match the gate (pr has diff+verification+traceability; `policy_override` has the attempted action + blocking rules + risk flags; deploy has target env + diff), and unpopulated sections are `None`/empty.
4. `available_actions` are gate-correct: pr/spec/plan/deploy offer `approve|reject|request_changes`; `incident_remediation`/`policy_override` additionally offer `escalate`.
5. An **agent** or **system** principal (`actor.kind != "user"`) is refused (`403`) on any `resolve`, for every gate type.
6. A **viewer** is refused (`403`) on any `resolve`; a **member** can resolve `spec/plan/pr/deploy/incident_remediation` (subject to per-gate checks) but is refused (`403`) on `policy_override`; an **admin** can resolve `policy_override`.
7. **No self-approval:** a user principal whose id equals the `agent`/user that produced the run is refused; and when `forbid_self_approval` is set, the run author cannot approve their own gate.
8. For a `pr` gate, `ApprovalAuthorizer` enforces the repo `review_rules` (e.g. `approval_required_for_merge`, `min_approvals`); a member without satisfying reviewer rules is refused.
9. `resolve(approve)` persists exactly one `approval_decision` row per approver (unique constraint), sets `approval_request.status=approved`, `resolved_at`, `resolver_user_id`, and emits one `approval.resolved` event.
10. `resolve` invokes the registered `GateResolutionHook.on_resolved` for the gate type; the returned `ResolutionOutcome` (incl. `blocking_reasons`, `follow_up_state`) is folded into the `ApprovalResolution` response.
11. **Approve-but-blocked:** when a hook returns `completed=false` with `blocking_reasons` (e.g. pr CI red), the gate status is `approved` but the response carries the reasons, **no** `review_approved` FSM event is emitted, and the subject's workflow does **not** advance to merged — mirroring F08's behavior exactly (regression-locked against `v1/F08-plan-execute-verify-pr-approval`'s approve-but-CI-red scenario, F08 §2 / AC#11, e.g. `blocking_reasons: ["CI status is failure (1 of 3 checks)"]`).
12. `request_changes` sets status `changes_requested` and the hook routes the subject workflow to drafting/`needs_human_input`; `reject` sets `rejected` and routes to `cancelled`/denied — both audited.
13. `escalate` keeps the gate `pending`, raises `risk_level` to `critical`, and raises the required resolving role to `admin`; subsequent member resolves are refused.
14. A **policy_override** approve mints a single `policy_override_grant` bound to the exact `action_fingerprint`, and `PolicyOverrideGate.consume(agent_run_id, fingerprint)` returns `True` **once** then `False` (single-use); an expired grant returns `False`; a fingerprint mismatch returns `False`.
15. A **deploy** gate for a `restricted_environment`/`allow_agent_deploy=false` action carries a `restricted_env` risk flag; approve emits `deploy.approved`; the agent's original deploy action is never executed by F36 itself.
16. **Tenant isolation:** `get`/`get_context`/`resolve` for an `approval_id` in another workspace return `404` (not `403` — no existence leak); `list`/`count` only ever return the actor's workspace.
17. **Audit:** every `create`, decision, and resolution emits a redacted `AuditEvent` to the injected F39 `AuditSink` with actor (user/agent id) + timestamp + payload hash; `gate_payload` and rendered context are passed through the F37 `SecretRedactor` before persist/audit/emit (no value matching the secret pattern survives in the audit row or emitted event).
18. The **inbox** (`GET /approvals?mine=true`) returns only gates the actor is authorized to resolve, sorted with `critical` risk first; `GET /approvals/count` matches the inbox length; filters (`gate_type`, `project_id`, `status`) work.
19. `GateRequirementResolver.is_required` returns the spec defaults: `pr`=True (unless policy relaxes), `spec`=True for feature-class, `plan`=`task.requires_approval.plan`, `deploy`=True for restricted env, `policy_override`=always True.
20. The Approval Review UI renders the nine must-show panels from `ApprovalContext`, hides empty sections, exposes the gate-correct actions, supports `a/r/x/e` shortcuts, and performs an optimistic resolve that **rolls back** and shows a blocking banner when the response reports `blocking_reasons`.

### 6.x Definition of Done
All ACs covered by passing tests; F08's pr-gate tests pass unchanged through the unified service (regression lock); a seeded integration test drives create → notify-event → authorize → resolve → hook for both a `pr` and a `policy_override` gate against real Postgres (testcontainer); coverage of `forge_approval` ≥ 80% (the `backend-tdd` bar Forge applies to itself).

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first. Layout: `packages/approval-sdk/tests/` (unit), `apps/api/tests/` (API), `apps/worker/tests/` (tasks/integration), `apps/web/__tests__/` (component).

**Key fixtures:**
- `make_request(gate_type, **over)` — factory building an `ApprovalRequest` for any gate type.
- `principal_admin / principal_member / principal_viewer / principal_agent / principal_system` — `Principal` variants.
- `fake_registry` — `GateRegistry` with recording fake `GateContextProvider`/`GateResolutionHook` per gate type; hooks return scripted `ResolutionOutcome` (incl. a "blocked" pr hook returning `blocking_reasons=["CI is failing"]`).
- `fake_policy_reader` — supplies `review_rules`/`deploy_rules` for authorizer tests.
- `fake_event_bus` (captures `approval.requested`/`approval.resolved`) / `fake_audit` (records `AuditSink` events, F39) / `fake_redactor` (asserts `SecretRedactor` was applied to `gate_payload`/context, F37).
- `pg` — Postgres testcontainer + migrations head, for repository + integration tests.

**Unit — authorizer (`tests/test_authorizer.py`):**
- `test_agent_and_system_never_resolve` — both refused for every gate; AC#5.
- `test_viewer_refused_all_gates`; `test_member_refused_policy_override`; `test_admin_allowed_policy_override`; AC#6.
- `test_no_self_approval_agent` / `test_no_self_approval_author_when_flag` — refused; AC#7.
- `test_pr_review_rules_enforced` — member without satisfying `review_rules` refused; AC#8.
- `test_deploy_permission_enforced` — restricted env requires deploy perm; AC#15.

**Unit — service create/resolve (`tests/test_service.py`):**
- `test_create_all_gate_types_emits_event_and_redacts` — AC#1, #17.
- `test_create_idempotent_on_pending_unique` — AC#2.
- `test_get_context_delegates_to_provider` — AC#3.
- `test_available_actions_per_gate` — AC#4.
- `test_resolve_persists_decision_sets_status_emits` — AC#9.
- `test_resolve_invokes_hook_and_folds_outcome` — AC#10.
- `test_approve_but_blocked_keeps_workflow` — hook `completed=false` → status approved, reasons returned; AC#11.
- `test_request_changes_and_reject_route` — AC#12.
- `test_escalate_raises_role_and_risk` — subsequent member refused; AC#13.
- `test_resolve_already_resolved_409` / `test_duplicate_vote_409`.

**Unit — requirements (`tests/test_requirements.py`):**
- `test_is_required_defaults_per_gate` — AC#19.

**Unit — policy-override grant (`tests/test_override_grant.py`):**
- `test_grant_single_use` — consume True then False; AC#14.
- `test_grant_expired_denies`; `test_fingerprint_mismatch_denies`; AC#14.

**Unit — provider/hook registry (`tests/test_registry.py`):**
- `test_missing_provider_raises`; `test_emit_only_gate_has_no_hook` (resolve returns `not_implemented` outcome gracefully).

**API tests (`apps/api/tests/test_approvals.py`):**
- `test_inbox_scoped_and_sorted` — critical first, only authorized gates; AC#18.
- `test_count_matches_inbox`; filter tests; AC#18.
- `test_cross_workspace_404` — AC#16.
- `test_decision_authz_matrix` — viewer 403, agent 403, member on policy_override 403, admin 200; AC#5,#6.
- `test_approve_blocked_returns_reasons` — **mirrors F08 AC#11** (approve while CI red → status `approved`, `outcome.completed=False`, `blocking_reasons` returned, no `review_approved` emitted); regression lock; AC#11.
- `test_context_has_nine_sections_for_pr` — AC#3.

**Integration — full flow (`apps/worker/tests/test_approval_flow.py`, Postgres + fakes):**
- `test_pr_gate_create_to_merged` — create pr gate → `approval.requested` → admin/member approve → pr hook merges → `approval.resolved` + audit; AC#1,9,10,17.
- `test_policy_override_grant_consumed_once` — create override gate → admin approve → grant minted → `consume` once True then False; AC#14.
- `test_expire_sweeper_marks_expired` — past `expires_at` → `expired` + event; routes subject to needs_human_input.

**Frontend (`apps/web/__tests__/approvals.test.tsx`, React Testing Library):**
- `test_inbox_renders_and_filters` — rows, gate badges, risk marker, filters; AC#18.
- `test_shell_renders_nine_panels_and_hides_empty` — pr shows all; override hides verification/traceability; AC#20.
- `test_actions_per_gate` — escalate only for incident/override; AC#4,#20.
- `test_optimistic_resolve_rollback_on_block` — optimistic approve rolls back + shows blocking banner; AC#11,#20.
- `test_keyboard_shortcuts` — `a/r/x/e` fire correct actions; AC#20.

---

## 8. Security & policy considerations

- **Agents never approve (Build-Prompt #2).** `ApprovalAuthorizer` rejects any non-`user` principal for every gate, server-side — independent of UI. The agent-runner identity that produced a run can never resolve a gate on that run.
- **Human approval is structural, not advisory (Build-Prompt #5).** Merge/deploy/remediation/override side effects run **only** inside a registered `GateResolutionHook` invoked after authorization; there is no "force" endpoint. `pr` merge remains gated by F08's `review_approved AND ci_green AND spec_validated`.
- **Least privilege per gate.** `policy_override` is admin-only; `deploy` requires environment-specific deploy permission from `deploy_rules`; `pr` requires repo `review_rules`. Escalation raises the required role to admin and cannot be down-graded.
- **Single-use, non-broadening overrides.** A `policy_override_grant` is bound to an exact `action_fingerprint`, short-TTL, and consumed atomically once — it permits one specific call and never expands the agent's standing scope.
- **Tenant isolation.** All queries filter by `workspace_id`; cross-workspace access returns `404` (no existence leak). `list`/`count` never cross workspaces.
- **Secret redaction.** `gate_payload`, `context_ref` snapshots, and rendered `ApprovalContext` pass through the secret-redaction filter before persistence, audit, event emission, or transmission.
- **Immutable audit.** Every create, decision, escalation, and resolution emits a redacted `AuditEvent` to the canonical `AuditSink` (`cross-cutting/F39-audit-log`, `audit_log` table with per-workspace hash chain) with actor + timestamp + payload hash (Security §"Audit log"). The `approval_decision` table is itself append-only (F39 `attach_immutability_trigger`), one-per-approver (unique), giving a tamper-evident multi-approver trail.
- **Idempotency & double-action safety.** Create is idempotent on the pending-unique index; resolve is idempotent per approver and rejects already-resolved gates (`409`); the decision endpoint is per-user rate-limited to prevent accidental double action.
- **Consistent authorization across surfaces.** Slack/API/UI all call the same `ApprovalService.resolve`, so the no-self-approval and RBAC rules cannot be bypassed by choosing a different surface.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — a cross-cutting slice that introduces a new shared package, migrates a foundational table, generalizes an existing v1 implementation (F08), and ships a new top-level UI plus the two gate primitives (deploy, policy_override) no other slice owns. Rough split: `forge_approval` core (service/authorizer/registry/requirements) (M), data-model migration + repository (S/M), deploy + policy_override providers/hooks + grant (M), API surface (S/M), unified Approval UI inbox + shell + panel registry (M), F08 re-homing + regression lock (M).

**Key risks:**
1. **Re-homing F08 without behavior drift.** Generalizing F08's pr `ApprovalService`/review page is the riskiest change. *Mitigation:* port F08's approval tests verbatim and run them through the unified service as a regression gate (AC#11); keep the pr hook/provider as the exact F08 logic, only relocated.
2. **Naming reconciliation (`kind`→`gate_type`).** The foundation baseline, F07, and F08 all use the `kind` column (values `spec|plan|pr|deploy`); post-F36 slices (F17, F31) reference `gate_type`. *Mitigation:* a single F36 migration renames the column once, and F07's `approval_granted:<kind>` guard string is reconciled to read `gate_type`; one source of truth thereafter. (F31 is v3 and authored against the F08-era `kind`/`deployment_id` shape — it is re-pointed at `gate_type` + `subject_type='deployment'` when it lands; that reconciliation is F31's, not F36's.)
3. **Registry wiring / startup order.** Providers/hooks must be registered before requests are resolved. *Mitigation:* a single composition-root `bootstrap/gate_registry.py`; missing providers degrade to read-only with an explicit `not_implemented` outcome rather than crashing.
4. **Producers in later phases (deploy v3, policy_override F29 v3).** The gate primitives ship now but their producing workflows land later. *Mitigation:* ship the gate + provider + hook + tests with synthetic triggers; document the create call-site the producers will use (the `consume`/`is_required`/`create` contracts are frozen here).
5. **Multi-approver scope creep.** `required_approvals>1` aggregation is tempting to over-build. *Mitigation:* schema + decision rows support it; V1 resolution logic is single-decisive-vote and explicitly defers aggregation (see §12).

---

## 10. Key files / paths (exact)

**packages/approval-sdk/forge_approval/**
- `models.py`, `registry.py`, `authorizer.py`, `requirements.py`, `service.py`, `events.py`, `repository.py`, `redaction.py`
- `providers/deploy.py`, `providers/policy_override.py`
- `tests/test_authorizer.py`, `tests/test_service.py`, `tests/test_requirements.py`, `tests/test_override_grant.py`, `tests/test_registry.py`

**packages/db/forge_db/models/**
- `approval.py` (canonical `approval_request`, `approval_decision`, `policy_override_grant`)

**packages/db/migrations/versions/**
- `<rev>_f36_approval_framework.py` (rename `kind`→`gate_type`, add columns, new tables, replace pr-only unique index with `uq_pending_gate`, backfill)

**apps/api/app/**
- `routers/approvals.py` (generic gate-agnostic surface; absorbs F08 `/approvals*`)
- `services/approval_service.py` (app adapter constructing `forge_approval.ApprovalService`)
- `bootstrap/gate_registry.py` (composition root registering all providers/hooks)
- `schemas/approval.py`, `models/approval.py` (re-export)
- `tests/test_approvals.py`

**apps/worker/forge_worker/tasks/**
- `approvals.py` (`expire_pending_approvals`, `consume_override_grant`)
- `tests/test_approval_flow.py`

**apps/web/**
- `app/(dashboard)/approvals/page.tsx` (inbox), `app/(dashboard)/approvals/[approvalId]/page.tsx` (review shell)
- `components/approvals/{ApprovalShell,ApprovalInbox,GoalPanel,ConfidencePanel,RiskFlagsPanel,RunTraceEmbed,KnowledgeProvenancePanel,ActionBar,DeployPlanPanel,PolicyOverridePanel}.tsx`, `components/approvals/panel-registry.ts`
- `lib/api/approvals.ts`
- `app/(dashboard)/projects/[projectId]/tasks/[taskId]/review/page.tsx` (refactor → redirect to `/approvals/{id}`)
- `__tests__/approvals.test.tsx`

**deploy/**
- `docker-compose.yml` (Celery beat entry for `expire_pending_approvals`)

---

## 11. Research references (relevant links from the spec/research report)

- Human Approval System — Approval Gate Types table (six gates) + "Approval UI Must Show" nine items: `docs/FORGE_SPEC.md` §Human Approval System.
- Core Design Principle #2 "Human-in-the-loop by design" + Build-Prompt constraints #2 (agent never self-assigns scope) and #5 (human approval before merge — always): `docs/FORGE_SPEC.md` §Core Design Principles, §Build Prompt.
- Workflow Engine — `spec_review`/`plan_review`/`awaiting_review`/`awaiting_approval` states, `escalation_policy` (`on_low_confidence`, `on_policy_conflict: escalate_to_admin`), Workflow DSL `record: approval_event`: `docs/FORGE_SPEC.md` §Workflow Engine.
- Repo Policy — `review_rules` (`required_reviewers`, `approval_required_for_merge`, `min_approvals`) and `deploy_rules` (`allow_agent_deploy`, `restricted_environments`): `docs/FORGE_SPEC.md` §Repo Policy System.
- Skill Profiles — `human_review_required`, `requires_human_approval_before_action`, `max_blast_radius`: `docs/FORGE_SPEC.md` §Skill Profiles.
- Task Schema — `requires_approval {spec, plan, pr, deploy}`, `handoff_rules.on_missing_spec_approval: block`: `docs/FORGE_SPEC.md` §Task Schema.
- Security — RBAC roles (admin/member/viewer/agent-runner), immutable audit log, secret redaction, per-workspace isolation, rate limiting: `docs/FORGE_SPEC.md` §Security.
- LangGraph human-in-the-loop interrupts (pause/resume at gates — the pattern the agent-side override consumption mirrors): https://langchain-ai.github.io/langgraph/ ; https://www.youtube.com/watch?v=4F8wvpb8JkI
- Spec Gating Rules ("Approval UI must show requirement-to-diff and requirement-to-test traceability"): `docs/FORGE_SPEC.md` §Spec Gating Rules.
- Open SWE — approvals/actions surfaced in Slack with a single resolution path (research report §"Symphony and Open SWE: What to Borrow"): https://github.com/langchain-ai/open-swe

---

## 12. Out of scope / future

- **Gate-specific content computation** — spec traceability + merge gate (F08), runbook/blast-radius modeling (F17), conditional policy evaluation (F04/F29). F36 owns the frame; these slices provide the providers/hooks.
- **The FSM, its guards/effects, and the `approval_granted:<gate>` predicate** — owned by F07. F36 only standardizes the column the guard reads and supplies the `create_*_approval` effect bodies' target service.
- **Multi-approver aggregation (`required_approvals > 1`, CODEOWNERS-routed reviewers)** — the schema (`approval_decision`, `required_approvals`) supports it; V1 resolves on a single decisive vote. Quorum aggregation + reviewer routing is a fast-follow.
- **Env-promotion execution for the `deploy` gate** — F36 ships the gate primitive + authorization + `deploy.approved` event; the actual promotion workflow is v3 (Deployment gates / environment promotion). F36 does not deploy anything.
- **The conditional policy engine that emits `policy_override` gates** — F29 (v3). F36 provides the gate, the admin-only authorization, and the single-use grant; F29 decides *when* to raise one.
- **SLA escalation policies / auto-approval** — beyond the simple `expires_at` sweep, time-based auto-escalation chains and any auto-approval are explicitly out (every gate requires a human decision in V1).
- **Approval delegation / out-of-office reassignment** — future board/RBAC feature; not modeled here.
- **Per-gate notification routing** — owned by F16 (Slack) / email; F36 only emits `approval.requested`/`approval.resolved`.
