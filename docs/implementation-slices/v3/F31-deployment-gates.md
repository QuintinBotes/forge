# F31 — Deployment Gates & Environment Promotion

> Phase: v3 · Spec module(s): Review & Approval Layer (Human Approval System → **Deploy approval** gate), Repo Policy Layer (`deploy_rules`), Workflow Engine (FSM/DSL reuse), Orchestrator (durable promotion runs, audit), Integration Layer (GitHub Deployments/Actions), Observability (immutable deployment audit) · Status target: **Done** = a project can define an **ordered environment pipeline** (e.g. `dev → staging → production`) with per-environment gate config; a human or agent can **request a promotion** of a specific `commit_sha`/artifact to an environment; the request is driven by a deterministic, Postgres-backed **deployment state machine** that (a) refuses to promote past a stage whose predecessor has not succeeded for the same artifact, (b) evaluates a **deployment gate** (policy `deploy_rules` + automated checks + freeze windows), (c) requires **human approval before any deploy to a restricted environment — always, with no policy relaxation possible**, (d) triggers the deploy through a pluggable `DeployProvider`, (e) runs a post-deploy **health/verification** check, (f) records the per-environment "currently deployed" state, and (g) auto- or manually **rolls back** to the last-good artifact on failure; every state change, gate decision, approval, deploy, health result, and rollback is written to an append-only audit trail; an agent can never self-approve or self-relax a deploy gate; and the whole slice is green under `ruff` + type check + `pytest` (engine unit suite, API integration suite, web component/e2e suite).

---

## 1. Intent — what & why

The spec lists **Deploy approval** as a first-class human gate ("Agent requests env promotion · Required unless policy relaxes for dev") and puts "**Deployment gates and environment promotion workflows**" in the Phase 3 roadmap. F08 built the generic `approval_request` primitive and the `pr` gate type, and explicitly deferred the `deploy` gate type to "a separate slice". F31 is that slice, and it goes beyond a single gate: it adds the **environment-promotion control plane** Forge has been missing.

Concretely F31 owns five things the spec describes but no prior slice implements:

1. **The environment pipeline model** — an ordered, per-project list of environments (`dev`, `staging`, `production`, …) with rank, restricted flag (derived from repo `deploy_rules.restricted_environments`), gate config (required checks, approver group, freeze windows, health check, deploy provider config, auto-rollback). This is the "environment promotion" topology.
2. **The deployment state machine** — a deterministic, Postgres-backed FSM (reusing the F07 DSL/guard/effect engine) that drives a single promotion: `requested → gate_evaluating → awaiting_approval → approved → deploying → verifying → succeeded`, with `gate_rejected | failed | rolling_back → rolled_back | cancelled` branches. Routing is **explicit policy/guards, never LLM judgement** (core design principle).
3. **The deployment gate evaluator** — the deploy analogue of F08's `MergeGateEvaluator`: it composes the repo policy `deploy_rules`, predecessor-success ordering, automated checks (CI green at the commit, spec-validated, optional security-scan-clean), and freeze-window state into a `GateEvaluation { can_proceed, requires_human_approval, blocking_reasons }`. **Restricted environments always require human approval — this cannot be relaxed by task or pipeline config.**
4. **The deploy execution + health + rollback machinery** — a pluggable `DeployProvider` (GitHub Deployments/Actions `workflow_dispatch` default, plus a generic webhook/command provider), a `HealthChecker`, and a rollback path that re-promotes the last-good artifact.
5. **The deploy gate type on the shared approval primitive** — extends F08's `approval_request` (and `ApprovalService.resolve`) so the same approval UI, RBAC, and **no-self-approval** rule govern deploys, with a `deployment_id` subject.

Why it matters: this is the slice that makes Forge's "human-in-the-loop, policy-aware" promise real for the riskiest action an engineering platform takes — shipping to production. Without it, the agent's `promote_environment` tool call (already gated to `deny + requires_approval` by F04) has nothing to drive, no ordering guarantee, no health check, and no rollback.

**F31 does NOT** implement: the FSM DSL engine internals (reused from F07), the agent loop or the `promote_environment` tool gate (F04/agent-runtime), the GitHub webhook signature/installation-token plumbing (F03 — F31 consumes its adapter), or the PR-merge flow (F08). It also does not invent a CI system — it reads CI status and triggers an existing provider.

---

## 2. User-facing behavior / journeys

**Journey A — Configure a pipeline (admin):**
1. In Project Settings → **Environments**, an admin sees the environments discovered from the connected repo's `deploy_rules` (`environments: [dev]`, `restricted_environments: [staging, production]`).
2. They order them `dev (rank 0) → staging (rank 1) → production (rank 2)`, set `production`'s approver group to `team-sre` (`min_approvals: 2`), add a Fri 17:00–Mon 09:00 **freeze window** to `production`, configure each environment's **deploy provider** (GitHub Actions `deploy.yml` with `workflow_dispatch`) and an HTTP **health check** (`GET https://prod.example.com/healthz` expect 200), and enable **auto-rollback** on `production`. `dev` is marked `auto_promote_on_merge: true`.
3. Save validates the pipeline against policy: every pipeline environment must appear in `deploy_rules.environments ∪ restricted_environments`; `staging`/`production` are forced `restricted = true` (cannot be unset).

**Journey B — Happy promotion path (engineer):**
1. A PR for `CORE-42` merges to `main` at `sha=abc123` (F08). Because `dev.auto_promote_on_merge` is true, a `Deployment(env=dev, commit=abc123)` is auto-requested.
2. `dev` is unrestricted and has no required approval → the gate clears automatically; the deploy provider runs; the health check passes; the **Deployments** board shows `dev → abc123 (healthy)`.
3. The engineer opens **Deployments**, clicks **Promote to staging** on `abc123`. The gate clears the automated checks (predecessor `dev` succeeded for `abc123`, CI green, spec validated) but `staging` is restricted → a **deploy approval** is created; the engineer (not the requester is allowed for staging if policy permits — here min_approvals 1) and reviewers get a Slack/email notification.
4. A reviewer opens the deploy approval, sees: target environment, source commit + diff-since-currently-deployed, predecessor state, gate check results, freeze status, and the run trace; clicks **Approve**. The deploy runs, health passes, the board shows `staging → abc123 (healthy)`.
5. **Promote to production** → gate requires the `team-sre` group with **2 approvals**; after two approvals the deploy runs, health passes, board shows `production → abc123 (healthy)`. The task timeline and audit log show every step.

**Journey C — Blocked by freeze window:**
- During the Fri–Mon freeze, **Promote to production** is refused at gate evaluation: the deployment moves to `gate_rejected` with `blocking_reasons: ["environment 'production' is in a freeze window until Mon 09:00"]`; the UI shows a "Frozen" banner and offers a **freeze override** action available only to `admin` (records an audited override decision before allowing the approval flow to proceed).

**Journey D — Predecessor not promoted:**
- An engineer tries **Promote to production** for `def456` while `staging` is still on `abc123`. The gate fails `predecessor_succeeded`: "production requires staging to have a successful deployment of def456; staging is on abc123." No approval is created.

**Journey E — Health check fails → auto-rollback:**
- A production deploy of `def456` succeeds at the provider but the health check fails after 3 attempts. With auto-rollback enabled, the deployment transitions `verifying → rolling_back`; a `rollback` deployment of the last-good `abc123` is created and executed; on success the original deployment is `rolled_back`, production state returns to `abc123 (healthy)`, and the on-call group is notified with the failure detail.

**Journey F — Agent-requested promotion:**
- An agent in a task with `requires_approval.deploy: true` calls `promote_environment(environment=staging)`. F04 returns `deny + requires_approval=True`; the agent runtime raises a deploy `ApprovalRequest`, F31 creates a `Deployment(initiated_by=agent)` in `awaiting_approval`. The agent **cannot** approve it (no-self-approval); a human must. If `deploy_rules.allow_agent_deploy=false`, even an unrestricted-env agent deploy still requires human approval.

**Journey G — Cancel:**
- Any non-terminal deployment can be cancelled by `member+` (the initiator or an admin): it moves to `cancelled`; if already `deploying`, cancellation is best-effort (the provider deploy may still complete and is reconciled to `failed`/`succeeded` by the status callback, never silently lost).

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

New SQLAlchemy 2.x models in `packages/db/forge_db/models/deployment.py`; new enums appended to `packages/db/forge_db/models/enums.py`. One Alembic migration `packages/db/migrations/versions/<rev>_f31_deployment_gates.py` (depends on the F01 board migration, the F07 workflow migration, and the F08 approval/PR migration). Models inherit `WorkspaceScopedModel` (UUID PK + timestamps + `workspace_id`) and use `json_type()`/`enum_type()` from `forge_db/base.py` for cross-dialect JSON + VARCHAR-backed enums (SQLite unit tests, Postgres prod).

