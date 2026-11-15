# Feature Specification: Outer loop

**Feature Branch**: `010-outer-loop`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M10 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`
section 4): "Feature flags, canary and automatic rollback for agent changes. True lead time and
per-change outcomes. Replace the DORA proxy. Cost per delivered change and spend caps."

**Builds on**: lettered spec B (`docs/superpowers/specs/2026-09-19-progressive-delivery-design.md`),
lettered spec C (`docs/superpowers/specs/2026-09-19-outer-loop-measurement-design.md`), lettered
spec D (`docs/superpowers/specs/2026-09-19-change-economics-design.md`), all three in Draft status;
lettered spec A (`docs/superpowers/specs/2026-09-19-risk-classification-spine-design.md`) for the
risk class; ADR-0005 (machines propose, people decide), ADR-0007 (production paths fail closed),
ADR-0008 (enforced budgets, unpriced is never free). Roadmap issues: R45, R46, R47. Earlier roadmap
work this depends on: R22 (risk-proportionate auto-merge), R23 (factory metric definitions).

## Context

What exists today, verified in the code:

- `packages/deploy-core` has a deployment state machine, a policy gate, freeze windows, post-deploy
  health checkers (`scf_deploy/health.py`) and an automatic rollback path that is off by default:
  a failed health check after a full deployment moves to `rolling_back` when the gate's
  `auto_rollback` setting is on (`scf_deploy/definitions/deployment_promotion.yaml`,
  `scf_deploy/schemas.py`). A deployment is all or nothing. There is no rollout strategy, no
  canary state and no feature flag store anywhere in `packages/` or `apps/`.
- The automation action that would trigger a deployment uses a mock dispatcher
  (`apps/worker/scf_worker/tasks/automations.py`, `_build_executor`).
- `packages/observability/scf_obs/analytics/dora.py` reports lead time as the promotion's own
  request-to-finish duration and says in its docstring that this is a proxy. The `deployment` row
  holds a `commit_sha` and no authored or landed timestamp.
- Swarm Code Factory already learns when a pull request closes and whether it landed
  (`apps/api/scf_api/routers/integration.py`, pull-request outcome feedback).
- Cost is metered per model call in the `cost_event` ledger with `agent_run_id`, `task_id` and
  `workflow_run_id` (`packages/db/scf_db/models/cost.py`). Local-CLI calls are recorded as
  unpriced (`packages/observability/scf_obs/cost/meter.py`). No aggregation by delivered change
  exists.
- A `budget` table with a `hard_cap` flag exists and `evaluate_budget`
  (`scf_obs/analytics/budgets.py`) reports an alert when it is exceeded. Nothing stops a run on
  that alert. A run has a token budget (`packages/agent-runtime/scf_agent/budget.py`) and no
  currency cap.
- Lettered spec A is not implemented at the audited commit. This feature takes the risk class and
  the identity of a change from R20 to R23 and does not define them.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - See what a delivered change cost and stop runaway spend (Priority: P1)

A workspace owner opens a delivered change and sees what it cost, itemised, with unpriced usage
shown separately. They set a spend cap for the workspace or a project. When the cap is reached,
SCF refuses to start new agent runs under it and stops runs that are in flight, and it says why.

**Why this priority**: An autonomous loop without a currency cap can spend without limit, and that
is the failure owners fear most. It is also the smallest slice: the ledger and the budget records
already exist.

**Independent Test**: Seed cost events for one change and a budget below their total. Read the
per-change cost and attempt to start a run. Needs nothing from Stories 2 to 4.

**Acceptance Scenarios**:

1. **Given** a delivered change with priced model calls across several runs, **When** its cost is
   requested, **Then** the response gives a total and its components, and the model-usage component
   equals the sum of the ledger's entries for those runs.
2. **Given** a change with local-CLI usage, **When** its cost is requested, **Then** the unpriced
   volume is reported as its own figure and is not counted as zero cost.
3. **Given** a hard cap that is already spent, **When** a run is requested under that scope by the
   API or by a tracker label, **Then** it is refused with a reason that names the cap, and the
   refusal is recorded.
4. **Given** a run in flight, **When** its scope's hard cap is crossed, **Then** the run stops
   before its next model call and ends in a failed state that names the cap.
5. **Given** a cap that is not marked hard, **When** it is crossed, **Then** runs continue and the
   overrun is reported.
6. **Given** any cost report, **When** it is read, **Then** no figure is attributed to a person.

---

### User Story 2 - Know the true lead time of a change and where it waited (Priority: P2)

An engineering lead asks how long changes take from first commit to running in the target
environment, and how much of that was waiting. SCF answers from recorded timestamps and stops
reporting the promotion duration as lead time.

**Why this priority**: The current figure measures the wrong interval and the code says so. Until
it is replaced, no factory metric that mentions lead time can be published.

**Independent Test**: Seed a change with known authored, landed and deployed times and read the
lead-time report. Needs nothing from Stories 1, 3 or 4.

**Acceptance Scenarios**:

1. **Given** a change with recorded first-commit, landed and deployment-finished times, **When**
   lead time is requested, **Then** it equals deployment-finished minus first-commit.
2. **Given** the same change, **When** lead time is requested, **Then** the report separates
   waiting time (queued for review, approval or deployment) from active time, and the parts sum to
   the total.
3. **Given** a deployment whose change has no recorded commit time, **When** lead time is
   requested, **Then** that change is reported as unmeasured and is excluded from the aggregate,
   never filled in with the promotion duration.
4. **Given** the DORA report, **When** it is read after this feature, **Then** its lead time is the
   commit-to-production figure and the proxy note is gone from code and documents.

---

### User Story 3 - Get a verdict on whether each change worked (Priority: P3)

A lead asks "did the last ten changes work?" and receives ten verdicts, each derived from what
happened after the change shipped, not a dashboard to interpret.

**Why this priority**: A factory that cannot say whether its output worked is optimising
throughput alone. It follows Story 2 because it needs the same change timeline.

**Independent Test**: Seed ten delivered changes, some followed by a rollback, a revert or a linked
incident, and request the last ten verdicts.

**Acceptance Scenarios**:

1. **Given** a change whose deployment stayed healthy through the observation window with no
   rollback, revert or linked incident, **When** verdicts are requested, **Then** it is `worked`.
2. **Given** a change that was rolled back, reverted, or linked to an incident inside the window,
   **When** verdicts are requested, **Then** it is `failed` and the verdict names the evidence.
3. **Given** a change still inside its window, or one with no post-release signal, **When**
   verdicts are requested, **Then** it is `pending` or `unmeasured`, never `worked`.
4. **Given** a request for the last ten verdicts, **When** it is answered, **Then** it returns the
   ten most recently delivered changes of the caller's workspace, newest first.

---

### User Story 4 - Ship an agent change gradually and have it reverse itself (Priority: P4)

A platform engineer lets higher-risk agent changes go out as a canary. SCF promotes the canary
when its health signals hold and aborts and rolls it back when they breach, without a person on
call. A change can also ship dark behind a flag bound to it, and flags that no longer matter are
reported.

**Why this priority**: It is the largest slice, it depends on risk classes and auto-merge (R20 to
R22), and it is only safe once Stories 1 to 3 can show its effect.

**Independent Test**: Drive the deployment state machine with a scripted health checker: one run
with passing signals, one with a breach. Flags are testable alone through their own API.

**Acceptance Scenarios**:

1. **Given** a change whose risk class requires a gated rollout, **When** it is deployed, **Then**
   it enters a canary stage before full promotion with no operator action.
2. **Given** a canary whose health signals pass for the configured evaluation period, **When** the
   period ends, **Then** it is promoted and the promotion is recorded with the signals used.
3. **Given** a canary whose health signal breaches, **When** the breach is observed, **Then** the
   rollout aborts, the previous version is restored, and the abort and rollback are recorded with
   the breaching signal.
4. **Given** a repository with no risk rules, **When** a change is deployed, **Then** it is
   promoted directly as today.
5. **Given** a flag bound to a change, **When** it is evaluated for a workspace and a subject,
   **Then** the same inputs always give the same answer, and an unknown flag evaluates to off.
6. **Given** a flag whose state has not changed for the configured number of days, **When** stale
   flags are listed, **Then** it appears in the list.
7. **Given** a deployment provider that cannot restore a previous version, **When** a canary
   breaches, **Then** the rollout stops in a failed state that asks for a person, and it is never
   recorded as rolled back.

---

### Edge Cases

- A hard cap is crossed between two model calls of the same run. The run stops before the next
  call; the overshoot is at most one call.
- The cost ledger or the budget store is unreachable when a run is requested. The request is
  refused (ADR-0007), not admitted without a check.
- A cap is denominated in a currency other than the ledger's. Conversion uses the existing rate
  book; a missing rate is a refusal with a reason, not a silent pass.
- All usage under a cap is unpriced. The cap cannot be evaluated in currency. The behaviour must be
  stated, not defaulted to "under the cap".
- A change is delivered by several pull requests, or one pull request is deployed several times.
  Lead time uses the first commit and the first successful deployment to the target environment.
- A change is reverted and re-landed. Each landing is its own delivered change; the revert is
  evidence for the first one's verdict.
- A change ships behind a flag that is never turned on. Its verdict is `unmeasured`.
- Health signals are missing during a canary. Missing data is treated as a breach after a stated
  grace period, not as health.
- Two canaries target the same environment. The second waits or is refused; they do not overlap.

## Requirements *(mandatory)*

### Functional Requirements

**Cost per delivered change and spend caps (R47)**

- **FR-001**: The system MUST report, for one delivered change, a total cost and its components:
  model usage, and each further component it can measure (CI compute, review, rework), with any
  component it cannot measure reported as not measured.
- **FR-002**: The model-usage component MUST equal the sum of the existing cost ledger entries for
  the runs that produced the change.
- **FR-003**: Unpriced usage MUST be reported as a separate volume and MUST NOT contribute zero to
  a currency total without being flagged.
- **FR-004**: Cost MUST be reported per change, per project and per workspace, and MUST NOT be
  reported per person.
- **FR-005**: The system MUST refuse to start an agent run when a hard cap that covers it is spent,
  on every entry path, and MUST record the refusal with the cap's identity.
- **FR-006**: The system MUST stop an in-flight run before its next model call when a hard cap that
  covers it is crossed, and MUST end the run in a failed state that names the cap.
- **FR-007**: A run MUST be able to carry its own currency cap in addition to its token budget.
- **FR-008**: When the cap cannot be evaluated (store unreachable, missing exchange rate), the
  system MUST refuse new runs under that cap and say why.

**True lead time and outcomes (R46)**

- **FR-009**: The system MUST record, for each delivered change, when its first commit was
  authored, when it landed on the main branch and when its deployment to the target environment
  finished.
- **FR-010**: Lead time MUST be reported as first-commit to deployment-finished, with waiting and
  active portions reported separately and summing to the total.
- **FR-011**: A change with a missing timestamp MUST be reported as unmeasured and excluded from
  aggregates. The promotion duration MUST NOT be substituted.
- **FR-012**: The existing DORA report MUST use the true lead time, and the statement that it is a
  proxy MUST be removed from code and documents once that is so.
- **FR-013**: The system MUST produce one outcome verdict per delivered change from the set
  `worked`, `failed`, `pending`, `unmeasured`, with the evidence that produced it.
- **FR-014**: A verdict MUST be `failed` when the change was rolled back, reverted, or linked to an
  incident within the observation window. [NEEDS CLARIFICATION: the length of the observation
  window and whether it is configurable per project. The issues assume seven days, configurable.]
- **FR-015**: The system MUST return the most recent N verdicts for a workspace, newest first.
- **FR-016**: No lead-time or outcome figure MUST be attributable to an individual.

**Flags, canary and automatic rollback (R45)**

- **FR-017**: The system MUST store feature flags scoped to a workspace, each optionally bound to
  the change that introduced it, and MUST evaluate a flag deterministically for a given subject.
- **FR-018**: An unknown or disabled flag MUST evaluate to off.
- **FR-019**: The system MUST list flags whose state has not changed for a configured number of
  days as stale.
- **FR-020**: Flags MUST NOT be usable as a general configuration store: a flag holds an on/off
  state and a rollout rule, nothing else.
- **FR-021**: A deployment MUST select its rollout strategy from the change's risk class: direct
  promotion where the class allows it, canary where it requires a gated rollout.
- **FR-022**: A canary MUST be promoted or aborted on health signals, and MUST treat missing
  signals as a breach after a stated grace period.
- **FR-023**: An aborted canary MUST be rolled back automatically where the provider supports it,
  and the abort, the signal and the rollback MUST be recorded in the deployment's history and the
  audit log.
- **FR-024**: Where the provider cannot restore the previous version, the rollout MUST stop in a
  failed state that requires a person, and MUST NOT be recorded as rolled back.
- **FR-025**: A repository without risk rules MUST keep today's direct promotion.
- **FR-026**: The live rollout state of a deployment MUST be readable by workspace members.

### Key Entities

- **Delivered change**: one change that landed on the main branch and was deployed. Its identity is
  the one R23 defines for the factory metrics. It links agent runs, a pull request, a commit and
  deployments. [NEEDS CLARIFICATION: R23 has not landed at the audited commit; if it identifies a
  change by something other than repository plus landed commit, these requirements follow it.]
- **Change timeline**: first-commit, landed and deployment-finished times for a delivered change,
  plus the intervals classed as waiting or active.
- **Outcome verdict**: `worked`, `failed`, `pending` or `unmeasured` for a delivered change, with
  evidence and the time it was decided.
- **Change cost**: total and components for a delivered change, with unpriced volume alongside.
- **Spend cap**: an amount, currency, period and scope (workspace or project), hard or advisory.
  The `budget` record exists today.
- **Feature flag**: a workspace-scoped on/off state with a rollout rule, optionally bound to a
  delivered change, with a last-changed time.
- **Rollout**: the staged progress of one deployment: strategy, current stage, health evaluations,
  and the promotion or abort decision.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For any delivered change, an owner can read its itemised cost in one request, and the
  model-usage figure reconciles to the ledger exactly.
- **SC-002**: With a hard cap spent, no new run starts under it and an in-flight run overshoots by
  at most one model call.
- **SC-003**: Reported lead time for a seeded change equals the commit-to-production interval to
  the second, and the word "proxy" no longer describes lead time anywhere in the repository.
- **SC-004**: "Did the last ten changes work?" is answered by one request that returns ten
  verdicts, each with evidence.
- **SC-005**: A canary with a seeded health breach is aborted and restored with no human action,
  and the record shows the breaching signal.
- **SC-006**: A repository without risk rules shows no change in deployment behaviour.
- **SC-007**: Every flag unchanged past the staleness threshold appears in the stale list.

## Assumptions

- Lettered specs B, C and D are Draft and say "revise after A". This spec fixes their scope for the
  roadmap issues and leaves interfaces to planning.
- R20 to R23 land first and provide the risk class and the delivered-change identity. If they do
  not provide a change identity, R46 defines it as repository plus landed commit.
- Health signals for a canary come from the existing post-deploy health checkers. Binding to an
  external metrics system is out of scope.
- Review time and rework are measured only where SCF already holds the timestamps (approval
  requests and decisions, follow-up runs on the same task). Support cost is out of scope for this
  version, as lettered spec D section 5 leaves open.
- Spec B's scheduled rollback rehearsal, spec C's semantic layer and spec D's cost-against-outcome
  view are not in roadmap issues R45 to R47. They stay as design input for later work.
- Ring deployments are treated as a canary with a wider cohort and are not modelled separately.
- Flag evaluation is served by SCF's API. Evaluation at a network edge or in a sidecar, raised in
  lettered spec B section 5, is out of scope.
