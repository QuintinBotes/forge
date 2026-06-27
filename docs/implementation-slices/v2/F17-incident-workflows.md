# F17 — Incident Workflows & Postmortem Generation

> Phase: v2 · Spec module(s): Workflow Engine (Incident Workflow States), Skill Profiles (`incident-response`), Human Approval System (Incident remediation gate), Native Project Board (`incidents` entity, follow-up Task generation), Knowledge & MCP layers (context gathering), Integration Layer V2 (PagerDuty/Datadog/Sentry/Grafana alert ingest) · Status target: **Done** = an inbound alert (webhook or manual) creates an `Incident`, opens a Postgres-FSM `WorkflowRun` driven by a new `incident` workflow definition through `alert_received → incident_created → context_gathering → impact_assessed → remediation_proposed → awaiting_approval → executing_runbook → monitoring → resolved → postmortem_created → closed`; every remediation action is gated by the `incident-response` skill profile (read-only/low-blast-radius by default, `deploy_prod`/`delete_data`/`modify_access_controls` structurally forbidden) **and** a human approval before any mutating step; on resolution a postmortem document is generated and its action items become real board Tasks linked to the incident; every transition, tool call, approval, and runbook step is in the immutable audit log; and the whole surface is exercised against recorded fixtures with zero live network calls. Lint + types + `pytest` green on `packages/workflow-engine`, `packages/board-core`, `packages/integration-sdk`, the `apps/api` incident router, and the `apps/worker` incident tasks.

---

## 1. Intent — what & why

The spec defines a first-class **Incident Workflow** distinct from the feature workflow:

```
alert_received -> incident_created -> context_gathering -> impact_assessed
-> remediation_proposed -> awaiting_approval -> executing_runbook
-> monitoring -> resolved -> postmortem_created
```

and a dedicated `incident-response` skill profile that is deliberately the most-restricted profile Forge ships:

```yaml
incident-response:
  requires_plan: false
  requires_human_approval_before_action: true
  max_blast_radius: low
  allowed_actions: [read_logs, query_metrics, read_repo, run_diagnostic_scripts]
  forbidden_actions: [deploy_prod, delete_data, modify_access_controls]
```

This slice makes that workflow real and adds **postmortem task generation** (Phase 2 roadmap item: *"Incident workflows and postmortem task generation"*). It is the incident-response analogue of F08 (the feature implementation arc): it owns the incident-specific FSM definition, guards, effects, the alert-ingest boundary, the runbook execution + blast-radius gate, recovery monitoring, and the postmortem composer that closes the loop into the board as follow-up work.

Why it is its own slice and not part of F07:
- F07 explicitly scoped the incident workflow out: *"Incident workflow (V2) — `alert_received → … → postmortem_created` is a separate definition loaded by the same DSL parser; not built here."* F17 supplies that definition and the guard/effect bodies it references.
- The incident path's safety posture is fundamentally different from the feature path: the agent operates on **live production telemetry**, not a worktree, and its default is **read-only diagnosis**. The structural enforcement that the agent can never `deploy_prod` / `delete_data` / `modify_access_controls`, and that **no** mutating remediation runs without a human approval, is the entire reason the workflow is modeled as explicit states with gates rather than a free-running agent.