**New `environment_pipeline`** — one per (project, repo).

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK→workspace CASCADE | tenant scope |
| `project_id` | UUID FK→project CASCADE | indexed |
| `repo_id` | text | `github.com/org/api` (matches `deploy_rules.repo_id`) |
| `enabled` | bool default `true` | |
| `version` | int default `1` | optimistic lock for config edits |
| `created_at`/`updated_at` | timestamptz | |

Unique: `(project_id, repo_id)`.

**New `environment`** — an ordered stage in a pipeline.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK→workspace CASCADE | |
| `pipeline_id` | UUID FK→environment_pipeline CASCADE | indexed |
| `name` | text | `dev` \| `staging` \| `production` — must exist in repo `deploy_rules` |
| `rank` | int NOT NULL | promotion order; 0 = first stage |
| `is_restricted` | bool NOT NULL | **derived from policy** `restricted_environments`; cannot be set false if policy says restricted |
| `requires_approval` | bool default `true` | effective only when **not** restricted (restricted ⇒ always true) |
| `gate_config` | `json_type()` default `{}` | `GateConfig` (§4): required checks, approver group, `min_approvals`, freeze windows, auto-rollback, `auto_promote_on_merge` |
| `provider_config` | `json_type()` default `{}` | `DeployProviderConfig` (provider name + provider-specific args) |
| `health_check` | `json_type()` default `{}` | `HealthCheckSpec` (§4) |
| `created_at`/`updated_at` | timestamptz | |

Unique: `(pipeline_id, name)` and `(pipeline_id, rank)`. CHECK `rank >= 0`.

**New `deployment`** — a single promotion attempt (the "run" of the deployment FSM).

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK→workspace CASCADE | tenant scope |
| `project_id` | UUID FK→project | indexed |
| `pipeline_id` | UUID FK→environment_pipeline | |
| `environment_id` | UUID FK→environment | target stage |
| `environment_name` | text | denormalized for audit stability |
| `repo_id` | text | |
| `commit_sha` | text | the artifact identity being promoted |
| `artifact_ref` | text null | optional image/tag/release ref |
| `from_environment_name` | text null | predecessor stage at request time (null for first stage) |
| `kind` | `enum_type(DeploymentKind)` | `promotion` \| `rollback` \| `redeploy` |
| `rollback_of` | UUID FK→deployment null | set when `kind=rollback` |
| `state` | `enum_type(DeploymentState)` | CHECK on the 12 values; indexed |
| `trigger` | `enum_type(DeploymentTrigger)` | `manual` \| `auto_promote` \| `agent` \| `automation` \| `rollback` |
| `initiated_by` | text | `user:<uuid>` \| `agent:<uuid>` \| `system:auto_promote` \| `system:automation:<rule_id>` |
| `workflow_run_id` | UUID FK→workflow_run null | the task run that produced the artifact (provenance) |
| `agent_run_id` | UUID null | populated for agent-initiated |
| `provider_name` | text null | resolved provider |
| `provider_external_id` | text null | GH deployment id / Actions run id |
| `provider_url` | text null | |
| `health_status` | `enum_type(HealthStatus)` null | `passing` \| `failing` \| `unknown` |
| `failure_reason` | text null | |
| `freeze_override_by` | UUID FK→user null | set if an admin overrode a freeze |
| `version` | int NOT NULL default 0 | optimistic-lock counter (bumped per transition) |
| `requested_at` | timestamptz | |
| `started_at`/`finished_at` | timestamptz null | |

Indexes: `ix_deployment_env_state (environment_id, state)`; partial `ix_deployment_active (environment_id) WHERE state NOT IN ('succeeded','failed','gate_rejected','rolled_back','cancelled')` to enforce **at most one in-flight deployment per environment** (also a partial unique index `uq_deployment_active_env`). Index `(repo_id, environment_name, state, finished_at)` for "currently deployed" lookups (last `succeeded` per environment).

