# Feature Specification: Automation lines

**Feature Branch**: `002-automation-lines`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M2 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md` section 4). Issues R10 to R13.

**Builds on**: lettered spec K (`docs/superpowers/specs/2026-11-07-context-memory-and-lines-design.md` sections 3 and 4), ADR-0020 (automation lines are station definitions), ADR-0005, ADR-0007, ADR-0014, and spec `specs/001-governed-pipeline/spec.md`.

## Background

After milestone M1 a governed run is one agent attempt inside a fixed lifecycle. Teams need repeatable multi-step lines: write failing tests first, implement, check locally, wait for CI, retry a bounded number of times, then hand a reviewed pull request to a person. Spec K settles the design: a line is a definition whose states each carry one station (`agent`, `service` or `human`), the station's outcome picks the next state, and lines run on their own durable workflow beside the feature workflow.

Two gaps in today's code must close first. A member can post a human-approval event straight to the transition route, bypassing the approval service. And nothing stops two parallel runs from editing the same files.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Human gates cannot be bypassed (Priority: P1)

A workspace admin requires two approvals and no self-approval on pull request gates. Today a member can skip those rules by posting the approval event to the workflow transition route. After this feature, entering a human gate opens an approval request, and only its resolution moves the run.

**Why this priority**: spec K names it as the prerequisite for all line work, and it closes a governance hole in feature runs that exists today.

**Independent Test**: post `review_approved_by_human` to the transition route of a run at its `pr` gate and assert a 409. Approve the gate through the approval service and assert the run advances with the approver as actor.

**Acceptance Scenarios**:

1. **Given** a feature run at a human gate, **When** any caller posts a `*_by_human` event to the transition route, **Then** the route answers 409 `gate_events_come_from_approvals` and the run does not move.
2. **Given** a feature run that enters `spec_review`, `plan_review` or `awaiting_review`, **When** it arrives, **Then** one approval request for that run and gate exists.
3. **Given** that request resolves `approved` under the approval rules, **When** the resolution is recorded, **Then** the run advances and the transition's actor is the deciding user.
4. **Given** the hook that advances the run fails, **When** a decision is recorded, **Then** the decision stays recorded.

---

### User Story 2 - A line can be written and validated (Priority: P1)

A platform engineer writes a line definition. Swarm Code Factory tells them, before anything runs, whether it is safe: every state has a station, success is routed, no failure routes to success, agents hold no write tools, and untrusted text cannot steer where a service writes.

**Why this priority**: validation is pure and has no runtime dependency, and every later story loads definitions through it.

**Independent Test**: feed the validator the reference `implementation_loop` definition and a set of broken variants. Assert zero errors for the first and the documented error code for each variant.

**Acceptance Scenarios**:

1. **Given** the reference implementation-loop definition from spec K section 4.5, **When** it is validated, **Then** there are no errors.
2. **Given** a definition that routes `failed` to the success target, **When** it is validated, **Then** the error is `FAIL_OPEN_ROUTE`.
3. **Given** a definition whose agent station has no budget, **When** it is validated, **Then** the error is `AGENT_STATION_UNBUDGETED`.
4. **Given** a service input marked task-trusted that reads an agent output, **When** it is validated, **Then** the `trusted_inputs` invariant fails.
5. **Given** a version 1 workflow document, **When** it is parsed, **Then** the result is unchanged from today.

---

### User Story 3 - The implementation loop runs a ticket to a ready pull request (Priority: P1)

A maintainer starts the implementation loop on a task. A tester agent writes failing tests; a service confirms they fail. An implementer agent makes them pass; a service runs lint and tests; the branch is delivered; a service waits for CI. Failures return to the implementer a bounded number of times. The run ends with a pull request marked ready for a person, or parks with a reason.

**Why this priority**: this is the first line and the reason the milestone exists.

**Independent Test**: run the line on the durable test server with scripted agents and a local repository. Drive one path where CI fails twice then passes, and assert the visit records, the final state and the bounded retries.

**Acceptance Scenarios**:

1. **Given** a tester station whose tests pass immediately, **When** `confirm_red` runs, **Then** the outcome is `not_red` and the run returns to the tester.
2. **Given** local checks that fail, **When** the outcome is routed, **Then** the implementer runs again with the failure summary, and the branch is not delivered.
3. **Given** CI with zero check runs, **When** `await_ci` completes, **Then** the outcome is `no_checks` and the run parks for a person.
4. **Given** the implementer reaches its visit limit, **When** it would be entered again, **Then** the outcome is `visit_limit` and the run parks.
5. **Given** the run's token budget is spent, **When** an agent station is next, **Then** the agent is not called and the run fails with `budget_exhausted`.
6. **Given** a service name that is not bound on the worker, **When** its station runs, **Then** the outcome is `not_configured` and only this run fails.
7. **Given** a deployment on the non-durable engine, **When** a line run is started, **Then** the answer is 503 `durable_engine_required`.

---

### User Story 4 - A person decides inside a line (Priority: P2)

A line author places a human station on the `pr` gate. The run waits there until the approval request resolves. The workflow reads the decision from the database, so a forged wake-up signal cannot approve anything.

**Why this priority**: the first line ends before the human gate, so it ships without this. Review and triage lines need it.

**Independent Test**: run a two-station line with a human station. Send the wake-up signal with no decision and assert the run still waits. Record a real approval and assert it advances.

**Acceptance Scenarios**:

1. **Given** a run at a human station, **When** the wake-up signal arrives with no recorded decision, **Then** the run keeps waiting.
2. **Given** a recorded approval by a user, **When** the workflow next wakes, **Then** the outcome is `approved` and the actor is that user.
3. **Given** the gate passes its deadline undecided, **When** the workflow wakes, **Then** the gate expires and the outcome is `expired`.
4. **Given** the wake-up signal is lost, **When** the periodic wake occurs, **Then** the decision is still found.

---

### User Story 5 - Parallel runs do not collide (Priority: P2)

Three tickets run at once on one repository. Two of them need the same file. SCF records which paths each run has changed, holds the second run when its paths overlap an active claim, and brings finished runs up to date with the base branch one at a time before their final check, so each pull request is checked against the code it will land on.

**Why this priority**: it matters only once several lines run together, and dogfooding (R27) needs it.

**Independent Test**: start two runs whose changes touch the same path and assert the second gets `claim_conflict` and parks or waits. Start two runs with disjoint paths and assert both pass the integration step in sequence, never concurrently.

**Acceptance Scenarios**:

1. **Given** an active run that has claimed a path, **When** a second run in the same repository claims an overlapping path, **Then** the second run's outcome is `claim_conflict` and it does not deliver its branch.
2. **Given** a run reaches a terminal state, **When** it ends, **Then** its claims are released.
3. **Given** two runs ready to integrate, **When** both request the integration step, **Then** the steps run one after the other per repository.
4. **Given** the base branch moved since a run branched, **When** integration brings the run branch up to date and the checks then fail, **Then** the outcome is `checks_failed` and the pull request is not marked ready.
5. **Given** bringing the branch up to date produces a conflict, **When** integration runs, **Then** the outcome is `conflict`, the branch is left unchanged and the run returns to the implementer or parks.

---

### Edge Cases

- A line definition from an older SCF release fails today's validation: new runs are refused with 409 `line_invalid`; in-flight runs keep their pinned revision.
- A station input path does not resolve: the outcome is `failed` with `input_unresolved`, before the service is called.
- An agent returns a value outside its declared decisions: the outcome is `failed`.
- A run is cancelled while a gate is open: the gate expires with reason `run_cancelled`; a draft pull request stays open with a comment.
- A run parks: only a user with write access can resume it; agents and automations cannot.
- A run holding claims crashes: claims are released when the run reaches a terminal state or after a bounded lease.
- A task has no repository target or several: a line that references `task.repo` refuses to start with 409 `repo_target_ambiguous`.
- Replay of the existing feature workflow histories is unchanged by this feature.

## Requirements *(mandatory)*

### Functional Requirements

**Gates through approvals (R12)**

- **FR-001**: Entering `spec_review`, `plan_review` or `awaiting_review` on a feature run MUST open an approval request for that run and gate.
- **FR-002**: The transition route MUST refuse the three `*_by_human` events with 409 `gate_events_come_from_approvals`.
- **FR-003**: When such a request resolves `approved`, SCF MUST submit the matching event to the run with the deciding user as actor. A failure to submit MUST NOT undo the recorded decision.

**Line contract (R10)**

- **FR-004**: SCF MUST define a strict line definition (DSL version 2) with `initial`, `terminals`, `budget`, `concurrency`, `stations` and `transitions`, as settled in spec K section 4.1. Unknown fields are rejected.
- **FR-005**: Parsing MUST return today's version 1 definition for documents without a DSL version or with version 1, and a line definition for version 2.
- **FR-006**: A service catalog MUST describe each service's params, inputs with trust level, outputs, extra outcomes, limits, idempotency, effect and credential name, as metadata only.
- **FR-007**: A pure validator MUST report the error codes listed in spec K section 4.1, including `FAIL_OPEN_ROUTE`, and MUST always apply the line invariants `no_merge`, `agent_no_write_tools`, `trusted_inputs` and `agent_outputs_not_guards`.

**Running lines (R11)**

- **FR-008**: Lines MUST run on a durable station workflow separate from the feature workflow. The feature workflow's behaviour and replay MUST NOT change.
- **FR-009**: Each station visit MUST be recorded with state, visit number, kind, status, outcome, outputs, links, cost and times, and be readable through the API.
- **FR-010**: An outcome with no edge MUST park the run for person-fixable outcomes and fail it for failure outcomes, as listed in spec K section 4.1.
- **FR-011**: Visits per station MUST be bounded by `max_visits`, and the run's token budget MUST be enforced before each agent station.
- **FR-012**: A service that is not bound on the worker MUST yield `not_configured`. No service capable of integrating a change into the base branch may exist in the catalog.
- **FR-013**: Agent stations MUST never receive tools that write outside the run's worktree.
- **FR-014**: Human stations MUST resolve only through the approval service, with the decision read from the database. Wake-up signals MUST carry nothing the workflow trusts.
- **FR-015**: On a station run the transition route MUST accept only `resume` and `cancel`, and only from a user with write access.
- **FR-016**: SCF MUST ship the implementation-loop line of spec K section 4.5 with its services bound: failing tests confirmed first, local checks before delivery, CI awaited, bounded retries, pull request marked ready, no integration into the base branch.
- **FR-017**: Starting a line on the non-durable engine MUST be refused with 503 `durable_engine_required`.

**Parallel safety (R13)**

- **FR-018**: SCF MUST record, per active run, the repository paths the run has changed, and MUST refuse a claim that overlaps another active run's claim in the same repository.
- **FR-019**: Claims MUST be released when a run reaches a terminal state, and MUST expire after a bounded lease if the run stops reporting.
- **FR-020**: SCF MUST provide a per-repository serialised integration step that brings a run branch up to date with the base branch and re-runs the checks, one run at a time. [NEEDS CLARIFICATION: the roadmap calls R13 a "serialised merge step", while ADR-0020 forbids any service that integrates into the base branch until a `pr` gate guards it. Default assumed: the step updates the run branch only; integration into the base branch stays with a person until R22.]

### Key Entities

- **Line definition**: a version 2 workflow definition: stations, transitions, terminals, budget and concurrency.
- **Station**: one unit of work in a state: `agent`, `service` or `human`.
- **Service catalog entry**: metadata for one service a line may call.
- **Station visit**: the record of one entry into a station by one run.
- **Approval request**: the human decision for a gate, tied to a run or a station visit.
- **File claim**: a run's hold on a set of repository paths while it is active.
- **Integration slot**: the per-repository turn a run takes to update and re-check its branch.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Zero feature or line runs advance past a human gate without a resolved approval request.
- **SC-002**: The validator rejects 100 percent of a fixed corpus of unsafe line definitions, each with the expected code, and accepts the reference line.
- **SC-003**: A ticket run through the implementation loop ends in exactly one of: pull request ready for review, parked with a reason, or failed with a reason. No run ends without one.
- **SC-004**: No run exceeds its declared token budget or a station's visit limit.
- **SC-005**: With three concurrent runs on one repository, zero pairs of active runs hold overlapping claims, and zero integration steps overlap in time.
- **SC-006**: Existing feature-workflow replay fixtures pass unchanged.

## Assumptions

- Milestone M1 has landed: the durable worker has real activities, and service-side checks and CI read-back exist to reuse.
- Spec K section 4.4 (the signed `workflow_template` installer and `verify_package`) is not in the roadmap's M2 issue list. The first line is loaded from a definition bundled with SCF until the installer is scheduled. [NEEDS CLARIFICATION: ADR-0020 says lines are installed only from verified packages. Is a bundled first line acceptable for M2, or must the installer be added to the roadmap before R11?]
- Dependency-aware scheduling, tracker triggers, lines without a task, fan-out inside a run and compensation on cancel stay out of scope, as in spec K section 4.6.
- `max_usd` is recorded and alerts but does not stop a run; `max_tokens` is the enforced cap (spec K section 4.7).
- Yield and cost targets quoted in spec K come from another project's self-reported figures. They are leads for milestone M5, not requirements here.