What F17 reuses vs. owns:
- **Reuses (does not rebuild):** the F07 Postgres FSM (`WorkflowDefinition`/`TransitionRule`/`load_definition`, `GuardRegistry`/`EffectRegistry`, `PostgresWorkflowEngine`, `WorkflowEffectDispatcher`, the `workflow_run`/`workflow_transition` tables + transition algorithm + audit), the F11 `incident-response` `SkillProfile` → `SkillDirectives` projection and `skill_permits_action`, the **F36** canonical approval framework (`ApprovalService.create/resolve`, the `incident_remediation` `GateType`, the unified Approval UI shell + panel registry — F17 only registers the gate's provider/hook; the `pr` gate is the analogous F08 precedent), the F06 agent runtime entrypoint, the F05 hybrid retriever, the F09 `MCPGatewayClient.query()`, the F37 `SecretRedactor` + `require_role` RBAC + encrypted secrets vault, the F39 central `audit_log` + `attach_immutability_trigger`, and the F01 `incidents` entity + board Task creation + `activity_events` timeline.
- **Owns:** the `incident` workflow definition (`incident.yaml`), the incident state/event vocabulary additions, incident guards/effects, the incident domain (alert ingest + dedup, runbook model, recovery monitoring, postmortem composer, follow-up task generation), the incident REST + alert-webhook surface, the incident Celery tasks, and the incident UI (incident detail, timeline, remediation approval, postmortem view).

---

## 2. User-facing behavior / journeys

**Journey A — Alert to incident (machine + on-call).**
1. PagerDuty/Datadog/Sentry/Grafana posts to `POST /integrations/alerts/{provider}/webhook` (signature-verified). The alert is normalized to an `IncidentAlert` and deduped by `dedup_key`. A new `Incident` is created (or the alert is attached to an existing open incident with the same dedup key), severity mapped (`sev1..sev4`), and a `WorkflowRun(definition="incident")` is started in `alert_received`. The on-call (PagerDuty assignee → incident commander) and the incident Slack channel are notified (F16).
2. The run auto-advances `alert_received → incident_created` (`acknowledge_and_notify`). The board shows the incident under the project with a live lifecycle badge.

**Journey B — Automated read-only diagnosis.**
3. `context_gathering`: an incident-response agent run (F06, skill `incident-response`) gathers **read-only** context — repo context + hybrid knowledge (F05), recent logs/metrics via MCP query-through (F09; Datadog/Sentry/Grafana), and policy-approved diagnostic scripts. Each finding lands on the incident timeline with provenance. The agent **cannot** mutate anything — the skill allowlist is `read_logs/query_metrics/read_repo/run_diagnostic_scripts`.
4. `impact_assessed`: the agent (or a human) records an `ImpactAssessment` — blast radius, affected services, user impact, and a severity recommendation. The board may bump severity.

**Journey C — Propose, approve, remediate.**
5. `remediation_proposed`: the agent proposes a **runbook** — an ordered list of steps, each with a declared `blast_radius` and rationale. The proposal is read-only data; nothing executes yet.
6. `awaiting_approval`: an `ApprovalRequest(gate_type="incident_remediation")` is created (always required — `incident-response.requires_human_approval_before_action = true`). The approval UI shows the runbook, the gathered context, the impact assessment, and a per-step blast-radius badge. Any step exceeding `max_blast_radius=low` (or naming a forbidden action) is flagged **blocked**; the plan cannot be approved as-is and must be edited or escalated to admin via an explicit policy override.
7. On **Approve**, `executing_runbook`: steps run in order through the runbook executor. Each step is re-checked at execution time against the skill directives **and** the repo policy **and** the blast-radius cap — a defense-in-depth re-evaluation, not a one-time check. A step that fails routes the run to remediation re-proposal (retry budget) or `needs_human_input`.

**Journey D — Recover, resolve, learn.**
8. `monitoring`: a recovery monitor watches the recovery signals (metric thresholds returning to healthy, or explicit human confirmation). On `recovery_confirmed` → `resolved`; on `recovery_failed` → back to `remediation_proposed` (budget remaining) or `needs_human_input`.
9. `resolved → postmortem_created`: a postmortem document is generated from the full incident timeline (detection, diagnosis, impact, remediation steps, resolution) and stored. Its **action items become board Tasks** (kind `bug`/`chore`) linked back to the incident, assigned to the suggested owners. The postmortem view renders the doc and links the generated tasks.
10. `postmortem_created → closed`: a human closes the incident; follow-up Tasks live on as normal board work.

**Journey E — Escalation / abort.**
- At any non-terminal state, `cancel` → `cancelled` and `fail` → `failed` (reused F07 universal edges). A forbidden/over-blast remediation proposal escalates to admin (`needs_human_input` + admin approval request). `resume` from `needs_human_input` returns to the paused-from state.

**Journey F — Manual incident.**
- A member declares an incident from the board (`POST /incidents`) without an external alert; the same FSM runs from `incident_created` (skipping `alert_received`).

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One additive Alembic migration `packages/db/migrations/versions/<rev>_f17_incident_workflows.py` in the single shared Alembic history (chains on F07's `workflow_run` migration, F01's `incidents` migration, and F36's approval-framework migration). SQLAlchemy 2.x models live in `packages/board-core/src/board_core/models/` (incident domain) and `packages/db/forge_db/models/workflow.py` (baseline `workflow_run` from `cross-cutting/F00-foundation`; FSM columns from F07; F17 extends it). The migration is reversible.

**Extend `incidents` (F01 table — additive columns):**

| column | type | notes |
|---|---|---|
| `lifecycle_state` | `VARCHAR(32)` NULL | denormalized mirror of the incident `WorkflowRun.current_state` for board filtering; source of truth is the run |
| `source` | `VARCHAR(32)` NOT NULL default `'manual'` | `manual` \| `pagerduty` \| `datadog` \| `sentry` \| `grafana` \| `webhook` |
| `dedup_key` | `VARCHAR(256)` NULL | normalized provider dedup key; partial unique among open incidents (see below) |
| `commander_id` | `UUID` FK→`users.id` NULL | incident commander (PagerDuty assignee or board owner) |
| `blast_radius` | `VARCHAR(16)` NULL | assessed blast radius (`low`\|`medium`\|`high`), reuses F11 `BlastRadius` |
| `impact_summary` | `TEXT` NULL | from the latest `ImpactAssessment` |
| `detected_at` | `TIMESTAMPTZ` NULL | first alert time |
| `acknowledged_at` | `TIMESTAMPTZ` NULL | `incident_created` time |
| `resolved_at` | `TIMESTAMPTZ` NULL | `resolved` time |
| `postmortem_id` | `UUID` FK→`postmortems.id` NULL | set on `postmortem_created` |

Partial unique index `uq_incidents_open_dedup` on `(workspace_id, dedup_key)` WHERE `dedup_key IS NOT NULL AND lifecycle_state NOT IN ('resolved','postmortem_created','closed','cancelled')` — at most one **open** incident per dedup key (the dedup→attach rule).

**Extend `workflow_run` (F07 table — additive columns):**

| column | type | notes |
|---|---|---|
| `incident_id` | `UUID` FK→`incidents.id` NULL | set for `definition_name='incident'` runs |
| `recovery_retry_count` | `INTEGER` NOT NULL default `0` | monitoring→remediation loop budget |

- Make `task_id` **nullable** and add CHECK `ck_workflow_run_subject`: exactly one of `task_id`/`incident_id` is non-null.
- Add partial unique index `uq_workflow_run_active_incident` on `(incident_id)` WHERE `current_state NOT IN ('resolved','postmortem_created','closed','failed','cancelled')` — at most one live incident run per incident (mirrors F07's active-task index).
- Widen the `current_state` CHECK constraint to include the new incident states (see §4 enum additions).

**New table `incident_alerts`** (raw inbound alerts; idempotency + audit, modeled on F03's `github_webhook_deliveries`):

| column | type | notes |
|---|---|---|
| `id` | `UUID` PK | |
| `workspace_id` | `UUID` FK→`workspace.id` | tenant scope |
| `incident_id` | `UUID` FK→`incidents.id` NULL | set after create/attach |
| `provider` | `VARCHAR(32)` NOT NULL | `pagerduty`\|`datadog`\|`sentry`\|`grafana`\|`manual` |
| `external_id` | `VARCHAR(256)` NULL | provider event id |
| `delivery_id` | `VARCHAR(256)` NULL | webhook delivery id (dedup) |
| `dedup_key` | `VARCHAR(256)` NOT NULL | |
| `severity` | `VARCHAR(8)` NOT NULL | `sev1..sev4` |
| `title` | `TEXT` NOT NULL | |
| `payload_hash` | `VARCHAR(64)` NOT NULL | sha256 of redacted raw payload (raw not persisted in full — secret-redaction rule) |
| `status` | `VARCHAR(16)` NOT NULL | `received`\|`created_incident`\|`attached`\|`skipped`\|`error` |
| `received_at` | `TIMESTAMPTZ` NOT NULL | |

Partial unique `uq_incident_alerts_delivery` on `(provider, delivery_id)` WHERE `delivery_id IS NOT NULL` (webhook idempotency).

**New table `incident_events`** (structured incident timeline; append-only):

| column | type | notes |
|---|---|---|
| `id` | `UUID` PK | |
| `incident_id` | `UUID` FK→`incidents.id` | indexed |
| `workflow_run_id` | `UUID` FK→`workflow_run.id` NULL | |
| `sequence` | `INTEGER` NOT NULL | monotonic per incident |
| `kind` | `VARCHAR(32)` NOT NULL | `state_change`\|`context_finding`\|`impact`\|`remediation_proposed`\|`runbook_step`\|`approval`\|`recovery_check`\|`note` |
| `actor` | `VARCHAR(128)` NOT NULL | `system`\|`user:<uuid>`\|`agent:<uuid>` |
| `summary` | `TEXT` NOT NULL | one-line human summary |
| `data` | `JSONB` NOT NULL default `{}` | **secret-redacted** structured detail (finding/step/metric snapshot) |
| `created_at` | `TIMESTAMPTZ` NOT NULL | |

`UNIQUE(incident_id, sequence)`. Insert-only (enforced in repository). A redacted **summary** of each incident event is also mirrored to F01 `activity_events` (`entity_type='incident'`) via `/internal/activity-events` so the board timeline shows it.

**New table `remediation_plans`** (the proposed/approved runbook):

| column | type | notes |
|---|---|---|
| `id` | `UUID` PK | |
| `incident_id` | `UUID` FK→`incidents.id` | indexed |
| `workflow_run_id` | `UUID` FK→`workflow_run.id` | |
| `agent_run_id` | `UUID` FK→`agent_run.id` NULL | the run that proposed it |
| `attempt` | `INTEGER` NOT NULL | 1-based; matches `recovery_retry_count + 1` |
| `max_blast_radius` | `VARCHAR(16)` NOT NULL | max across steps |
| `status` | `VARCHAR(16)` NOT NULL | `proposed`\|`approved`\|`rejected`\|`executing`\|`succeeded`\|`failed` |
| `steps` | `JSONB` NOT NULL | list of `RunbookStep` (see §4) |
| `created_at` | `TIMESTAMPTZ` NOT NULL | |

**New table `postmortems`:**

| column | type | notes |
|---|---|---|
| `id` | `UUID` PK | |
| `incident_id` | `UUID` FK→`incidents.id` unique | one postmortem per incident |
| `workspace_id` | `UUID` FK→`workspace.id` | |
| `status` | `VARCHAR(16)` NOT NULL default `'draft'` | `draft`\|`published` |
| `content_md` | `TEXT` NOT NULL | rendered markdown body |
| `content_hash` | `VARCHAR(64)` NOT NULL | sha256 |
| `storage_uri` | `TEXT` NULL | MinIO key `postmortems/{incident_key}/v{n}.md` |
| `root_cause` | `TEXT` NULL | |
| `data` | `JSONB` NOT NULL | the structured `Postmortem` model (timeline, factors, lessons) |
| `created_by` | `UUID` FK→`users.id` NULL | null when agent-generated |
| `created_at`/`updated_at` | `TIMESTAMPTZ` | |

**New table `postmortem_action_items`** (the generated follow-up work):

| column | type | notes |
|---|---|---|
| `id` | `UUID` PK | |
| `postmortem_id` | `UUID` FK→`postmortems.id` | indexed |
| `task_id` | `UUID` FK→`tasks.id` NULL | the board Task created from this item |
| `title` | `TEXT` NOT NULL | |
| `description` | `TEXT` NOT NULL | |
| `kind` | `VARCHAR(16)` NOT NULL | `bug`\|`chore` |
| `priority` | `VARCHAR(8)` NOT NULL | board `Priority` |
| `owner_hint` | `VARCHAR(128)` NULL | |
| `created_at` | `TIMESTAMPTZ` NOT NULL | |

`approval_request` is the **F36** canonical gate table. `incident_remediation` is already a first-class value of F36's `GateType` enum (F17 does **not** add it). The incident gate is created with `subject_type='incident'`, `subject_id=<incident_id>`, and `workflow_run_id=<incident run>` (so F07's `approval_granted:incident_remediation` guard resolves). The one-pending-gate-per-subject constraint is F36's generic `uq_pending_gate (subject_type, subject_id, gate_type) WHERE status='pending'` — F17 adds **no** approval index of its own.

### 3.2 Backend (FastAPI routes + services/packages)

**Workflow wiring — `packages/workflow-engine/forge_workflow/` (mirrors F07 layout):**

```
forge_workflow/
├── states.py                      # EXTEND: add incident states/events to the shared enums (§4)
├── incident/
│   ├── __init__.py
│   ├── guards.py                  # incident guards registered into GuardRegistry (§4)
│   ├── effects.py                 # incident effect NAMES + registration into EffectRegistry
│   └── registry.py                # default_incident_registries() = feature builtins + incident guards/effects
└── definitions/
    └── incident.yaml              # the incident WorkflowDefinition (§4) — packaged with the wheel
```

The same `PostgresWorkflowEngine` from F07 drives incident runs — it reads `definition_name` from the run and looks up the definition. F17 registers incident guards/effects so `load_definition("incident.yaml", default_incident_registries())` validates.

**Incident domain — `packages/board-core/src/board_core/incidents/`:**

```
incidents/
├── schemas.py        # re-exports forge_contracts.incident DTOs
├── alert.py          # AlertNormalizer + dedup_key derivation; create-or-attach logic
├── service.py        # IncidentService — create/get/list, ingest alert, record events, lifecycle reads
├── runbook.py        # Runbook validation + blast-radius rollup; RunbookExecutor Protocol
├── recovery.py       # RecoveryMonitor Protocol + ThresholdRecoveryMonitor (deterministic V1)
├── postmortem.py     # PostmortemComposer Protocol + TemplatePostmortemComposer (deterministic V1)
├── approval.py       # incident_remediation GateContextProvider + GateResolutionHook (registered into F36's GateRegistry)
├── actions.py        # follow-up Task creation via board-core task service (F01)
├── repository.py     # IncidentRepository, RemediationPlanRepository, PostmortemRepository (append-only events)
└── errors.py         # IncidentNotFound, DuplicateAlert, BlastRadiusExceeded, RunbookStepError
```

**API — `apps/api/forge_api/routers/incidents.py`** (Phase-0 stub → real handlers) + `apps/api/forge_api/routers/alerts.py` (new). All routes auth-required except alert webhooks (signature-verified instead). Base prefix `/api/v1`.

| Method & path | Handler | RBAC | Returns |
|---|---|---|---|
| `POST /incidents` | `declare_incident(body: IncidentDeclareRequest)` | member+ | `IncidentDTO` 201 (manual incident; starts FSM at `incident_created`) |
| `GET /incidents` | `list_incidents(project_id?, state?, severity?)` | viewer+ | `list[IncidentDTO]` |
| `GET /incidents/{id}` | `get_incident` | viewer+ | `IncidentDetailDTO` (incident + run state + latest plan + impact) |
| `GET /incidents/{id}/timeline` | `incident_timeline` | viewer+ | `list[IncidentEventDTO]` |
| `POST /incidents/{id}/events` | `send_incident_event(body: WorkflowEvent)` | member+ (human-gate events member/admin only; `system`/`agent` only via service token) | `IncidentDTO` (proxies to the FSM engine) |
| `GET /incidents/{id}/remediation` | `get_remediation_plan` | viewer+ | `RemediationPlanDTO` |
| `GET /incidents/{id}/postmortem` | `get_postmortem` | viewer+ | `PostmortemDTO` (incl. linked Task keys) |
| `POST /incidents/{id}/postmortem/publish` | `publish_postmortem` | member+ | `PostmortemDTO` |
| `POST /integrations/alerts/{provider}/webhook` | `ingest_alert` | signature only | `202` |
| `POST /integrations/alerts/manual` | `ingest_manual_alert(body: ManualAlertRequest)` | member+ | `IncidentDTO` 201 |

The remediation **approval** is resolved through the **F36** canonical surface (`POST /approvals/{id}/decision {approve|reject|escalate}`, `gate_type='incident_remediation'`). F17 does **not** modify F36's `ApprovalService`; instead it registers an `incident_remediation` `GateResolutionHook` (via the `bootstrap/gate_registry.py` composition root) whose `on_resolved(...)` emits the FSM event: approve → `remediation_approved` (`awaiting_approval → executing_runbook`), reject → `remediation_rejected` (`→ needs_human_input`), escalate → re-target the gate to admin (raise `risk_level`, required role admin). No separate approval endpoint is added.

Error mapping reuses F07's handlers (`InvalidTransitionError`→409 with `allowed_events`, `GuardFailedError`→409, etc.) plus F17: `DuplicateAlert`→200 (attached, idempotent), `BlastRadiusExceeded`→409 with the offending step ids, `IncidentNotFound`→404.

`IncidentService`, `RunbookExecutor`, `RecoveryMonitor`, `PostmortemComposer`, and the agent port are wired in `apps/api/forge_api/deps.py` / the worker container; tests inject deterministic doubles.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

**`apps/worker/forge_worker/tasks/incident.py`** (Celery, dedicated queue `incident`). These are the **effect bodies** the `incident.yaml` definition dispatches by name through the F07 `CeleryEffectDispatcher`; each calls back into the FSM via `deliver_event` with an `idempotency_key` on completion (same pattern as F07/F08).

- `acknowledge_and_notify(run_id)` — set `acknowledged_at`, notify commander + Slack channel (F16), emit `incident_acknowledged` (auto-advances `incident_created → context_gathering`).
- `gather_incident_context(run_id)` — start an F06 agent run with skill `incident-response`; the agent uses **read-only** tools: `read_repo`, `search_knowledge` (F05 `HybridRetriever.retrieve`), `query_mcp` (F09 `MCPGatewayClient.query` for Datadog/Sentry/Grafana logs+metrics), `run_diagnostic_scripts` (policy-allowed, sandboxed). Persists `incident_events(kind='context_finding')`; emits `context_gathered`.
- `assess_incident_impact(run_id)` — agent/heuristic produces `ImpactAssessment`; persists impact; updates `incidents.blast_radius`/`impact_summary`; emits `impact_assessed`.
- `propose_remediation(run_id)` — agent proposes a `Runbook`; validates each step's `blast_radius` and action against the resolved `incident-response` `SkillDirectives` via `assert_runbook_within_policy`; persists a `remediation_plans` row (`status=proposed`, `attempt = recovery_retry_count + 1`); emits `remediation_proposed` when the plan is within posture, or `remediation_blast_radius_exceeded` when any step is over `max_blast_radius=low` or names a forbidden action (the guards on both outgoing edges re-validate as defense in depth).
- `bump_recovery_retry(run_id)` — increments `workflow_run.recovery_retry_count`; runs on the two re-propose edges **before** `propose_remediation` so `attempt` reflects the new try and the next guard check sees the consumed budget.
- `create_remediation_approval(run_id)` — calls F36 `ApprovalService.create(gate_type='incident_remediation', subject_type='incident', subject_id=<incident_id>, workflow_run_id=<run>)`; notifies approvers (F16); **no auto-advance** (waits for human).
- `escalate_remediation_to_admin(run_id)` — when a proposal exceeds `max_blast_radius` or names a forbidden action: route to `needs_human_input`, create an admin `policy_override` gate (F36), set `failure_reason`.
- `execute_runbook(run_id)` — loads the approved plan; for each step in order, re-checks `skill_permits_action(directives, ToolCall) AND repo_policy.Decision AND step.blast_radius <= directives.max_blast_radius`; executes via `RunbookExecutor`; persists `incident_events(kind='runbook_step')`; on all-success emits `runbook_completed`, on failure emits `runbook_step_failed`.
- `start_recovery_monitoring(run_id)` — schedules `check_recovery` (Celery `apply_async(countdown=...)`); on healthy emits `recovery_confirmed`, on still-degraded after the window emits `recovery_failed`.
- `mark_resolved(run_id)` — set `resolved_at`; notify; emit `postmortem_requested`.
- `generate_postmortem(run_id)` — `PostmortemComposer.compose(...)` over the incident timeline → `Postmortem`; persist `postmortems` row + MinIO snapshot; set `incidents.postmortem_id`; create follow-up board Tasks from action items (via `actions.create_action_item_tasks`); persist `postmortem_action_items` with `task_id`. This effect emits **no** further FSM event — the run rests in `postmortem_created` awaiting the human `close`, which is guard-gated on the postmortem being persisted (`postmortem_persisted`).

`check_recovery(run_id)` is a separate Celery task (re-enqueues itself up to a bounded number of windows) using `RecoveryMonitor.check_recovery`.

**LangGraph:** the agent loop itself (LangGraph StateGraph, tool registry) is owned by F06; F17 only invokes F06's entrypoint with the `incident-response` skill and a curated read-only/low-blast tool set, and consumes the typed result. No new graph is authored here.

### 3.4 Frontend / UI (Next.js routes/components)

**App `apps/web/`** (App Router, shadcn/ui, TanStack Query):

- Route `app/[workspace]/projects/[projectKey]/incidents/page.tsx` — incident list (TanStack Table): key (`CORE-INC4`), title, severity, lifecycle state, commander, age.
- Route `app/[workspace]/incidents/[incidentKey]/page.tsx` — incident detail with tabs:
  - **Overview** — `IncidentLifecycleBadge` (renders the 10 states + error states with live SSE updates), severity, commander, impact summary, blast radius.
  - **Timeline** — `IncidentTimeline` (merged `incident_events` + board `activity_events`): context findings, impact, runbook steps, approvals, recovery checks.
  - **Remediation** — `RemediationPlanPanel`: ordered steps with per-step `BlastRadiusBadge`; blocked/forbidden steps flagged; with a deep link to the F36 Approval Review shell for the pending `incident_remediation` gate. The Approve / Reject / **Escalate** actions live in F36's unified `ApprovalShell`, not here.
  - **Postmortem** — `PostmortemView` (rendered markdown) + `ActionItemsList` linking the generated board Tasks (deep links to F01 task detail).
- Components in `apps/web/components/incidents/`: `IncidentLifecycleBadge`, `IncidentTimeline`, `RemediationPlanPanel` (also registered into F36's approval `panel-registry` under `incident_remediation` as the gate's central must-show panel), `BlastRadiusBadge`, `PostmortemView`, `ActionItemsList`, `DeclareIncidentDialog`.
- Data via TanStack Query hooks (`useIncident`, `useIncidentTimeline`, `useRemediationPlan`, `usePostmortem`). Approval resolution + optimistic update/rollback are owned by F36's Approval Shell. Live updates via the F01 SSE board stream scoped to `entity_type=incident`.

F17 supplies the `incident_remediation` `GateContextProvider` so F36's review shell renders the nine "Approval UI Must Show" items for the gate: incident goal + impact assessment, the runbook (per-step blast radius) in the central panel, gathered context findings (knowledge/telemetry provenance), confidence, risk flags (blocked/forbidden steps), and the full incident trace.

### 3.5 Infra / deploy (compose, helm, caddy)

- **No new compose service.** Reuses `db`, `redis`, `worker`, `api`, `minio`, `mcp-gateway`, `caddy`. Add a dedicated Celery queue `incident` to the `worker` command so long-running diagnosis/monitoring doesn't starve feature/verification/indexing queues (`deploy/docker-compose.yml`).
- **MinIO bucket** `forge-postmortems` (versioned postmortem snapshots) added to `deploy/scripts/install.sh` and `make setup` bucket bootstrap.
- **Alert webhook routing:** `POST /integrations/alerts/{provider}/webhook` must be reverse-proxied to `api` **without** body buffering/rewrite (signatures are over exact raw bytes — same constraint as F03's GitHub webhook). Add a Caddyfile matcher comment asserting no `request_body` transform on this path.
- **New env** (`deploy/.env.example` + `.env.production.example`): `PAGERDUTY_WEBHOOK_SECRET`, `DATADOG_WEBHOOK_SECRET`, `SENTRY_WEBHOOK_SECRET`, `GRAFANA_WEBHOOK_SECRET` (per-provider signing secrets; absent provider → that webhook returns 501 Not Configured), `INCIDENT_RECOVERY_WINDOW_SECONDS=300`, `INCIDENT_RECOVERY_MAX_WINDOWS=6`. (The remediation retry budget is **not** an env var — it is `retry_policy.max_retries: 2` in the bundled `incident.yaml`, the source of truth the `recovery_retry_*` guards read.)
- The alert-provider MCP/connector sources (Datadog/Sentry/Grafana) are registered through the existing F09 MCP connection model (read-only query-through) — no new gateway code.
- Helm: N/A for V1; V2 Helm chart (separate slice) gains the `incident` worker queue + the `forge-postmortems` bucket values when it lands. F17 documents the values it needs.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Enum additions — `forge_workflow/states.py` (additive to the shared F07 enums; CHECK constraint widened in the migration):**

```python
class WorkflowState(str, Enum):
    # ... existing feature states (F07) ...
    # --- incident states (F17) ---
    alert_received = "alert_received"
    incident_created = "incident_created"
    context_gathering = "context_gathering"
    impact_assessed = "impact_assessed"
    remediation_proposed = "remediation_proposed"
    awaiting_approval = "awaiting_approval"
    executing_runbook = "executing_runbook"
    monitoring = "monitoring"
    resolved = "resolved"
    postmortem_created = "postmortem_created"
    # reused shared: closed, needs_human_input, failed, cancelled

class WorkflowEventType(str, Enum):
    # ... existing feature events (F07) ...
    # --- incident events (F17) ---
    alert_ingested = "alert_ingested"
    incident_acknowledged = "incident_acknowledged"
    context_gathered = "context_gathered"
    impact_assessed = "impact_assessed"
    remediation_proposed = "remediation_proposed"
    remediation_blast_radius_exceeded = "remediation_blast_radius_exceeded"
    remediation_approved = "remediation_approved"
    remediation_rejected = "remediation_rejected"
    runbook_completed = "runbook_completed"
    runbook_step_failed = "runbook_step_failed"
    recovery_confirmed = "recovery_confirmed"
    recovery_failed = "recovery_failed"
    postmortem_requested = "postmortem_requested"
    # reused shared: close, resume, fail, cancel

INCIDENT_TERMINAL_STATES = frozenset({WorkflowState.closed, WorkflowState.failed, WorkflowState.cancelled})
```

`HUMAN_GATE_EVENTS` (F07) is extended with `remediation_approved`, `remediation_rejected` (member/admin actor only).

**Incident DTOs / models — `packages/contracts/forge_contracts/incident.py`** (Pydantic v2; reuses `BlastRadius` from `forge_contracts.skill` (F11) and `Priority`/`TaskKind` from `forge_contracts` (F01)):

```python
from enum import StrEnum
from datetime import datetime
from uuid import UUID
from typing import Literal, Protocol
from pydantic import BaseModel, Field

class IncidentSeverity(StrEnum):
    SEV1 = "sev1"; SEV2 = "sev2"; SEV3 = "sev3"; SEV4 = "sev4"

class AlertProvider(StrEnum):
    PAGERDUTY = "pagerduty"; DATADOG = "datadog"; SENTRY = "sentry"
    GRAFANA = "grafana"; MANUAL = "manual"; WEBHOOK = "webhook"

class IncidentAlert(BaseModel):
    provider: AlertProvider
    external_id: str | None = None
    delivery_id: str | None = None
    dedup_key: str
    title: str
    severity: IncidentSeverity
    service: str | None = None
    repo_id: str | None = None
    description: str | None = None
    received_at: datetime
    # raw provider payload is NOT carried beyond normalization (secret redaction)

class ContextFinding(BaseModel):
    kind: Literal["log", "metric", "repo", "knowledge", "mcp", "diagnostic"]
    summary: str
    source: str                      # e.g. "mcp://datadog/...", "github.com/org/api:app/x.py"
    refs: list[str] = Field(default_factory=list)

class ImpactAssessment(BaseModel):
    blast_radius: "BlastRadius"
    affected_services: list[str] = Field(default_factory=list)
    user_impact: str
    severity_recommendation: IncidentSeverity

class RunbookStep(BaseModel):
    id: str
    order: int
    title: str
    action: str                      # canonical ToolCall.name semantic action (F11 KNOWN_ACTIONS)
    args: dict = Field(default_factory=dict)
    blast_radius: "BlastRadius"
    rationale: str
    status: Literal["proposed","approved","skipped","running","succeeded","failed"] = "proposed"

class Runbook(BaseModel):
    incident_id: UUID
    attempt: int
    steps: list[RunbookStep]
    max_blast_radius: "BlastRadius"  # = max(step.blast_radius)
    proposed_by_agent_run_id: UUID | None = None

class StepResult(BaseModel):
    step_id: str
    status: Literal["succeeded","failed","skipped"]
    summary: str
    output_ref: str | None = None    # MinIO key for full output (redacted)

class RecoveryStatus(BaseModel):
    recovered: bool
    healthy_signals: list[str] = Field(default_factory=list)
    degraded_signals: list[str] = Field(default_factory=list)

class ActionItem(BaseModel):
    title: str
    description: str
    kind: Literal["bug","chore"] = "chore"
    priority: str = "medium"         # board Priority value
    owner_hint: str | None = None

class PostmortemTimelineEntry(BaseModel):
    at: datetime
    summary: str

class Postmortem(BaseModel):
    incident_id: UUID
    summary: str
    timeline: list[PostmortemTimelineEntry]
    root_cause: str
    contributing_factors: list[str] = Field(default_factory=list)
    resolution: str
    lessons_learned: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
```

**Protocols — `forge_contracts/incident.py`** (implemented by board-core / F06 wiring; doubles in tests):

```python
class IncidentAgentPort(Protocol):
    """Backed by the F06 agent runtime with skill_profile='incident-response'."""
    async def gather_context(self, *, incident_id: UUID, repo_id: str | None,
                             knowledge_scope: dict) -> list[ContextFinding]: ...
    async def assess_impact(self, *, incident_id: UUID,
                            findings: list[ContextFinding]) -> ImpactAssessment: ...
    async def propose_remediation(self, *, incident_id: UUID, attempt: int,
                                  assessment: ImpactAssessment,
                                  findings: list[ContextFinding]) -> Runbook: ...

class RunbookExecutor(Protocol):
    async def execute_step(self, step: RunbookStep, *, incident_id: UUID,
                           directives: "SkillDirectives", policy: "RepoPolicy") -> StepResult: ...

class RecoveryMonitor(Protocol):
    async def check_recovery(self, *, incident_id: UUID,
                             assessment: ImpactAssessment) -> RecoveryStatus: ...

class PostmortemComposer(Protocol):
    def compose(self, *, incident: "IncidentSnapshot",
                events: list["IncidentEventDTO"],
                plans: list[Runbook]) -> Postmortem: ...

class AlertAdapter(Protocol):
    """integration-sdk: maps a provider webhook to a normalized IncidentAlert."""
    def verify(self, *, secret: str, body: bytes, headers: dict[str, str]) -> bool: ...
    def normalize(self, *, body: bytes, headers: dict[str, str]) -> IncidentAlert: ...
```

**Blast-radius safety helper (`board_core/incidents/runbook.py`) — the structural guarantee:**

```python
BLAST_ORDER = {"low": 0, "medium": 1, "high": 2}

def assert_runbook_within_policy(
    runbook: Runbook, directives: "SkillDirectives",
) -> list[str]:
    """Return the ids of steps that VIOLATE the incident-response posture (empty == OK):
      - step.action (or an alias) in directives.forbidden_actions
      - directives.allowed_actions non-empty and step.action not covered
      - BLAST_ORDER[step.blast_radius] > BLAST_ORDER[directives.max_blast_radius]
    Reuses F11 skill_permits_action + ACTION_ALIASES. Pure, no I/O.
    A non-empty result MUST block plan approval and route to escalate_remediation_to_admin."""
```

**Deterministic V1 implementations (test doubles + offline build):**
- `ThresholdRecoveryMonitor` — recovers when configured healthy-signal predicates hold; deterministic, no network in tests.
- `TemplatePostmortemComposer` — assembles a `Postmortem` from the incident timeline using `spec-templates/postmortem.md.j2` (detection → diagnosis → impact → remediation steps → resolution → action items). No LLM. The real LLM-backed composer (incident-response/spec-analyst skill) plugs in behind `PostmortemComposer`.
- `AlertAdapter` impls: `PagerDutyAlertAdapter`, `DatadogAlertAdapter`, `SentryAlertAdapter`, `GrafanaAlertAdapter` in `packages/integration-sdk/forge_integrations/alerts/`, each with a `verify`/`normalize` pair and recorded-fixture tests (F03 fixtures-only pattern).

**Canonical bundled definition — `forge_workflow/definitions/incident.yaml`:**

```yaml
name: incident
version: "1"
modes:
  default: single_agent

retry_policy:          # remediation retry loop (monitoring -> remediation)
  max_retries: 2
  backoff: exponential
  initial_delay_seconds: 60

escalation_policy:
  confidence_threshold: 0.72
  on_low_confidence: pause_and_notify
  on_policy_conflict: escalate_to_admin

transitions:
  - {from: alert_received,        on: alert_ingested,        to: incident_created,     effects: [acknowledge_and_notify]}
  - {from: incident_created,      on: incident_acknowledged, to: context_gathering,    effects: [gather_incident_context], skill: incident-response}
  - {from: context_gathering,     on: context_gathered,      to: impact_assessed,      effects: [assess_incident_impact],  skill: incident-response}
  - {from: impact_assessed,       on: impact_assessed,       to: remediation_proposed, effects: [propose_remediation],     skill: incident-response}
  - {from: remediation_proposed,  on: remediation_proposed,  to: awaiting_approval,    guards: [remediation_within_blast_radius], effects: [create_remediation_approval]}
  - {from: remediation_proposed,  on: remediation_blast_radius_exceeded, to: needs_human_input, guards: [remediation_exceeds_blast_radius], effects: [escalate_remediation_to_admin], priority: 1}
  - {from: awaiting_approval,     on: remediation_approved,  to: executing_runbook,    guards: [approval_granted:incident_remediation], effects: [execute_runbook], record: approval_event}
  - {from: awaiting_approval,     on: remediation_rejected,  to: needs_human_input,    record: approval_event}
  - {from: executing_runbook,     on: runbook_completed,     to: monitoring,           effects: [start_recovery_monitoring]}
  - {from: executing_runbook,     on: runbook_step_failed,   to: remediation_proposed, guards: [recovery_retry_remaining], effects: [bump_recovery_retry, propose_remediation], skill: incident-response, priority: 1}
  - {from: executing_runbook,     on: runbook_step_failed,   to: needs_human_input,    guards: [recovery_retry_exhausted], effects: [pause_and_notify]}
  - {from: monitoring,            on: recovery_confirmed,    to: resolved,             effects: [mark_resolved]}
  - {from: monitoring,            on: recovery_failed,       to: remediation_proposed, guards: [recovery_retry_remaining], effects: [bump_recovery_retry, propose_remediation], skill: incident-response, priority: 1}
  - {from: monitoring,            on: recovery_failed,       to: needs_human_input,    guards: [recovery_retry_exhausted], effects: [pause_and_notify]}
  - {from: resolved,             on: postmortem_requested,  to: postmortem_created,    effects: [generate_postmortem]}
  - {from: postmortem_created,    on: close,                 to: closed,               guards: [postmortem_persisted]}
  - {from: needs_human_input,     on: resume,                to: __paused_from__}
  - {from: needs_human_input,     on: cancel,                to: cancelled}
  # universal cancel/fail edges injected by the engine for every non-terminal state (F07)
```

The engine injects `cancel`/`fail` edges and resolves `__paused_from__` exactly as in F07; the `incident` definition is loaded by the same `load_definition` parser/validator. The two re-propose edges run `bump_recovery_retry` before `propose_remediation`, so `recovery_retry_count` is consumed once per failed remediation and `attempt = recovery_retry_count + 1` numbers each `remediation_plans` row; the `recovery_retry_remaining`/`recovery_retry_exhausted` guards compare the pre-bump value against `retry_policy.max_retries` (the source of truth for the budget).

**Incident guards — `forge_workflow/incident/guards.py` (registered into the shared `GuardRegistry`):**

```python
#   approval_granted:incident_remediation -> latest ApprovalRequest(run, gate_type=incident_remediation) == approved
#   remediation_within_blast_radius       -> assert_runbook_within_policy(latest plan, directives) == []
#   remediation_exceeds_blast_radius      -> assert_runbook_within_policy(...) != []   (negation)
#   recovery_retry_remaining              -> run.recovery_retry_count < retry_policy.max_retries
#   recovery_retry_exhausted              -> run.recovery_retry_count >= retry_policy.max_retries
#   postmortem_persisted                  -> incidents.postmortem_id IS NOT NULL  (close cannot fire before the postmortem exists)
```

**REST request/response models — `apps/api/forge_api/schemas/incidents.py`:** `IncidentDeclareRequest{project_id, title, severity, repo_id?, commander_id?}`, `ManualAlertRequest`, `IncidentDTO`, `IncidentDetailDTO`, `IncidentEventDTO`, `RemediationPlanDTO`, `PostmortemDTO{...; action_item_task_keys: list[str]}`.

---

## 5. Dependencies — features/slices that must exist first

Hard (build-time) dependencies:

- **`cross-cutting/F00-foundation`** — REQUIRED (referenced variously as `cross-cutting/F00-foundation` / `cross-cutting/F00-platform-foundation` / `v1/F00-foundation-substrate` across sibling slices; reconcile when the foundation slice lands). Supplies `packages/contracts`, `packages/db` base session + `TimestampMixin` + the baseline `workflow_run`/`approval_request`/`users`/`workspace` tables + the Alembic baseline this migration stacks on, the RBAC role scaffold, and the MinIO `ArtifactStore`.
- **`cross-cutting/F37-auth-secrets-byok`** — REQUIRED. The `Principal` + `require_role(...)` RBAC dependency every incident route is gated by, the canonical `SecretRedactor` (telemetry/log/postmortem redaction), the encrypted secrets vault (provider tokens, BYOK model keys consumed by the F06 incident agent), and "no anonymous API access".
- **`v1/F07-feature-workflow-fsm`** — REQUIRED. The Postgres FSM that F17 loads the `incident` definition into: `WorkflowDefinition`/`TransitionRule`/`load_definition`, `GuardRegistry`/`EffectRegistry`, `PostgresWorkflowEngine`, `WorkflowEffectDispatcher`/`CeleryEffectDispatcher`/`NullEffectDispatcher`, the `workflow_run`/`workflow_transition` tables + transition algorithm + audit, the shared `WorkflowState`/`WorkflowEventType` enums F17 extends, the `approval_granted:<gate>` guard family, and the `/workflow/runs/{id}/events` mechanics the incident router proxies.
- **`v1/F01-project-board`** — REQUIRED. The `incidents` entity (F17 extends it; `key=CORE-INC<number>`, `severity` enum), `tasks` + the board task-creation service (postmortem follow-up Tasks), `activity_events` + `/internal/activity-events` (timeline mirror, `entity_type=incident`), the SSE board stream, and `Priority`/`TaskKind` enums.
- **`v1/F11-skill-profiles`** — REQUIRED. The `incident-response` `SkillProfile`, `to_directives` → `SkillDirectives` (`approval_before_action`, `max_blast_radius`, `allowed_actions`, `forbidden_actions`), `skill_permits_action` + `ACTION_ALIASES`, `KNOWN_ACTIONS`, and `BlastRadius`. F17's blast-radius safety helper is a thin composition over these.
- **`cross-cutting/F36-human-approval-system`** — REQUIRED. The canonical approval framework: the generalized `approval_request` table with `gate_type` (incl. the first-class `incident_remediation` value) + `uq_pending_gate` index, `ApprovalService.create/resolve`, the `GateRegistry` (F17 registers the `incident_remediation` `GateContextProvider` + `GateResolutionHook`), the `/approvals/*` REST surface, and the unified Approval UI shell + `panel-registry` F17 slots `RemediationPlanPanel` into. (F08's `pr` gate is the analogous precedent re-homed into F36.)
- **`v1/F06-single-execution-agent`** — REQUIRED (stubbable). The agent runtime entrypoint + sandbox runner that backs `IncidentAgentPort`/`RunbookExecutor`. F17 ships deterministic doubles so it builds/tests without the real agent.

Soft (integration-time) dependencies — F17 degrades gracefully if absent:

- **`v1/F05-hybrid-knowledge-retrieval`** — context gathering uses `HybridRetriever.retrieve`; absent → repo/log/metric findings only.
- **`v1/F09-mcp-gateway-v1`** — live logs/metrics via `MCPGatewayClient.query` (Datadog/Sentry/Grafana as read-only MCP sources); absent → no live telemetry findings.
- **`v1/F16-slack-notifications`** — commander/channel/approval notifications, and approve/reject-from-Slack (which calls the same F36 `ApprovalService.resolve`); F17 emits the events regardless and degrades to email/in-app.
- **`cross-cutting/F39-audit-log`** — the central immutable `audit_log` + the reusable `attach_immutability_trigger(table)` helper that `incident_events` adopts for DB-level append-only (until then, append-only is enforced in `IncidentRepository`, mirroring F07's `workflow_transition`).
- **`v1/F02-spec-engine`** — the postmortem composer reuses the artifact-snapshot/MinIO + Jinja-template pattern (not a hard import); `spec-templates/postmortem.md.j2` lives alongside the spec templates.
- **Provider alert adapters** — there is no dedicated alert-integrations slice in the current plan, so F17 **owns** the four `AlertAdapter` impls in `packages/integration-sdk/forge_integrations/alerts/`. They may later migrate to a connector marketplace (`v3/F32-integration-marketplace`).

Downstream consumers (depend on F17, not prerequisites): `cross-cutting/F38-observability-cost-metrics` and `v1/F12-eval-harness` (incident MTTR, time-to-acknowledge, remediation approval accept/reject metrics, agent model cost per incident).

---

## 6. Acceptance criteria (numbered, testable)

1. `load_definition("incident.yaml", default_incident_registries())` parses into a `WorkflowDefinition` with the 10 incident states reachable; validation **fails loudly** for an unknown state, an unregistered incident guard/effect, or a non-terminal state with no outgoing rule (reuses F07's `DSLValidationError`).
2. Ingesting an `IncidentAlert` (webhook or manual) creates an `Incident` (severity mapped from the provider), starts a `WorkflowRun(definition="incident")` in `alert_received`, auto-advances to `incident_created`, and writes the `created→…` `workflow_transition` rows plus an `incident_alerts` row with `status='created_incident'`.
3. A second alert with the same `dedup_key` while an incident is open does **not** create a new incident — it attaches (`incident_alerts.status='attached'`), enforced by `uq_incidents_open_dedup`; the endpoint returns 200 idempotently.
4. A webhook with a missing/invalid provider signature returns 401 and performs no DB write; a duplicate `delivery_id` is skipped (`uq_incident_alerts_delivery`) and returns 202 without re-processing.
5. The happy path drives `alert_received → incident_created → context_gathering → impact_assessed → remediation_proposed → awaiting_approval → executing_runbook → monitoring → resolved → postmortem_created → closed` given the corresponding events with passing guards, producing ordered `workflow_transition` rows with strictly increasing `sequence`.
6. `context_gathering` runs an agent with skill `incident-response`; the resolved `SkillDirectives.allowed_actions == {read_logs, query_metrics, read_repo, run_diagnostic_scripts}` and any attempt to use `deploy_prod`/`delete_data`/`modify_access_controls` during the run is denied by `skill_permits_action` (deny, `requires_approval=True`, `severity="critical"`).
7. `remediation_proposed → awaiting_approval` only when `remediation_within_blast_radius` holds (`assert_runbook_within_policy` returns `[]`); a proposal containing a step with `blast_radius='high'` or a forbidden action takes the `remediation_blast_radius_exceeded` edge to `needs_human_input` with `escalate_remediation_to_admin`, and the offending step ids are recorded.
8. `awaiting_approval` does **not** advance to `executing_runbook` without an approved `ApprovalRequest(gate_type='incident_remediation')`; with no approval it raises `GuardFailedError(failed_guards=["approval_granted:incident_remediation"])` and state is unchanged — i.e. **no mutating remediation runs without a human approval** (enforces `requires_human_approval_before_action`).
9. `POST /approvals/{id}/decision {approve}` on an incident gate is rejected (403) for `viewer` and for the `agent-runner` identity that produced the run; accepted for `member`/`admin`; on approve the FSM transitions to `executing_runbook` and `execute_runbook` is dispatched once.
10. `execute_runbook` re-checks **every** step at execution time against `skill_permits_action AND repo policy AND blast-radius cap` (defense in depth); a step that would violate the posture is not executed and the run routes to escalation even if the plan was previously approved (e.g. policy changed) — verified by mutating the policy between approval and execution.
11. A runbook step failure with retry budget remaining returns to `remediation_proposed` (re-propose), increments `recovery_retry_count`; with the budget exhausted it routes to `needs_human_input`.
12. `monitoring` emits `recovery_confirmed` when `RecoveryMonitor.check_recovery().recovered` is true → `resolved`; `recovery_failed` re-proposes remediation (budget remaining) or escalates (exhausted).
13. `resolved → postmortem_created` generates a `Postmortem` from the incident timeline, persists a `postmortems` row (+ MinIO snapshot + `content_hash`), and the postmortem includes a non-empty timeline, root cause, and ≥1 action item for the seeded scenario.
14. Postmortem action items become **real board Tasks**: for each `ActionItem`, a `tasks` row (kind `bug`/`chore`) is created in the incident's project via the board service, a `postmortem_action_items` row links `task_id`, and `GET /incidents/{id}/postmortem` returns the created task keys.
15. `cancel`/`fail` from any non-terminal incident state move to `cancelled`/`failed`; `resume` from `needs_human_input` returns to `paused_from_state`; events with no rule return `InvalidTransitionError` with `allowed_events`.
16. Every incident transition, agent tool call, runbook step, approval decision, and recovery check writes to the immutable audit log (`workflow_transition` + append-only `incident_events`); `incident_events` rows are never updated/deleted by the service, and `data`/`summary` are secret-redacted (no log/metric value matching the secret pattern survives).
17. Tenant isolation: cross-workspace access to `/incidents/{id}` returns 404 (not 403); all reads/writes filter by `workspace_id`; an incident `WorkflowRun` has exactly one of `task_id`/`incident_id` set (CHECK enforced).
18. The migration `upgrade`/`downgrade` runs clean against the Postgres test container; the widened `workflow_run.current_state` CHECK accepts the incident states and the partial unique indexes **F17 owns** (`uq_incidents_open_dedup`, `uq_workflow_run_active_incident`, `uq_incident_alerts_delivery`) reject their respective duplicates at the DB level. (One-pending-`incident_remediation`-gate-per-incident is F36's `uq_pending_gate`, not F17's.)
19. `assert_runbook_within_policy` is pure and total: a Hypothesis property test over random `Runbook`s × the `incident-response` directives always returns a `list[str]` and never raises; it returns non-empty for any step with a forbidden action or `blast_radius > low`.
20. The bundled `forge_workflow/definitions/incident.yaml` and the community `examples/workflows/incident.yaml` parse to equal `WorkflowDefinition` objects (drift guard), mirroring F07's `default_feature.yaml` drift test.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Framework: `pytest` + `pytest-asyncio`. Unit tests use SQLite in-memory (JSON columns) + `NullEffectDispatcher` + deterministic doubles + a `FakeClock`; integration tests use the Postgres test container + Celery eager mode + recorded alert fixtures (no live network). Write tests first; no module is "done" until `ruff check`, types, and `pytest` are green for the touched packages.

**Key fixtures (`packages/board-core/tests/conftest.py`, `packages/workflow-engine/tests/conftest.py`):**
- `incident_definition` — `load_definition(incident.yaml, default_incident_registries())`.
- `incident_engine(session, dispatcher, clock)` — `PostgresWorkflowEngine` with `NullEffectDispatcher`.
- `incident_directives` — `to_directives(incident-response profile)` (from F11 fixtures).
- `seeded_incident(session, **flags)` — Incident + Project + Workspace + commander.
- `fake_alert(provider, dedup_key, severity)` + recorded provider webhook bodies under `tests/fixtures/alerts/{pagerduty,datadog,sentry,grafana}.json`.
- `fake_incident_agent` — `IncidentAgentPort` returning scripted findings / impact / runbook (incl. a forbidden-action / high-blast variant).
- `fake_runbook_executor` — records executed steps; can be set to fail a given step.
- `fake_recovery_monitor` — scripted `RecoveryStatus` sequence.
- `template_postmortem_composer` — deterministic composer.
- `secret_payload` — an alert/log payload containing a fake API key (redaction assertion).

**Unit — definition & guards (`tests/test_incident_dsl.py`, `tests/test_incident_guards.py`):**
- `test_loads_incident_definition` + validation failures (unknown state / unregistered guard / unreachable state) (AC1).
- `remediation_within_blast_radius` / `remediation_exceeds_blast_radius` over an OK plan vs a forbidden-action plan vs a `blast_radius=high` plan (AC7).
- `approval_granted:incident_remediation` true/false against a seeded F36 gate (AC8).
- `recovery_retry_remaining`/`exhausted` at counts 0/2, and `bump_recovery_retry` increments `recovery_retry_count` exactly once per re-propose edge (AC11).
- `postmortem_persisted` false (no `postmortem_id`) blocks `close`; true allows it.

**Unit — blast-radius safety (`tests/test_runbook_policy.py`):**
- table-driven `assert_runbook_within_policy` cases: clean low-blast plan → `[]`; `deploy_prod` step → offending id; `delete_data` → offending id; `blast_radius=high` step → offending id; allowlist miss → offending id (AC7, AC10).
- `test_assert_runbook_within_policy_is_total` — Hypothesis over random runbooks × incident directives (AC19).

**Unit — alert normalization (`tests/test_alert_adapters.py`, integration-sdk):**
- per-provider `verify` (good/bad/missing signature) and `normalize` against recorded fixtures → expected `IncidentAlert` (severity mapping, dedup_key) (AC4).

**Unit — postmortem composer (`tests/test_postmortem.py`):**
- `TemplatePostmortemComposer.compose` over a seeded timeline → non-empty timeline/root_cause/≥1 action item; deterministic (same input → identical `content_hash`) (AC13).

**Unit — engine drive (`tests/test_incident_engine.py`):**
- `test_alert_creates_incident_and_acknowledges` (AC2).
- `test_dedup_attaches_not_creates` (AC3).
- `test_full_happy_path` — drives all states to `closed` (AC5).
- `test_blast_radius_gate` — within vs exceeds (AC7).
- `test_approval_required_before_execution` — no approval → `GuardFailedError`; approved → executes (AC8).
- `test_execute_runbook_rechecks_policy` — policy mutated post-approval → escalation (AC10).
- `test_step_failure_retry_then_escalate` — `FakeClock`; budget then exhaustion (AC11).
- `test_monitoring_recovery_paths` (AC12).
- `test_cancel_fail_resume` parametrized over non-terminal states (AC15).
- `test_incident_events_append_only_and_redacted` (AC16).

**Integration — Postgres + API (`apps/api/tests/test_incidents.py`, `apps/worker/tests/test_incident_flow.py`):**
- `test_migration_upgrade_downgrade` + partial-unique-index rejections (AC18).
- `test_webhook_signature_and_dedup` (AC4) — bad signature 401; duplicate delivery skipped.
- `test_alert_to_postmortem_end_to_end` — recorded alert → drive `advance` through the worker with fakes → `postmortem_created`; assert one incident, one postmortem, MinIO snapshot, follow-up Tasks created with `task_id` links and correct project (AC2,5,13,14,16).
- `test_approval_rbac` — viewer/agent-runner 403, member approves → executes (AC9).
- `test_cross_workspace_404` (AC17).
- `test_bundled_and_example_incident_dsl_match` (AC20).

**Frontend (`apps/web/__tests__/incident.test.tsx`, Vitest + RTL):**
- `IncidentLifecycleBadge` renders each state; `RemediationPlanPanel` flags blocked/forbidden steps; `IncidentApprovalActions` optimistic approve + rollback on block; `ActionItemsList` links generated task keys.

---

## 8. Security & policy considerations

- **Read-only-by-default agent (least privilege).** The `incident-response` skill directives are the floor for every incident agent run: positive allowlist `read_logs/query_metrics/read_repo/run_diagnostic_scripts` and hard deny `deploy_prod/delete_data/modify_access_controls`. The runtime executes a tool **iff** `skill_permits_action(...).allowed AND repo_policy.Decision.allowed` (F11 composition) — the incident agent can never widen its own scope (Build Prompt constraint #2). Context gathering is structurally incapable of mutating production.
- **No remediation without a human approval — always.** `requires_human_approval_before_action=true` is realized as a non-bypassable state: `awaiting_approval` can only be left via `approval_granted:incident_remediation` (member/admin actor; the producing `agent-runner` is forbidden from self-approving). Matches the spec Approval Gate "Incident remediation — Required for blast-radius > low" and the principle that risky actions are human-gated.
- **Blast-radius cap enforced twice.** `assert_runbook_within_policy` gates plan **approval** (proposals over `max_blast_radius=low` or naming a forbidden action are unapprovable and escalate to admin via explicit policy override), and `execute_runbook` **re-checks every step at execution time** against the (possibly newer) policy + directives — defense in depth so a stale approval or a policy change cannot execute an over-blast action.
- **Alert intake is untrusted.** Provider webhooks are signature-verified per provider secret over the exact raw bytes (no body buffering/rewrite at Caddy), deduped by `delivery_id`, and parsed with `yaml.safe_load`/strict JSON; an absent provider secret returns 501 (fail closed), a bad signature 401 with no DB write. Raw payloads are not persisted beyond a redacted hash.
- **Secret redaction on telemetry.** Logs/metrics/diagnostic output flow through F37's canonical `SecretRedactor` before landing in `incident_events.data`, the postmortem, MinIO, or the board timeline — satisfying "secrets stripped from logs, traces, and retrieval results"; runbook step output stored in MinIO is redacted and served via short-TTL signed URLs scoped to the workspace.
- **Immutable audit.** Every transition (`workflow_transition`, F07), tool call, runbook step, approval decision, and recovery check is append-only (`incident_events` never updated/deleted by the service; the F39 `attach_immutability_trigger` adds the DB-level guarantee once F39 lands) with actor recorded verbatim, and a redacted summary is fanned to F39's central `audit_log` — feeding the queryable audit log the Security section requires.
- **BYOK & spec-gating posture.** Incident agent runs (context gathering, impact, remediation proposal) and any future LLM-backed `PostmortemComposer` use the workspace's bring-your-own model keys via the F06 runtime + F37 vault — F17 introduces no separate key handling. Provider webhook signing secrets follow the F03 deployment-secret pattern (env, fail-closed when absent). Spec-gating (Build-Prompt constraint #4) is a feature-class control and does **not** apply to incident-class work, which has no spec/implementation arc; the equivalent non-negotiable control here is the mandatory human `incident_remediation` approval before any mutating step.
- **MCP least privilege.** Telemetry pull uses the F09 gateway in read-only query-through mode (write-blocked, RFC 8707 token binding, namespace scoping, per-call audit) — the incident path adds no new MCP write capability.
- **Tenant isolation & idempotency.** All reads/writes scoped by `workspace_id` (cross-workspace → 404). The `incident_id`/`task_id` exactly-one CHECK keeps the shared `workflow_run` table unambiguous. FSM idempotency keys + row locks (F07) prevent duplicate alert delivery or duplicate Celery callbacks from double-advancing an incident or double-executing a runbook step.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** It is the incident analogue of F08 and touches engine (new definition + guards/effects), board-core (incident domain), integration-sdk (alert adapters), api, worker, and web — but it **reuses** the F07 FSM, F11 skill gate, F36 approval framework, F06 agent, and F01 board, so the novel surface is the incident domain (alert/runbook/recovery/postmortem) and the safety composition, not new infrastructure. Rough split: incident domain + postmortem/action-items (M), workflow definition + guards/effects + worker tasks (M), alert adapters + webhook intake (S/M), api + F36 gate provider/hook registration (S/M), web (M).

| Risk | Severity | Mitigation |
|---|---|---|
| An incident agent mutating production (the catastrophic failure mode) | High | Allowlist + forbidden-action deny enforced in the F11 tool gate **and** re-checked per step at execution (AC6, AC10); no `deploy_prod`/`delete_data`/`modify_access_controls` tool is ever registered for the incident skill; integration test mutates policy post-approval to prove re-check |
| Stale approval executing an over-blast step | High | Execution-time re-evaluation of `assert_runbook_within_policy` (defense in depth), not a one-time approval check (AC10) |
| Alert dedup races / duplicate incidents | Medium | Partial unique `uq_incidents_open_dedup` + `uq_incident_alerts_delivery` + create-or-attach under row lock; idempotent webhook (AC3, AC4, AC18) |
| Recovery monitoring false "recovered" / flapping | Medium | Bounded recovery windows (`INCIDENT_RECOVERY_MAX_WINDOWS`), explicit healthy/degraded signal lists in `RecoveryStatus`, human `resume`/confirm path; `FakeClock` deterministic tests (AC12) |
| Incident state vocabulary widening the shared enum/CHECK | Medium | Additive enum + reversible migration that widens the CHECK; one engine/one table keeps F07's transition algorithm + audit reused (AC18); contract test that feature runs are unaffected |
| Postmortem quality without an LLM | Low | Deterministic `TemplatePostmortemComposer` is honest scaffolding (timeline/impact/steps assembled from real audit data); LLM composer plugs in behind the Protocol later; action-item extraction from a structured agent result, not free-form parsing |
| Provider webhook format drift | Low | Adapters are pure + recorded-fixture tested (F03 pattern); per-provider `verify`/`normalize`; absent secret → 501 |

---

## 10. Key files / paths (exact)

Create:
- `packages/workflow-engine/forge_workflow/incident/__init__.py`
- `packages/workflow-engine/forge_workflow/incident/guards.py`
- `packages/workflow-engine/forge_workflow/incident/effects.py`
- `packages/workflow-engine/forge_workflow/incident/registry.py`
- `packages/workflow-engine/forge_workflow/definitions/incident.yaml`
- `packages/board-core/src/board_core/incidents/{__init__,schemas,alert,service,runbook,recovery,postmortem,approval,actions,repository,errors}.py`
- `packages/board-core/src/board_core/models/{incident_alert,incident_event,remediation_plan,postmortem,postmortem_action_item}.py`
- `packages/integration-sdk/forge_integrations/alerts/{__init__,base,pagerduty,datadog,sentry,grafana}.py`
- `packages/contracts/forge_contracts/incident.py`
- `apps/api/forge_api/routers/alerts.py`
- `apps/api/forge_api/schemas/incidents.py`
- `apps/api/forge_api/services/incident_service.py`
- `apps/worker/forge_worker/tasks/incident.py`
- `packages/db/migrations/versions/<rev>_f17_incident_workflows.py`
- `spec-templates/postmortem.md.j2`
- `examples/workflows/incident.yaml`
- `apps/web/app/[workspace]/incidents/[incidentKey]/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/incidents/page.tsx`
- `apps/web/components/incidents/{IncidentLifecycleBadge,IncidentTimeline,RemediationPlanPanel,BlastRadiusBadge,IncidentApprovalActions,PostmortemView,ActionItemsList,DeclareIncidentDialog}.tsx`
- `apps/web/lib/incidents/{api,queries,mutations}.ts`
- Tests: `packages/workflow-engine/tests/{test_incident_dsl,test_incident_guards,test_incident_engine}.py`, `packages/board-core/tests/{test_runbook_policy,test_postmortem,test_alert_adapters}.py`, `apps/api/tests/test_incidents.py`, `apps/worker/tests/test_incident_flow.py`, `apps/web/__tests__/incident.test.tsx`

Edit (fill stubs / extend):
- `packages/workflow-engine/forge_workflow/states.py` (add incident states/events; extend `HUMAN_GATE_EVENTS`)
- `packages/db/forge_db/models/workflow.py` (extend `workflow_run`: `incident_id`, nullable `task_id`, CHECK, indexes, `recovery_retry_count`)
- `packages/board-core/src/board_core/models/incident.py` (extend `incidents` columns)
- `apps/api/forge_api/routers/incidents.py` (Phase-0 stub → real handlers)
- `apps/api/forge_api/bootstrap/gate_registry.py` (register the `incident_remediation` `GateContextProvider` + `GateResolutionHook` from `board_core.incidents.approval` into F36's `GateRegistry` — F36's composition root)
- `packages/workflow-engine/forge_workflow/pyproject.toml` (package-data include for `definitions/incident.yaml`)
- `deploy/docker-compose.yml` (`incident` worker queue), `deploy/scripts/install.sh` (`forge-postmortems` bucket), `deploy/.env.example` + `.env.production.example` (provider webhook secrets + recovery/retry knobs), `deploy/caddy/Caddyfile` (alert webhook no-buffer matcher)

---

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` → **Workflow Engine → Incident Workflow States** (the authoritative 10-state sequence reproduced in §4).
- `docs/FORGE_SPEC.md` → **Skill Profiles → `incident-response`** (`requires_human_approval_before_action`, `max_blast_radius: low`, allowed/forbidden actions — the structural safety contract).
- `docs/FORGE_SPEC.md` → **Human Approval System** → Approval Gate "Incident remediation — High-risk runbook step — Required for blast-radius > low" and the "Approval UI Must Show" 9-item list (reused for the remediation approval surface).
- `docs/FORGE_SPEC.md` → **Native Project Board** (entity hierarchy incl. `Incident`; postmortem follow-up Tasks land here) and **Task Schema** (`kind: incident`).
- `docs/FORGE_SPEC.md` → **Knowledge Sync Modes** (MCP query-through for always-fresh telemetry) and **MCP Integration → Security Rules** (read-only default, RFC 8707, audit) for context gathering.
- `docs/FORGE_SPEC.md` → **Integrations → V2** (Datadog, Sentry, PagerDuty, Grafana as the alert sources) and **Phase 2 roadmap** item "Incident workflows and postmortem task generation".
- `docs/FORGE_SPEC.md` → **Security** (least privilege, immutable audit, secret redaction, sandbox isolation) — the incident path's hardening requirements.
- `docs/forge-research-report.md` → **Workflow Engine** (Postgres FSM for V1 durable top-level workflows; LangGraph for agent-level routing) — F17 reuses the F07 FSM rather than introducing a new engine.
- `docs/forge-research-report.md` → **Symphony / Open SWE** (task-as-control-plane; structured task context over free-form prompting) — postmortem action items become first-class board Tasks, not chat.
- Sibling slices (contracts reused): `docs/implementation-slices/cross-cutting/F36-human-approval-system.md` (the `incident_remediation` gate + Approval UI), `cross-cutting/F37-auth-secrets-byok.md` (RBAC, `SecretRedactor`, vault, BYOK), `cross-cutting/F39-audit-log.md` (immutable audit + `attach_immutability_trigger`), `v1/F07-feature-workflow-fsm.md`, `v1/F11-skill-profiles.md`, `v1/F08-plan-execute-verify-pr-approval.md` (the analogous `pr` gate pattern), `v1/F06-single-execution-agent.md`, `v1/F05-hybrid-knowledge-retrieval.md`, `v1/F09-mcp-gateway-v1.md`, `v1/F01-project-board.md`; downstream: `cross-cutting/F38-observability-cost-metrics.md`.

---

## 12. Out of scope / future

- **Auto-remediation without human approval** — explicitly never in scope for `incident-response`; `requires_human_approval_before_action` is permanent for this profile. A future, separately-named skill profile with a relaxed posture (and its own policy override trail) could allow bounded auto-remediation, but it is not F17.
- **Temporal migration (V2 engine swap)** — F17's incident workflow runs on the F07 Postgres FSM behind the `WorkflowEngine` Protocol; when Temporal lands it inherits the same state vocabulary and guard semantics.
- **Real LLM postmortem authoring** — F17 ships the deterministic `TemplatePostmortemComposer`; the LLM-backed composer (incident-response/spec-analyst skill) plugs in behind `PostmortemComposer` later, same pattern as F02's `TemplateSpecGenerator`.
- **On-call scheduling / paging rotation** — F17 consumes PagerDuty alerts and notifies a commander; it does not own schedules/escalation policies (that is the PagerDuty integration's job).
- **Rich SLA/SLO breach automation** — F01 emits `sla_breached`; auto-declaring incidents from SLO burn-rate alerts is a fast-follow that would feed the same `IncidentAlert` intake.
- **Step-level partial-rollback / saga semantics for runbooks** — V1 runbook execution is sequential with failure→re-propose; compensating actions and partial rollback are future work.
- **Multi-service / cross-repo incidents** — F17 assumes a single primary `repo_id` for context; multi-repo incident context joins are V2+ (aligns with F08's single-repo V1 stance).
- **Incident analytics dashboards (MTTR, MTTA, remediation accept rate)** — F17 emits the audit/timeline data the observability + eval harness consumes; the dashboards themselves are the observability slice.
- **`sync_and_index` of telemetry into pgvector** — incident context uses live MCP query-through (F09); periodically indexing telemetry is the Phase-2 `mcp_sync_and_index` slice, not F17.