**New `deployment_transition`** — append-only audit (mirrors F07's `workflow_transition`).

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `deployment_id` | UUID FK→deployment | indexed |
| `sequence` | int NOT NULL | monotonic per deployment, starts at 1 |
| `from_state` | text NOT NULL | |
| `to_state` | text NOT NULL | |
| `event` | text NOT NULL | `DeploymentEventType` |
| `guard_results` | `json_type()` default `{}` | |
| `effects_dispatched` | `json_type()` default `[]` | |
| `actor` | text NOT NULL | `system` \| `user:<uuid>` \| `agent:<uuid>` |
| `payload` | `json_type()` default `{}` | **secret-redacted** before persist |
| `idempotency_key` | text null | |
| `created_at` | timestamptz | |

Constraints: `UNIQUE(deployment_id, sequence)`; partial `UNIQUE(deployment_id, idempotency_key)`. Insert-only (enforced in repository).

**New `deployment_check_result`** — per-deployment automated gate check outcomes.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `deployment_id` | UUID FK→deployment CASCADE | |
| `name` | `enum_type(GateCheckName)` | `policy_allows` \| `predecessor_succeeded` \| `ci_green` \| `spec_validated` \| `security_clean` \| `not_frozen` |
| `status` | `enum_type(GateCheckStatus)` | `passed` \| `failed` \| `pending` \| `skipped` |
| `detail` | text NOT NULL | human-readable |
| `metrics` | `json_type()` default `{}` | e.g. `{"ci_status":"success"}` |
| `created_at` | timestamptz | |

**Extend F08 `approval_request` (additive migration):** add `deployment_id UUID NULL FK→deployment.id`; make `workflow_run_id` NULLABLE; add CHECK `(workflow_run_id IS NOT NULL) <> (deployment_id IS NOT NULL)` (exactly one subject — `agent_run_id` stays an optional supplementary FK); the foundation `approval_request.kind` enum **already includes `deploy`** (declared in `cross-cutting/F00-foundation-substrate` as `spec|plan|pr|deploy` and noted by F08), so F31 reuses `kind='deploy'` with **no new enum value**; add partial unique index `uq_pending_deploy_approval ON approval_request (deployment_id) WHERE kind='deploy' AND status='pending'`. The `health_status`/`record_environment_state` "currently deployed" view is computed (latest `succeeded` deployment per `environment_id`), not a separate table.

Migration is reversible (`downgrade` drops the four new tables + enums and reverts the `approval_request` additions). Unit tests run models on SQLite (`JSON` variant); partial/unique index behavior is asserted against the Postgres test container.

### 3.2 Backend (FastAPI routes + services/packages)

**(A) Pure-ish engine package — `packages/deploy-core/forge_deploy/`** (depends on `forge_contracts`, `forge_db` models/enums, `forge_workflow` DSL primitives, `forge_policy`, `pydantic`; **reuses** F07's `load_definition`, `GuardRegistry`, `EffectRegistry`, and transition algorithm rather than reinventing). Mirrors the `forge_workflow`/`forge_automation` decoupling: state logic is deterministic; all side effects (deploy, notify, health, approval-create) go through injected Protocols.

```
packages/deploy-core/forge_deploy/
├── __init__.py
├── states.py            # DeploymentState, DeploymentEventType, TERMINAL_STATES, enums
├── schemas.py           # Pydantic: EnvironmentSpec, PipelineSpec, GateConfig, FreezeWindow,
│                        #   DeployProviderConfig, HealthCheckSpec, DeploymentRequest, DeploymentDTO,
│                        #   GateCheckResult, GateEvaluation, HealthCheckResult, PromotionCheck
├── pipeline.py          # PipelineResolver: resolve pipeline+policy, ordering, predecessor, currently_deployed
├── gate.py              # DeploymentGateEvaluator (§4)
├── freeze.py            # is_frozen(env, now) -> FreezeState ; next_open(...)
├── providers.py         # DeployProvider Protocol + GitHubDeploymentsProvider, WebhookCommandProvider, NullDeployProvider
├── health.py            # HealthChecker Protocol + HttpHealthChecker, CommandHealthChecker, NullHealthChecker
├── engine.py            # DeploymentStateMachine.transition(deployment_id, event) (reuses forge_workflow algorithm)
├── guards.py            # deploy guard registry (gate_clear, predecessor_succeeded, approval_granted:deploy, ...)
├── effects.py           # deploy effect registry (evaluate_gate, trigger_deploy, run_health_check, start_rollback, ...)
├── repository.py        # DeploymentRepository (row-lock, append transition, currently-deployed reads)
├── errors.py            # GateBlockedError, PredecessorNotReadyError, FreezeWindowError, ProviderError, ...
└── definitions/
    └── deployment_promotion.yaml   # bundled deployment FSM DSL (§4)
```

The **engine is side-effect-free except via injected ports**: `DeploymentStateMachine.transition` row-locks the `deployment`, evaluates guards (which read `deployment_check_result` + `approval_request`), applies the state change, appends a `deployment_transition`, commits, then dispatches effects post-commit through the `WorkflowEffectDispatcher` (reused F07 Protocol → Celery). `DeploymentGateEvaluator.evaluate` is pure over its injected readers and writes the `deployment_check_result` rows.

**(B) API + persistence — `apps/api/forge_api/`**:

- `forge_api/routers/deployments.py` (mounted under `/api/v1`, all auth-required, `workspace_id` from principal, RBAC per route):

| Method & path | Handler | RBAC | Returns |
|---|---|---|---|
| `GET /projects/{project_id}/pipeline` | `get_pipeline` | viewer+ | `PipelineRead` (envs + currently-deployed per env) |
| `PUT /projects/{project_id}/pipeline` | `upsert_pipeline(body: PipelineUpsert)` | admin | `PipelineRead` (409 on stale `version`; 422 if env not in policy / restricted-unset) |
| `POST /projects/{project_id}/deployments` | `request_deployment(body: DeploymentRequest)` | member+ (`agent-runner` only via service token) | `DeploymentRead` 201 |
| `GET /projects/{project_id}/deployments` | `list_deployments(env?, state?, cursor, limit)` | viewer+ | `Page[DeploymentRead]` |
| `GET /deployments/{deployment_id}` | `get_deployment` | viewer+ | `DeploymentDetail` (incl. gate checks, transitions, diff-since) |
| `POST /deployments/{deployment_id}/cancel` | `cancel_deployment` | member+ (initiator/admin) | `DeploymentRead` |
| `POST /deployments/{deployment_id}/rollback` | `rollback_deployment` | member+ (prod ⇒ approver/admin) | `DeploymentRead` (creates a `kind=rollback` deployment) |
| `POST /deployments/{deployment_id}/freeze-override` | `override_freeze(body: {reason})` | **admin** | `DeploymentRead` (audited) |
| `GET /deployments/{deployment_id}/gate` | `get_gate` | viewer+ | `GateEvaluation` |
| `POST /providers/deployments/callback` | `provider_callback(body, sig)` | provider HMAC | 204 (provider status webhook → emits `deploy_succeeded`/`deploy_failed`) |

  The **deploy approval decision reuses F08's `POST /approvals/{approval_id}/decision`** — no new approval endpoint. Exception handlers map `GateBlockedError`/`PredecessorNotReadyError`/`FreezeWindowError`→409 with `blocking_reasons`, version conflict→409, cross-workspace→404, policy-relaxation-of-restricted→422.

- `forge_api/services/deployment_service.py` — orchestration: pipeline upsert (validate against the repo's `RepoPolicySnapshot` `deploy_rules`; force `is_restricted` from policy), `request_deployment` (resolve pipeline/env, compute `from_environment`, create the `deployment` in `requested`, then call `DeploymentStateMachine.transition(request)`), cancel, rollback (build a `kind=rollback` deployment of `currently_deployed(predecessor_or_self_last_good)`), freeze override.

- **Extend F08 `ApprovalService.resolve(approval_id, decision, principal, note)`** (`apps/api/forge_api/services/approval_service.py`): dispatch on the existing `approval_request.kind`. For `kind='deploy'`: authorize (no-self-approval; approver-group / `min_approvals` from the env `gate_config`), record the decision, and on the final required `approve` drive `DeploymentStateMachine.transition(approve)`; on `reject` drive `transition(reject)`. Returns the existing `ApprovalResolution` shape extended with the deployment outcome.

**Trigger wiring (auto-promote).** A `DeploymentRequester` Protocol + `request_promotion(...)` helper added to `forge_contracts/deployment.py` so board/automation/F08 can request a promotion without importing `forge_deploy`. F08's merge handler (or an F21 automation action `request_promotion`) calls it post-merge when the target env has `auto_promote_on_merge: true`. The concrete `CeleryDeploymentRequester` enqueues `forge_worker.tasks.deployments.request_deployment_task`.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

`apps/worker/forge_worker/tasks/deployments.py` (reuses the v1 Celery app + Redis broker; routes long deploy/health work to a dedicated `deployments` queue so it cannot starve indexing/verification workers). **No LangGraph** — the deployment FSM is deterministic and contains no agent loop.

- `advance_deployment(deployment_id, event_dict)` — the single driver; constructs `DeploymentStateMachine` with the real dispatcher and calls `transition`. Re-enqueues itself for chained machine-driven transitions (`gate_evaluating → … → deploying`), exactly like F08's `advance_workflow`.
- `evaluate_gate_task(deployment_id)` — runs `DeploymentGateEvaluator.evaluate`, persists `deployment_check_result` rows, then enqueues `advance_deployment` with `gate_passed` / `gate_requires_approval` / `gate_failed`.
- `trigger_deploy_task(deployment_id)` — resolves the `DeployProvider`, calls `provider.trigger(DeployRequest)`, stores `provider_external_id`/`url`, emits `deploy_started`. Terminal status arrives via `provider_callback` (preferred) or `poll_deploy_status` (Celery Beat fallback for providers without callbacks).
- `run_health_check_task(deployment_id)` — runs the configured `HealthChecker` with retries; emits `health_passed`/`health_failed`; on `health_failed` the FSM (not this task) decides auto-rollback per `gate_config`.
- `start_rollback_task(deployment_id)` — creates the `kind=rollback` deployment of the last-good artifact and drives it; on its terminal state, calls back `advance_deployment(original, rollback_succeeded|rollback_failed)`.
- `poll_deploy_status` / `reconcile_stuck_deployments` — Celery Beat; reconciles deployments stuck in `deploying`/`verifying` past a timeout (provider status fetched; if unknowable after `deploy_timeout_s`, mark `failed` with reason — never leave a deployment dangling).

Concurrency: the partial unique active-deployment-per-environment index + a `SELECT … FOR UPDATE` row lock on the `deployment` (and an advisory lock on `environment_id` while requesting) serialize concurrent promotions to the same environment.

### 3.4 Frontend / UI (Next.js routes/components)

**App:** `apps/web` (App Router, TS, Tailwind, shadcn/ui, TanStack Query). Reuses F08's approval review components for the deploy gate.

Routes:
- `app/[workspace]/projects/[projectKey]/deployments/page.tsx` — the **Promotion Board**: a column per environment (ordered by rank) showing the currently-deployed `commit_sha`, health badge, last deploy time, and a **Promote →** action that targets the next stage; an empty state guiding the user to configure a pipeline.
- `app/[workspace]/projects/[projectKey]/deployments/[deploymentId]/page.tsx` — **Deployment Detail**: target env, source commit + diff-since-currently-deployed, predecessor state, `GateEvaluation` checklist (policy / predecessor / CI / spec / security / freeze), approval panel (reuses F08 `ApprovalActions` + `RiskFlags`), provider log link, health result, transition timeline (reuses `v1/F10-run-trace-viewer`'s `RunTraceTimeline` component), and **Rollback** / **Cancel** / **Freeze override** (admin) buttons.
- `app/[workspace]/projects/[projectKey]/settings/environments/page.tsx` — **Pipeline editor**: order environments (drag rank), per-env gate config form (required checks toggles, approver group + `min_approvals`, freeze-window editor, deploy-provider config, health-check config, auto-rollback, `auto_promote_on_merge`), with restricted-env fields locked to policy.

Components (`apps/web/components/deployments/`): `PromotionBoard.tsx`, `EnvironmentColumn.tsx`, `DeploymentCard.tsx`, `GateChecklist.tsx`, `FreezeBanner.tsx`, `DeploymentTimeline.tsx`, `RollbackDialog.tsx`, `PipelineEditor.tsx`, `EnvironmentGateForm.tsx`, `FreezeWindowEditor.tsx`. Data layer: `apps/web/lib/deployments/{api,queries,mutations,types}.ts` with optimistic state on Promote/Approve and rollback-on-error (per board UX standards). The detail page is keyboard-first (`a` approve, `r` reject (where allowed), `b` rollback).

### 3.5 Infra / deploy (compose, helm, caddy)

Reuses existing services; F31-specific:
- **No new compose service**, but the `worker` gains a dedicated `deployments` Celery queue (configured in `deploy/docker-compose.yml` worker command) and a Celery **Beat** entry for `poll_deploy_status`/`reconcile_stuck_deployments` (Beat already required by F01/F21).
- **MinIO bucket** `forge-deploy-logs` for provider/health logs, lifecycle expiry (e.g. 90 days) — add to `deploy/scripts/install.sh` bucket bootstrap.
- **Provider callback ingress**: `POST /api/v1/providers/deployments/callback` must be reachable by GitHub/CI; document the required GitHub webhook events (`deployment`, `deployment_status`, `workflow_run`) in `docs/integrations/github-app-setup.md` (owned by F03; F31 adds the deployment-event requirement).
- New env knobs documented in `.env.example` / `deploy/.env.production.example`: `FORGE_DEPLOY_PROVIDER_CALLBACK_SECRET`, `FORGE_DEPLOY_DEFAULT_TIMEOUT_S` (default 1800), `FORGE_DEPLOY_HEALTH_RETRIES` (default 3).
- Helm (V3): no new chart resources beyond the worker queue/beat entry; values gain `deploy.queue` and the callback secret.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Enums** (`forge_deploy/states.py`; DB-persisted ones mirrored into `forge_db/models/enums.py`):
```python
from enum import StrEnum

class DeploymentState(StrEnum):
    REQUESTED = "requested"
    GATE_EVALUATING = "gate_evaluating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DEPLOYING = "deploying"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    GATE_REJECTED = "gate_rejected"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({
    DeploymentState.SUCCEEDED, DeploymentState.FAILED, DeploymentState.GATE_REJECTED,
    DeploymentState.ROLLED_BACK, DeploymentState.CANCELLED,
})

class DeploymentEventType(StrEnum):
    REQUEST = "request"
    GATE_PASSED = "gate_passed"
    GATE_REQUIRES_APPROVAL = "gate_requires_approval"
    GATE_FAILED = "gate_failed"
    APPROVE = "approve"
    REJECT = "reject"
    DEPLOY_STARTED = "deploy_started"
    DEPLOY_SUCCEEDED = "deploy_succeeded"
    DEPLOY_FAILED = "deploy_failed"
    HEALTH_PASSED = "health_passed"
    HEALTH_FAILED = "health_failed"
    ROLLBACK_SUCCEEDED = "rollback_succeeded"
    ROLLBACK_FAILED = "rollback_failed"
    CANCEL = "cancel"

class DeploymentKind(StrEnum):
    PROMOTION = "promotion"; ROLLBACK = "rollback"; REDEPLOY = "redeploy"

class DeploymentTrigger(StrEnum):
    MANUAL = "manual"; AUTO_PROMOTE = "auto_promote"; AGENT = "agent"
    AUTOMATION = "automation"; ROLLBACK = "rollback"

class GateCheckName(StrEnum):
    POLICY_ALLOWS = "policy_allows"; PREDECESSOR_SUCCEEDED = "predecessor_succeeded"
    CI_GREEN = "ci_green"; SPEC_VALIDATED = "spec_validated"
    SECURITY_CLEAN = "security_clean"; NOT_FROZEN = "not_frozen"

class GateCheckStatus(StrEnum):
    PASSED = "passed"; FAILED = "failed"; PENDING = "pending"; SKIPPED = "skipped"

class HealthStatus(StrEnum):
    PASSING = "passing"; FAILING = "failing"; UNKNOWN = "unknown"
```

**Pipeline / environment config** (`forge_deploy/schemas.py`, Pydantic v2; YAML/JSON-portable):
```python
from pydantic import BaseModel, Field, ConfigDict
from uuid import UUID
from datetime import datetime, time

class FreezeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # weekly recurring window in the pipeline timezone; closed = deploys blocked
    start_day: int = Field(ge=0, le=6)      # 0=Mon
    start_time: time
    end_day: int = Field(ge=0, le=6)
    end_time: time
    reason: str = "release freeze"

class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_checks: list[GateCheckName] = [GateCheckName.CI_GREEN]
    approver_user_ids: list[UUID] = []
    approver_team_ids: list[UUID] = []
    min_approvals: int = 1
    freeze_windows: list[FreezeWindow] = []
    timezone: str = "UTC"
    auto_rollback: bool = False
    auto_promote_on_merge: bool = False
    rollback_requires_approval: bool = False
    deploy_timeout_s: int = 1800

class DeployProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str                            # "github_actions" | "github_deployments" | "webhook" | "null"
    # provider-specific keys, e.g. workflow_file, ref, inputs, url, method, headers_secret_ref

class HealthCheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "none"                       # "http" | "command" | "none"
    url: str | None = None
    expect_status: int = 200
    command: str | None = None
    timeout_s: int = 60
    retries: int = 3
    interval_s: int = 10

class EnvironmentSpec(BaseModel):
    name: str
    rank: int = Field(ge=0)
    requires_approval: bool = True
    gate_config: GateConfig = GateConfig()
    provider_config: DeployProviderConfig
    health_check: HealthCheckSpec = HealthCheckSpec()

class PipelineSpec(BaseModel):
    repo_id: str
    enabled: bool = True
    environments: list[EnvironmentSpec] = Field(min_length=1)
```

**Deployment request + DTOs** (`forge_contracts/deployment.py`):
```python
class DeploymentRequest(BaseModel):
    environment: str                         # target stage name
    commit_sha: str
    artifact_ref: str | None = None
    kind: DeploymentKind = DeploymentKind.PROMOTION
    trigger: DeploymentTrigger = DeploymentTrigger.MANUAL
    workflow_run_id: UUID | None = None
    idempotency_key: str | None = None       # e.g. "{env}:{commit_sha}" dedupes duplicate requests

class DeploymentDTO(BaseModel):
    id: UUID
    project_id: UUID
    environment_name: str
    repo_id: str
    commit_sha: str
    artifact_ref: str | None
    from_environment_name: str | None
    kind: DeploymentKind
    rollback_of: UUID | None
    state: DeploymentState
    trigger: DeploymentTrigger
    initiated_by: str
    provider_name: str | None
    provider_url: str | None
    health_status: HealthStatus | None
    failure_reason: str | None
    requested_at: datetime
    finished_at: datetime | None
    model_config = ConfigDict(from_attributes=True)

class DeploymentRequester(Protocol):
    def request_promotion(self, *, project_id: UUID, request: DeploymentRequest,
                          initiated_by: str) -> DeploymentDTO: ...
```

**Gate evaluation** (`forge_deploy/gate.py`):
```python
class GateCheckResult(BaseModel):
    name: GateCheckName
    status: GateCheckStatus
    detail: str
    metrics: dict[str, str] = {}

class GateEvaluation(BaseModel):
    deployment_id: UUID
    environment: str
    can_proceed: bool                # all required checks passed
    requires_human_approval: bool    # True iff restricted OR env.requires_approval OR not allow_agent_deploy(agent)
    checks: list[GateCheckResult]
    blocking_reasons: list[str]

class DeploymentGateEvaluator:
    def __init__(self, repo: "DeploymentRepository", policy: "PolicyReader",
                 validation: "ValidationReader", github: "GitHubAdapter",
                 freeze: "FreezeEvaluator", clock: "Clock") -> None: ...
    async def evaluate(self, deployment_id: UUID) -> GateEvaluation: ...
```

Gate algorithm (deterministic, total):
1. **`policy_allows`** — load the repo `RepoPolicySnapshot.deploy_rules`. If `deployment.trigger == agent` and `allow_agent_deploy is False` → check `failed` is **not** raised, but `requires_human_approval = True` and a `policy_allows` check `passed` (human can still approve). If the env is in neither `environments` nor `restricted_environments` → `policy_allows` `failed` (env unknown to policy) → `can_proceed=False`.
2. **`predecessor_succeeded`** — for rank > 0, the predecessor environment must have a `succeeded`, non-rolled-back deployment of the **same `commit_sha`**; else `failed` with reason. Rank 0 → `skipped`.
3. **`ci_green`** (if in `required_checks`) — `github.get_combined_status(repo_id, commit_sha) == "success"`; else `failed`/`pending`.
4. **`spec_validated`** (if required) — an F02 `ValidationReport` with `status == "pass"` (F02's literal is `pass`/`fail`) exists for the commit's task spec, pinned to `head_sha == commit_sha` (exactly as F08 pins `pull_request.validated_head_sha`); else `failed`. `skipped` when no spec linkage.
5. **`security_clean`** (if required) — latest security-review report for the commit has no `critical` findings; `skipped` if unavailable.
6. **`not_frozen`** — `freeze.is_frozen(env, clock.now())` is False unless `deployment.freeze_override_by` is set; else `failed`.
7. `requires_human_approval = env.is_restricted OR env.requires_approval OR (trigger==agent AND not allow_agent_deploy)`. **`is_restricted` forces approval regardless of any other config — there is no code path that sets `requires_human_approval=False` for a restricted environment.**
8. `can_proceed = all required checks passed`. The evaluator never deploys; it returns the evaluation and the caller's FSM decides `gate_passed`/`gate_requires_approval`/`gate_failed`.

**Deploy provider** (`forge_deploy/providers.py`):
```python
class DeployRequest(BaseModel):
    deployment_id: UUID
    repo_id: str
    environment: str
    commit_sha: str
    artifact_ref: str | None
    config: dict[str, Any]

class DeployHandle(BaseModel):
    provider: str
    external_id: str
    url: str | None = None

class DeployStatus(BaseModel):
    state: Literal["pending", "in_progress", "success", "failure", "error"]
    detail: str | None = None
    finished: bool

class DeployProvider(Protocol):
    name: str
    async def trigger(self, req: DeployRequest) -> DeployHandle: ...
    async def get_status(self, handle: DeployHandle) -> DeployStatus: ...
    def supports_native_rollback(self) -> bool: ...

class NullDeployProvider:
    """Test double: trigger records the request and returns a synthetic handle;
    get_status returns a scripted sequence; no external calls."""
```

`GitHubDeploymentsProvider` and `WebhookCommandProvider` implement the Protocol; the GitHub one uses F03's `GitHubAppClient` (the authoritative `GitHubIntegration` surface, extended in this slice with create-deployment + `workflow_dispatch`; `get_status` reads `deployment_status`/`workflow_run`).

**Health checker** (`forge_deploy/health.py`):
```python
class HealthCheckResult(BaseModel):
    status: HealthStatus
    attempts: int
    detail: str
    log_ref: str | None = None

class HealthChecker(Protocol):
    async def check(self, spec: HealthCheckSpec, *, deployment_id: UUID) -> HealthCheckResult: ...
```

**State machine** (`forge_deploy/engine.py`) — reuses F07's `WorkflowDefinition`/`load_definition`/`GuardRegistry`/`EffectRegistry`/`WorkflowEffectDispatcher` and the F07 transition algorithm (row lock → guard eval → append transition → commit → post-commit dispatch), operating on `deployment` rows:
```python
class DeploymentEvent(BaseModel):
    type: DeploymentEventType
    payload: dict[str, Any] = {}
    actor: str = "system"
    idempotency_key: str | None = None

class DeploymentStateMachine:
    def __init__(self, session, definition: "WorkflowDefinition",
                 guards: "GuardRegistry", effects: "EffectRegistry",
                 dispatcher: "WorkflowEffectDispatcher") -> None: ...
    def transition(self, deployment_id: UUID, event: DeploymentEvent) -> DeploymentState: ...
```

**Bundled DSL — `forge_deploy/definitions/deployment_promotion.yaml`** (parsed by F07's `load_definition`; engine injects universal `cancel → cancelled` edges for non-terminal states):
```yaml
name: deployment_promotion
version: "1"
transitions:
  - {from: requested,         on: request,                to: gate_evaluating,   effects: [evaluate_gate]}
  - {from: gate_evaluating,   on: gate_passed,            to: approved,          guards: [gate_clear, no_approval_required], effects: [trigger_deploy], priority: 1}
  - {from: gate_evaluating,   on: gate_requires_approval, to: awaiting_approval, guards: [gate_clear], effects: [create_deploy_approval, notify_approval_requested]}
  - {from: gate_evaluating,   on: gate_failed,            to: gate_rejected,     effects: [notify_gate_failed]}
  - {from: awaiting_approval, on: approve,                to: approved,          guards: [approval_granted:deploy, gate_clear], effects: [trigger_deploy], record: approval_event}
  - {from: awaiting_approval, on: reject,                 to: gate_rejected,     record: approval_event, effects: [notify_rejected]}
  - {from: approved,          on: deploy_started,         to: deploying}
  - {from: deploying,         on: deploy_succeeded,       to: verifying,         effects: [run_health_check]}
  - {from: deploying,         on: deploy_failed,          to: failed,            effects: [maybe_rollback, notify_failed]}
  - {from: verifying,         on: health_passed,          to: succeeded,         effects: [record_environment_state, notify_succeeded]}
  - {from: verifying,         on: health_failed,          to: rolling_back,      guards: [auto_rollback_enabled], effects: [start_rollback], priority: 1}
  - {from: verifying,         on: health_failed,          to: failed,            guards: [auto_rollback_disabled], effects: [notify_failed]}
  - {from: rolling_back,      on: rollback_succeeded,     to: rolled_back,       effects: [record_environment_state, notify_rolled_back]}
  - {from: rolling_back,      on: rollback_failed,        to: failed,            effects: [notify_failed]}
```

**Deploy guards** (`forge_deploy/guards.py`, registered into the deploy `GuardRegistry`):
- `gate_clear` — the deployment's latest `GateEvaluation.can_proceed` is True (all required `deployment_check_result` rows `passed`).
- `no_approval_required` — latest `GateEvaluation.requires_human_approval` is False.
- `approval_granted:deploy` — the `deploy` `ApprovalRequest` for this deployment is `approved` **and** the configured `min_approvals` is met by distinct authorized approvers.
- `auto_rollback_enabled` / `auto_rollback_disabled` — read `env.gate_config.auto_rollback`.

**Approval extension** (F08 reuse — `apps/api/forge_api/services/approval_service.py`): `resolve()` branches on the existing `approval_request.kind`; `kind='deploy'` enforces no-self-approval (initiator `user:<id>`/`agent:<id>` cannot resolve), counts distinct approvers toward `min_approvals`, and on completion drives `DeploymentStateMachine.transition`. The `POST /approvals/{approval_id}/decision` contract is unchanged; `ApprovalResolution` gains optional `deployment: DeploymentDTO | None`.

**Representative API models** (`apps/api/forge_api/schemas/deployments.py`):
```python
class PipelineUpsert(PipelineSpec):
    version: int                     # optimistic lock
class EnvironmentRead(EnvironmentSpec):
    id: UUID; is_restricted: bool; currently_deployed: DeploymentDTO | None
class PipelineRead(BaseModel):
    id: UUID; project_id: UUID; repo_id: str; enabled: bool; version: int
    environments: list[EnvironmentRead]
class DeploymentRead(DeploymentDTO): ...
class DeploymentDetail(DeploymentRead):
    gate: GateEvaluation | None
    checks: list[GateCheckResult]
    transitions: list[dict]          # DeploymentTransitionDTO
    diff_since: dict | None          # {base_sha, files_changed, url} vs currently-deployed
```

**YAML pipeline artifact (`examples/deployments/python-api-pipeline.yaml`)** — parses to `PipelineSpec`:
```yaml
repo_id: github.com/org/api
enabled: true
environments:
  - name: dev
    rank: 0
    requires_approval: false
    gate_config: { required_checks: [ci_green], auto_promote_on_merge: true }
    provider_config: { provider: github_actions, workflow_file: deploy.yml, ref: main, inputs: { env: dev } }
    health_check: { kind: http, url: https://dev.example.com/healthz, expect_status: 200, retries: 3 }
  - name: staging
    rank: 1
    gate_config: { required_checks: [ci_green, spec_validated], min_approvals: 1 }
    provider_config: { provider: github_actions, workflow_file: deploy.yml, ref: main, inputs: { env: staging } }
    health_check: { kind: http, url: https://staging.example.com/healthz, expect_status: 200 }
  - name: production
    rank: 2
    gate_config:
      required_checks: [ci_green, spec_validated, security_clean]
      approver_team_ids: []          # filled with team-sre id at apply time
      min_approvals: 2
      auto_rollback: true
      freeze_windows:
        - { start_day: 4, start_time: "17:00", end_day: 0, end_time: "09:00", reason: weekend freeze }
    provider_config: { provider: github_deployments, environment: production }
    health_check: { kind: http, url: https://prod.example.com/healthz, expect_status: 200, retries: 5 }
```

---

## 5. Dependencies — features/slices that must exist first

Hard (build/runtime):
- `v1/F07-feature-workflow-fsm` — the DSL (`load_definition`, `WorkflowDefinition`, `TransitionRule`), `GuardRegistry`/`EffectRegistry`, `WorkflowEffectDispatcher` Protocol + `CeleryEffectDispatcher`, and the transition algorithm that `DeploymentStateMachine` reuses; `workflow_run` (provenance FK).
- `v1/F08-plan-execute-verify-pr-approval` — the generic `approval_request` table (whose `kind` enum already carries `deploy`) + `ApprovalService.resolve(approval_id, decision, principal, note)` (extended here to branch on `kind='deploy'`), the no-self-approval rule, the `MergeGateEvaluator` pattern `DeploymentGateEvaluator` mirrors, and the `GitHubAdapter.get_combined_status() -> str` consumer-view (F08's blessed alias of F03's `GitHubIntegration`) that drives the `ci_green` check. The `spec_validated` check reads F02's `ValidationReport` through F31's own injected `ValidationReader` port (see the F02 soft dep), pinned to `head_sha == commit_sha` exactly as F08 pins `validated_head_sha`.
- `v1/F04-repo-policy` — `deploy_rules` schema (`allow_agent_deploy`, `environments`, `restricted_environments`), the `RepoPolicySnapshot`, and the `PolicyEvaluator` `deploy`/`promote_environment` decision (`deny + requires_approval` for restricted) that agent-initiated promotions ride in on.
- `v1/F03-github-app` — F03's authoritative `GitHubIntegration` Protocol / `GitHubAppClient` (F08's `GitHubAdapter` is the narrow consumer alias of it, per F03's documented mapping), extended here for the GitHub Deployments API + Actions `workflow_dispatch`; `get_combined_status`/`get_ci_status` for the `ci_green` check; and the `deployment`/`deployment_status`/`workflow_run` webhook ingestion that feeds `provider_callback`.
- `v1/F01-project-board` — `project` scope, board timeline events the deployment surfaces on, RBAC principal (`admin`/`member`/`viewer`/`agent-runner`).
- `cross-cutting` — auth/RBAC, immutable audit store, MinIO artifact store, secret-redaction utility, `Clock`/`FakeClock`.

Soft (degrade-gracefully — F31 ships the path; it activates when the producer lands):
- `v1/F02-spec-engine` — `ValidationReport` behind the `spec_validated` gate check (absent → `skipped`).
- `v1/F12-eval-harness` / security-review skill — `security_clean` check (absent → `skipped`).
- `v1/F16-slack-notifications` — deploy approval/notify effects (absent → in-app/email; events still emitted).
- `v1/F10-run-trace-viewer` — the `RunTraceTimeline` component reused by the Deployment Detail page to render the `deployment_transition` history (absent → a local minimal timeline list; the audit rows still exist).
- `v2/F21-workflow-automations` — F31 **registers** a `request_promotion` action into F21's `ActionExecutor` + `/automations/catalog` and a `deployment_succeeded` trigger source into F21's dispatcher (so "when staging deploy succeeds → request production promotion"); the action calls the `DeploymentRequester` Protocol F31 exposes. Absent F21, promotions are still driven manually and by F08 auto-promote-on-merge.
- `v3/F29-advanced-policy-engine` (conditional/per-environment rules) and `v3/F30-multi-team-rbac` (team/role hierarchy, `PermissionResolver`) refine approver-group resolution; F31 uses the simple `gate_config` approver lists (`approver_user_ids`/`approver_team_ids` + `min_approvals`) until they land.

---

## 6. Acceptance criteria (numbered, testable)

1. **Pipeline upsert + policy binding.** `PUT /projects/{id}/pipeline` with envs `[dev, staging, production]` persists one `environment_pipeline` + three `environment` rows ordered by `rank`; `staging`/`production` get `is_restricted=true` derived from the repo's `deploy_rules.restricted_environments`; an attempt to set `is_restricted=false` on a policy-restricted env returns 422.
2. **Pipeline rejects unknown env.** Upserting an env whose `name` is in neither `deploy_rules.environments` nor `restricted_environments` returns 422 `RuleValidationError`.
3. **Optimistic config edit.** Two `PUT pipeline` with the same stale `version` → first 200, second 409 `version_conflict`.
4. **Request creates a deployment.** `POST /projects/{id}/deployments {environment:dev, commit_sha:abc123}` returns 201 + `DeploymentRead` in state `requested`, then the machine advances it; for unrestricted `dev` with `required_checks:[ci_green]` and green CI it reaches `deploying` without any `approval_request` being created.
4a. **At-most-one active deployment per environment.** A second active request for the same `environment_id` is rejected by the partial unique index (`409`/`DeploymentConflict`).
5. **Gate failure — predecessor.** Requesting `production` for `def456` while `staging`'s last successful deployment is `abc123` yields a `predecessor_succeeded` check `failed`, state `gate_rejected`, `blocking_reasons` naming the mismatch, and **no** approval and **no** provider call.
6. **Gate — CI red.** With `required_checks:[ci_green]` and combined status `failure`, the deployment reaches `gate_rejected` with a `ci_green` `failed` check; the provider is never triggered.
7. **Restricted env always needs approval.** A promotion to a restricted env whose `requires_approval` is set false (or whose task `requires_approval.deploy=false`) still creates a `pending` `deploy` `ApprovalRequest` and waits in `awaiting_approval`; `GateEvaluation.requires_human_approval` is true. There is no input that makes a restricted-env deployment skip approval.
8. **Approval drives deploy.** Resolving the `deploy` approval with `approve` (by an authorized non-initiator) when `min_approvals=1` transitions `awaiting_approval → approved` and triggers exactly one `DeployProvider.trigger`; `reject` transitions to `gate_rejected` and never triggers the provider.
9. **Multi-approval.** With `min_approvals=2`, one approval keeps the deployment in `awaiting_approval`; the second distinct authorized approval advances it; the same user approving twice does not satisfy the count.
10. **No self-approval.** The `user`/`agent` that initiated the deployment receives 403 from `POST /approvals/{id}/decision approve`; a `viewer` receives 403; an authorized `member`/approver succeeds.
11. **Deploy success → verify → succeeded.** A provider `success` callback emits `deploy_succeeded` → `verifying`; a passing `HealthChecker` emits `health_passed` → `succeeded`; the environment's currently-deployed view now returns this `commit_sha` with `health_status=passing`.
12. **Health fail → auto-rollback.** With `auto_rollback=true`, a `health_failed` transitions `verifying → rolling_back`, creates a `kind=rollback` deployment of the last-good artifact, and on its success transitions the original to `rolled_back`; the environment view reverts to the prior `commit_sha`.
13. **Health fail → no rollback.** With `auto_rollback=false`, `health_failed` transitions `verifying → failed` with `failure_reason` set and no rollback deployment created.
14. **Freeze window blocks.** During a configured freeze window, the `not_frozen` check is `failed` and the deployment is `gate_rejected`; an `admin` `POST .../freeze-override {reason}` sets `freeze_override_by`, after which a re-request passes `not_frozen` and proceeds through the normal approval gate; a non-admin override returns 403; the override is recorded in the audit trail.
15. **Predecessor ordering enforced positively.** Promoting `abc123` to `staging` succeeds only after `dev` has a `succeeded` deployment of `abc123`; promoting to `production` succeeds only after `staging` has `succeeded` for `abc123`.
16. **Agent deploy needs human.** A deployment with `trigger=agent` and `deploy_rules.allow_agent_deploy=false` always waits in `awaiting_approval` even for an unrestricted env; the agent identity cannot resolve it (AC10).
17. **Idempotent request.** Two `POST deployments` with the same `idempotency_key` (e.g. `staging:abc123`) create exactly one `deployment`; the second returns the existing one.
18. **Provider failure handled.** A provider `failure` callback emits `deploy_failed` → `failed` (or `rolling_back` if `maybe_rollback` decides), with `failure_reason`; a deployment stuck in `deploying` past `deploy_timeout_s` is reconciled to `failed` by `reconcile_stuck_deployments` (never left dangling).
19. **Append-only audit.** Every state change, gate decision, approval, deploy start, health result, rollback, and freeze override writes a `deployment_transition` (and/or `deployment_check_result`) row with actor + redacted payload; rows are never updated/deleted; the timeline reconstructs the full history in `sequence` order.
20. **Cancel.** A non-terminal deployment can be cancelled by its initiator or an `admin` → `cancelled`; a terminal deployment returns 409 on cancel.
21. **Auto-promote on merge.** With `dev.auto_promote_on_merge=true`, an F08 merge of `abc123` to `main` results (via `DeploymentRequester.request_promotion`) in a `Deployment(env=dev, commit=abc123, trigger=auto_promote)`; with it false, no deployment is created.
22. **RBAC.** `viewer` gets 403 on request/cancel/rollback/freeze-override/pipeline-upsert and 200 on get/list; `member` can request/cancel/rollback; only `admin` can upsert pipeline + override freeze; cross-workspace deployment access returns 404.
23. **DSL drift guard.** `forge_deploy/definitions/deployment_promotion.yaml` and `examples/deployments/deployment_promotion.yaml` parse to equal `WorkflowDefinition` objects under F07's `load_definition`; every guard/effect name in the YAML is registered.
24. **Gate evaluator totality.** `DeploymentGateEvaluator.evaluate` returns a `GateEvaluation` for every input combination (restricted/unrestricted × each check pass/fail/skip × agent/manual) and never raises; `requires_human_approval` is true for every restricted-env case (property test).
25. **Frontend.** The Promotion Board renders one column per env ordered by rank with currently-deployed + health badge; clicking **Promote** optimistically creates a deployment and rolls back on error; the Deployment Detail renders the `GateChecklist`, the freeze banner when frozen, and (for restricted envs) the approval actions; selecting a human-gated approve as the initiator surfaces the 403 inline (component/e2e tests).

### 6.x Definition of Done
All ACs covered by passing tests; `advance_deployment` drives a seeded promotion end-to-end across `dev → staging → production` against a `NullDeployProvider` + scripted `HealthChecker` + a fake `GitHubAdapter` on a real Postgres (testcontainer); coverage of new F31 code ≥ 80% (the `backend-tdd` bar Forge applies to itself).

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Framework: `pytest` + `pytest-asyncio`. Engine unit tests use **SQLite in-memory** (JSON variant) + `NullDeployProvider` + scripted `HealthChecker` + `NullEffectDispatcher` + `FakeClock`; integration tests use the **Postgres test container** + Celery eager mode + a fake `GitHubAdapter`. Write tests first; no module is done until `ruff check`, type check, and `pytest` for `packages/deploy-core` + the API deployments suite are green.

**Key fixtures (`packages/deploy-core/tests/conftest.py`):**
- `pipeline_spec` — the canonical `dev → staging → production` `PipelineSpec`.
- `seed_pipeline(session, **overrides)` — pipeline + 3 environments wired to a repo whose `RepoPolicySnapshot` marks staging/production restricted.
- `make_deployment(session, env, commit, **flags)` — factory at a given state.
- `null_provider` (scripted trigger/status), `scripted_health` (passing/failing sequences), `fake_github` (settable combined status + recorded deployments), `policy_reader` (settable `deploy_rules`), `validation_reader` (settable validation), `freeze_clock` (FakeClock to enter/exit a freeze window).
- `deploy_engine(session, dispatcher, clock)` — `DeploymentStateMachine` with the deploy registries + `NullEffectDispatcher`.
- `drive(engine, deployment, *events)` — applies `(event_type, payload)` and returns final state + transitions.

**Unit — gate (`tests/test_gate.py`):** AC5/6/7/14/15/24
- `test_predecessor_required_and_skipped_for_rank0`; `test_predecessor_same_commit_only`.
- `test_ci_green_required_pass_fail_pending`; `test_spec_validated_pass_skip`; `test_security_clean_skip_when_unavailable`.
- `test_restricted_always_requires_approval` (parametrized over `requires_approval` true/false, agent/manual) — `requires_human_approval` always true.
- `test_agent_without_allow_requires_approval`; `test_not_frozen_blocks_then_override_clears`.
- `test_evaluate_is_total` (Hypothesis over check/role/restricted combos) — always a `GateEvaluation`, never raises.

**Unit — freeze (`tests/test_freeze.py`):** weekend window wrap-around (Fri 17:00 → Mon 09:00) across `FakeClock` instants; timezone handling; `next_open`.

**Unit — pipeline resolver (`tests/test_pipeline.py`):** AC1/2/15
- `test_restricted_derived_from_policy`; `test_unknown_env_rejected`; `test_ordering_by_rank`; `test_predecessor_lookup`; `test_currently_deployed_returns_last_succeeded_non_rolledback`.

**Unit — engine (`tests/test_engine.py`):** AC4/8/11/12/13/18/19/20
- `test_unrestricted_auto_clears_to_deploying`; `test_restricted_waits_for_approval`.
- `test_approve_triggers_single_deploy`; `test_reject_to_gate_rejected_no_deploy`.
- `test_deploy_success_then_health_pass_to_succeeded`; `test_health_fail_auto_rollback`; `test_health_fail_no_rollback_to_failed`.
- `test_provider_failure_to_failed`; `test_timeout_reconcile_marks_failed`.
- `test_cancel_non_terminal`; `test_cancel_terminal_409`.
- `test_transitions_append_only_and_redacted`.
- `test_full_promotion_chain_dev_staging_production` (with approvals at restricted stages).

**Unit — providers/health (`tests/test_providers.py`, `tests/test_health.py`):** `GitHubDeploymentsProvider.trigger` calls the adapter once and maps status; `WebhookCommandProvider` posts with the secret header; `HttpHealthChecker` retries `retries` times then fails; `CommandHealthChecker` maps exit code.

**API integration (`apps/api/tests/deployments/`, httpx.AsyncClient + Postgres container):** AC1–4,4a,7–10,14,16,17,20,21,22
- `test_pipeline_crud.py`: upsert/get; restricted-unset 422 (AC1); unknown-env 422 (AC2); version conflict 409 (AC3).
- `test_request_and_advance.py`: unrestricted reaches deploying; active-dup 409 (AC4/4a); idempotency (AC17).
- `test_restricted_gate.py`: restricted always creates approval (AC7); agent-deploy needs human (AC16).
- `test_deploy_approval.py`: approve drives deploy; reject blocks; multi-approval; no-self-approval/viewer 403 (AC8/9/10) — reuses F08 `/approvals/{id}/decision`.
- `test_freeze.py`: frozen → gate_rejected; admin override clears; non-admin 403 (AC14).
- `test_cancel.py` (AC20); `test_rbac.py` (AC22); `test_auto_promote_on_merge.py` (AC21).
- `test_provider_callback.py`: HMAC-verified callback emits success/failure and advances state.

**DSL drift (`packages/deploy-core/tests/test_definition.py`):** AC23 — bundled vs example equality; all guard/effect names registered.

**Frontend (`apps/web/tests/deployments/`, Vitest + Testing Library + MSW; Playwright e2e):** AC25
- `PromotionBoard.test.tsx`: columns ordered by rank; currently-deployed + health badges; optimistic Promote + rollback on MSW error.
- `GateChecklist.test.tsx`: renders pass/fail/pending/skip per check.
- `DeploymentDetail.test.tsx`: freeze banner when frozen; approval actions for restricted; initiator approve → inline 403.
- `deployments.spec.ts` (Playwright): drive a dev→staging promotion against MSW including the approval step.

---

## 8. Security & policy considerations

- **Human approval before restricted deploys — inviolable.** `DeploymentGateEvaluator` forces `requires_human_approval=True` for every `is_restricted` environment, and `is_restricted` is derived from the repo `deploy_rules.restricted_environments` (not user-editable to false). There is no input — task `requires_approval.deploy=false`, env `requires_approval=false`, automation trigger — that lets a restricted-env deployment reach `approved` without an `approved` `deploy` `ApprovalRequest`. Double-enforced: the gate sets the flag and the FSM's `gate_passed → approved` transition guard `no_approval_required` cannot match for restricted envs.
- **No self-approval / least privilege.** The deployment initiator (`user:`/`agent:`) is forbidden from resolving its own `deploy` approval (reuses F08's rule); `agent-runner` and `viewer` cannot approve. Approver identity is checked against the env `gate_config` approver group and counted distinctly toward `min_approvals`. This realizes "The agent never self-assigns permissions or expands its own scope" for deploys.
- **Policy is the source of truth.** Allowed/restricted environments and `allow_agent_deploy` come from the audited `RepoPolicySnapshot`; the pipeline config can add gates and ordering but **cannot widen** what policy permits. Agent-initiated promotions arrive only via F04's `promote_environment` `deny + requires_approval` decision — the agent cannot self-deploy.
- **Freeze windows.** Restricted-env deploys are blocked during configured freeze windows; only an `admin` can override, and the override is an audited decision (`freeze_override_by` + a `freeze_override` transition with reason).
- **Provider callback authenticity.** `POST /providers/deployments/callback` verifies an HMAC (`FORGE_DEPLOY_PROVIDER_CALLBACK_SECRET`) before emitting any state event; an unverified callback is rejected (401) and logged.
- **Immutable audit.** `deployment_transition` + `deployment_check_result` are append-only (no UPDATE/DELETE in the repository; recommended DB role denies them), capturing actor, gate decisions, approvals, deploy handles, health results, rollbacks, and freeze overrides — satisfying the Security section's immutable, queryable audit-log requirement and enabling "who approved this prod deploy and why did it roll back?" forensics.
- **Secret redaction.** Provider configs may reference secrets (`headers_secret_ref`, tokens) by reference only; raw secrets are resolved at trigger time from the encrypted vault and never persisted into `deployment.provider_config`, transition payloads, or deploy logs (logs pass through the shared redaction filter before MinIO upload).
- **Concurrency / no double-deploy.** The partial unique active-deployment-per-environment index + row lock + `(deployment_id, idempotency_key)` uniqueness prevent two concurrent or duplicate deploys to the same environment; provider triggers are idempotent on `provider_external_id`.
- **Tenant isolation.** All reads/writes are workspace-scoped; cross-workspace deployment/pipeline access returns 404 (no existence leak).
- **Rate limiting.** Deploy-request, approval-decision, and freeze-override endpoints are per-user rate-limited (reusing the cross-cutting limiter) to prevent accidental deploy storms.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (~3 engineer-weeks: ~1.5 backend engine + gate + providers + health + API/approval extension, ~1 frontend promotion board + pipeline editor + deploy detail, ~0.5 tests/wiring/examples). The FSM reuses F07; the surface area is the provider breadth, gate correctness, rollback safety, and freeze-window time logic.

Key risks:
- **Provider integration variance** (High → mitigated): GitHub Deployments vs Actions `workflow_dispatch` vs generic webhooks have different status models. Mitigation: a narrow `DeployProvider` Protocol with three concrete adapters + `NullDeployProvider`; status normalized to `pending|in_progress|success|failure|error`; callback **and** poll paths; `reconcile_stuck_deployments` backstop.
- **Rollback correctness** (High → mitigated): a bad rollback is worse than the failed deploy. Mitigation: rollback re-promotes the **last-good `succeeded`, non-rolled-back** artifact resolved deterministically by `PipelineResolver`; rollback runs the same deploy+verify path; `rollback_requires_approval` config for sensitive envs; covered by `test_health_fail_auto_rollback` + integration.
- **Gate-bypass for restricted envs** (High → mitigated): the headline failure mode. Mitigation: double-enforced (`requires_human_approval` forced in the evaluator + `no_approval_required` guard can't match restricted) + property test `test_restricted_always_requires_approval` over all combos.
- **Freeze-window time bugs** (Med): wrap-around windows + timezones. Mitigation: pure `freeze.is_frozen` with exhaustive `FakeClock` table tests including the Fri→Mon wrap.
- **Concurrency / double-deploy** (Med): two promotions racing to one env. Mitigation: partial unique active index + row lock + idempotency key; covered by `test_active_dup_409`.
- **Engine reuse coupling to F07** (Med): F31 depends on F07's DSL/guard/effect internals being importable. Mitigation: depend only on the stable `load_definition`/registry/dispatcher surface (already public for F08/F21); if F07 internals shift, the deploy registries are isolated in `forge_deploy`.

---

## 10. Key files / paths (exact)

Create — engine package:
- `packages/deploy-core/pyproject.toml`
- `packages/deploy-core/forge_deploy/{__init__,states,schemas,pipeline,gate,freeze,providers,health,engine,guards,effects,repository,errors}.py`
- `packages/deploy-core/forge_deploy/py.typed`
- `packages/deploy-core/forge_deploy/definitions/deployment_promotion.yaml`
- `packages/deploy-core/tests/{conftest,test_gate,test_freeze,test_pipeline,test_engine,test_providers,test_health,test_definition}.py`

Create — DB + migration:
- `packages/db/forge_db/models/deployment.py`
- `packages/db/migrations/versions/<rev>_f31_deployment_gates.py`

Create — API:
- `apps/api/forge_api/routers/deployments.py`
- `apps/api/forge_api/schemas/deployments.py`
- `apps/api/forge_api/services/deployment_service.py`
- `apps/api/tests/deployments/test_*.py`

Create — worker:
- `apps/worker/forge_worker/tasks/deployments.py` (`advance_deployment`, `evaluate_gate_task`, `trigger_deploy_task`, `run_health_check_task`, `start_rollback_task`, `poll_deploy_status`, `reconcile_stuck_deployments`, `CeleryDeploymentRequester`, beat entries)

Create — frontend:
- `apps/web/app/[workspace]/projects/[projectKey]/deployments/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/deployments/[deploymentId]/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/settings/environments/page.tsx`
- `apps/web/components/deployments/{PromotionBoard,EnvironmentColumn,DeploymentCard,GateChecklist,FreezeBanner,DeploymentTimeline,RollbackDialog,PipelineEditor,EnvironmentGateForm,FreezeWindowEditor}.tsx`
- `apps/web/lib/deployments/{api,queries,mutations,types}.ts`
- `apps/web/tests/deployments/*.{test.tsx,spec.ts}`

Create — community artifacts:
- `examples/deployments/{python-api-pipeline.yaml,deployment_promotion.yaml}`

Edit (extend / wire):
- `packages/db/forge_db/models/enums.py` (append `DeploymentState`, `DeploymentEventType`, `DeploymentKind`, `DeploymentTrigger`, `GateCheckName`, `GateCheckStatus`, `HealthStatus`; the `approval_request.kind` enum already carries `deploy` from the foundation — **no enum change required**)
- `packages/db/forge_db/models/approval.py` (+ migration: add `deployment_id`, nullable `workflow_run_id`, the XOR CHECK, the partial unique index)
- `packages/contracts/forge_contracts/deployment.py` (new module: `DeploymentRequest`, `DeploymentDTO`, `DeploymentRequester` Protocol)
- `apps/api/forge_api/services/approval_service.py` (branch `resolve()` on `kind='deploy'`)
- `apps/api/forge_api/main.py` (mount deployments router) + `forge_api/deps.py` (`get_deployment_engine`, `get_deployment_requester`)
- `packages/integration-sdk/forge_integrations/github/client.py` (extend the authoritative `GitHubIntegration` Protocol / `GitHubAppClient`: create deployment, Actions `workflow_dispatch`, `deployment_status`/`workflow_run` reads) + `packages/contracts/forge_contracts/integration.py` (add the new method signatures to the `GitHubIntegration` Protocol); F08's narrow `GitHubAdapter.get_combined_status` view is reused unchanged for `ci_green`
- `.env.example`, `deploy/.env.production.example` (`FORGE_DEPLOY_PROVIDER_CALLBACK_SECRET`, `FORGE_DEPLOY_DEFAULT_TIMEOUT_S`, `FORGE_DEPLOY_HEALTH_RETRIES`)
- `deploy/docker-compose.yml` (worker `deployments` queue + beat entries), `deploy/scripts/install.sh` (`forge-deploy-logs` MinIO bucket)
- `docs/integrations/github-app-setup.md` (require `deployment`/`deployment_status`/`workflow_run` webhook events)

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md → **Human Approval System → Approval Gate Types**: "Deploy approval | Agent requests env promotion | Required unless policy relaxes for dev" and "Policy override | Always required" — the authoritative gate definition for this slice.
- FORGE_SPEC.md → **Repo Policy System → `deploy_rules`** (`allow_agent_deploy`, `environments`, `restricted_environments`) — the policy source of truth F31 binds to.
- FORGE_SPEC.md → **Task Schema** (`requires_approval.deploy`, `restricted_actions: [deploy_prod]`) — task-level deploy gating composed with repo policy.
- FORGE_SPEC.md → **Workflow Engine** (FSM/DSL, retry/escalation, human gates) — the engine pattern reused by `DeploymentStateMachine`.
- FORGE_SPEC.md → **Phased Roadmap → Phase 3 (V3)**: "Deployment gates and environment promotion workflows" — the roadmap line this slice implements.
- FORGE_SPEC.md → **Security** (immutable audit, policy evaluation on every action, secret redaction, RBAC) — drives §8.
- FORGE_SPEC.md → **Integrations** (GitHub App: PR/CI/review/webhooks) — extended here for the GitHub Deployments/Actions provider.
- Sibling slices (sources of truth for reused contracts): `docs/implementation-slices/v1/F07-feature-workflow-fsm.md` (DSL, guard/effect registries, dispatcher, transition algorithm), `docs/implementation-slices/v1/F08-plan-execute-verify-pr-approval.md` (`approval_request` primitive, `ApprovalService`, `MergeGateEvaluator` pattern, no-self-approval, `GitHubAdapter`, `ValidationReader`), `docs/implementation-slices/v1/F04-repo-policy.md` (`deploy_rules`, `promote_environment` decision), `docs/implementation-slices/v2/F21-workflow-automations.md` (dispatcher pattern, `request_promotion` action hook).
- forge-research-report.md → Symphony "task-as-control-plane" and Open SWE repo-aware patterns — promotions extend the control plane to deployment; provider/health/rollback are disciplined integrations of proven patterns, not novel algorithms: https://openai.com/index/open-source-codex-orchestration-symphony/ , https://github.com/langchain-ai/open-swe
- GitHub Deployments API + Actions `workflow_dispatch` (provider integration): https://docs.github.com/en/rest/deployments and https://docs.github.com/en/actions

---

## 12. Out of scope / future

- **Multi-repo coordinated promotion** — promoting an artifact that spans multiple repos as one atomic release (builds on `v2/F22-multi-repo-execution`); V3 F31 deploys one repo's artifact per pipeline.
- **Canary / blue-green / progressive rollout strategies** (traffic-shifting, percentage rollouts, bake-time gates) — F31 ships all-at-once deploys with a single post-deploy health gate; progressive strategies are a fast-follow on the `DeployProvider` Protocol.
- **Conditional/per-environment advanced policy rules** — owned by the V3 advanced policy engine; F31 uses flat `gate_config` + repo `deploy_rules`.
- **Full RBAC hierarchy for approver groups** (team/role inheritance, SCIM-driven groups) — owned by the V3 multi-team RBAC slice; F31 uses explicit `approver_user_ids`/`approver_team_ids` + `min_approvals`.
- **Scheduled/timed deployments** ("deploy at 02:00", "promote 24h after staging") — event-driven only here; a scheduled trigger pairs with the V2 automations Beat follow-up.
- **Deployment metrics dashboards** (DORA: deployment frequency, lead time, change-fail rate, MTTR) — F31 emits the audit events that an observability slice can aggregate; the dashboard itself is future.
- **Non-GitHub providers** (GitLab CI, Argo CD, Spinnaker, Kubernetes-native) — the `DeployProvider` Protocol is the extension point; only GitHub Actions/Deployments + a generic webhook ship in V3.
- **Incident-driven auto-rollback** — wiring F31 rollback into the incident workflow (`v2/F17-incident-workflows`) so an incident can trigger a rollback is a natural integration, not built here.
