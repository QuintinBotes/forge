# Feature Specification: One governed pipeline

**Feature Branch**: `001-governed-pipeline`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M1 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md` section 4): "The path that acts and the path that governs are not joined, so the trust layer applies to nothing an agent ships." Issues R04 to R09.

**Builds on**: lettered spec G (`docs/superpowers/specs/2026-11-07-agents-that-act-design.md` sections 3.3, 4, 4a), `docs/trust-layer.md`, ADR-0005 (machines propose, people decide), ADR-0007 (production paths fail closed), ADR-0008 (gated tool belt, scoped credentials), ADR-0014 (untrusted content cannot drive privileged tools).

## Background

Forge has two pipelines today.

- The **acting path** takes a labelled issue or an API call, runs a sandboxed agent on a Celery worker and creates a pull request. Nothing checks, reviews, attacks or attests that change.
- The **governed path** is the workflow state machine (`created` to `closed`) with approval gates, a red-team gate and signed attestations. On the durable engine every activity that would run an agent, run checks, create a pull request, create an approval or run the red-team scan refuses, so no agent change can travel it.

This feature makes them one path: every change an agent ships has passed independent verification and carries a signed attestation.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An issue becomes a governed change (Priority: P1)

A maintainer labels an issue for forge. Forge starts one workflow run for it. The agent works in its sandbox, forge re-checks the result itself, creates the pull request and opens a `pr` approval gate tied to that workflow run. The maintainer sees one run with its states, checks and gate, instead of an agent run that ends at a pull request.

**Why this priority**: every other story in this feature, and milestones M2 to M5, attach to this path. Without it the trust layer still governs nothing.

**Independent Test**: start a run from an issue fixture on the durable engine with a scripted model and a local bare repository. Assert the workflow run reaches `awaiting_review`, the agent run row is linked to it, a branch was delivered and exactly one `pr` approval request names the workflow run.

**Acceptance Scenarios**:

1. **Given** a workspace on the durable engine with a connected repository, **When** an authorised sender labels an issue for forge, **Then** forge creates one workflow run, and the agent run it starts is linked to that workflow run.
2. **Given** a governed run whose agent has committed a change, **When** verification passes, **Then** forge creates one pull request and one pending `pr` approval request that carries the workflow run id.
3. **Given** a worker with a service left unconfigured, **When** a run reaches the activity that needs it, **Then** the run stops with a named refusal and no pull request exists.
4. **Given** the durable engine is selected, **When** a caller starts a repository-changing agent run that is not linked to a workflow run, **Then** forge refuses to deliver a pull request for it and says why.
5. **Given** an activity is retried after a worker restart, **When** it runs again with the same input, **Then** no second agent run, pull request or approval request is created.

---

### User Story 2 - Forge checks the work itself (Priority: P1)

A reviewer wants to know that the checks on a change were run by forge, not reported by the agent that wrote it. After the agent finishes, forge runs the repository's declared commands on a clean checkout of the delivered commit, using the command definitions from the base branch. After the pull request exists, forge reads the repository's CI result for that commit and uses it as a condition of the final gate.

**Why this priority**: self-reported checks are the largest verification gap in the audit. Attestation (story 4) has nothing truthful to sign without it.

**Independent Test**: run the check stage against a branch whose agent edited the policy file to make the test command trivial. Assert forge ran the base branch's command and the stage failed.

**Acceptance Scenarios**:

1. **Given** an agent run that reported green tests, **When** the declared test command fails on a clean checkout of its commit, **Then** the run does not create a pull request and returns to the agent within its retry budget.
2. **Given** a change that edits the repository's command declarations, **When** forge verifies it, **Then** forge uses the declarations from the base branch.
3. **Given** a repository that declares no check command, **When** forge verifies a change, **Then** the result is a failure with reason `no_checks_declared`, never a pass.
4. **Given** a pull request whose CI has zero check runs or a failing run, **When** a person approves the `pr` gate, **Then** the run does not advance to its final state and the reason is shown.
5. **Given** a completed verification, **When** a user reads the run, **Then** each check shows its command id, exit status, commit and time.

---

### User Story 3 - A second model reviews and an adversary attacks (Priority: P2)

A team lead does not want one model to be builder, tester and judge. Before the pull request is created, a reviewer on a different model family reads the diff against the acceptance criteria and returns a verdict. An adversary on a different family tries to write a failing test or name a violated criterion. A change that either one rejects goes back to the builder or to a person.

**Why this priority**: independence from the generator is what makes a verdict worth recording. It depends on story 1 and is separable from story 2.

**Independent Test**: run the review stage with a scripted reviewer that returns `request_changes`. Assert no pull request is created, the finding is recorded and the builder's next attempt receives it. Repeat with builder and reviewer on the same family and assert a refusal.

**Acceptance Scenarios**:

1. **Given** a change that passed its checks, **When** the reviewer returns `request_changes` with findings, **Then** no pull request is created and the findings reach the builder's next attempt.
2. **Given** a reviewer or adversary configured on the builder's model family, **When** the stage starts, **Then** it refuses before any model call.
3. **Given** no reviewer is configured, **When** a governed run reaches review, **Then** the run stops for a person with reason `reviewer_not_configured`.
4. **Given** an adversary whose authored test fails when executed, **When** the scan completes, **Then** the verdict is `blocked`, the evidence is recorded and the change does not proceed.
5. **Given** no adversary is configured, **When** a governed run reaches the scan, **Then** nothing records `survived`; the run stops or records `not_run` according to the workspace's explicit setting.

---

### User Story 4 - Every agent pull request carries a signed attestation (Priority: P2)

An auditor opens an agent pull request and finds an attestation reference in its description. With forge's public key they verify offline which model built the change, which sandbox tier it ran in, which checks forge ran, what the reviewer and adversary concluded and which commit it covers. The check still verifies after forge restarts.

**Why this priority**: this is the product thesis (verifiable delivery). It needs the truthful inputs from stories 2 and 3.

**Independent Test**: complete a governed run in a test, restart the signer, and verify the stored attestation with the offline verifier. Assert exit code 0 and that the statement names the head commit and the check results.

**Acceptance Scenarios**:

1. **Given** a governed run that creates a pull request, **When** the pull request exists, **Then** an attestation for the head commit is stored and its id and digest appear in the pull request description.
2. **Given** a deployment that sets no signing key, **When** the service restarts, **Then** attestations signed before the restart still verify.
3. **Given** a signing key that is set but malformed, **When** forge starts signing, **Then** it refuses.
4. **Given** a `pr` gate that a person approves, **When** the approval resolves, **Then** a second attestation records the approver, as today.

---

### User Story 5 - Held-out scenarios (Priority: P3)

A product owner writes acceptance scenarios the builder must never see. Forge stores them outside the repository and outside every prompt, runs them against the finished change and reports only pass and fail counts to the builder.

**Why this priority**: it is the strongest independence signal the research found, but it needs authored scenarios, so it follows the gates that work with no extra input.

**Independent Test**: attach a scenario to a task, run a governed run with a scripted model, and assert that no model prompt, tool result or run event contains the scenario text, and that a failing scenario stops the pull request.

**Acceptance Scenarios**:

1. **Given** a task with held-out scenarios, **When** the builder runs, **Then** no prompt, tool output, context bundle or event visible to the builder contains scenario content.
2. **Given** a change that passes visible checks and fails a held-out scenario, **When** verification completes, **Then** no pull request is created and the builder is told only how many scenarios failed.
3. **Given** a task with no held-out scenarios, **When** verification completes, **Then** the record says `none_defined` and does not claim a pass.

---

### Edge Cases

- The agent finishes with no commits: the run ends as `no_changes`; no checks, review or pull request.
- The worker restarts between delivering the branch and creating the pull request: the retried activity finds the existing pull request.
- The reviewer returns malformed output: treated as a failed review, never as approval.
- CI is still running when a person approves: the gate decision is kept; the run advances only when forge reads a green result.
- The issue is edited after the run starts: the run uses the content fetched at dispatch.
- A held-out scenario cannot be executed (missing command, timeout): counted as failed, with the reason recorded.
- The retry budget ends during review or scan failures: the run stops for a person, as it does for failed checks.
- A deployment on the non-durable engine: the direct acting path still works and labels its pull requests as ungoverned.

## Requirements *(mandatory)*

### Functional Requirements

**Joining the paths**

- **FR-001**: On the durable engine, the API MUST store workflow runs in the shared database so the worker that executes the run can read and update them, and run ownership MUST survive an API restart.
- **FR-002**: The durable worker MUST have a real implementation bound for every workflow activity. An activity whose dependency is missing MUST refuse with a named, non-retryable error (ADR-0007).
- **FR-003**: An agent run started by a workflow run MUST be recorded with a link to that workflow run and MUST use the same sandbox, tool belt, budget and event stream as the acting path does today.
- **FR-004**: Every activity with an external effect (agent run, pull request, approval request, notification) MUST be idempotent on its idempotency key.
- **FR-005**: Issue-label dispatch and the run-creation API MUST start a workflow run for any repository-changing run when the durable engine is selected. [NEEDS CLARIFICATION: on a deployment without the durable engine, should repository-changing runs be refused, or allowed and marked ungoverned? Default assumed: allowed and marked.]
- **FR-006**: A run started from an issue MUST be tied to a board task. [NEEDS CLARIFICATION: does an issue-triggered run need a human spec approval before the agent starts, or does a label applied by an authorised sender count as that approval? Default assumed: the label counts, and the run enters at the ready-to-execute state.]
- **FR-007**: When the durable engine is selected, forge MUST NOT deliver a pull request for an agent run that has no workflow run.

**Independent checks**

- **FR-008**: After the agent finishes, forge MUST run the repository's declared lint, type-check and test commands itself, in a sandbox, on a clean checkout of the delivered commit.
- **FR-009**: Check commands MUST be taken from the base branch's policy, not from the change.
- **FR-010**: A repository with no declared command, and a pull request with zero CI check runs, MUST both count as failures.
- **FR-011**: Forge MUST read CI status for the pull request's head commit itself and use it as a condition of the final gate. Caller-supplied status MUST NOT satisfy the condition.
- **FR-012**: Each check result MUST be recorded with command id, exit status, commit, time and a bounded, redacted output tail, and be readable through the API.

**Independent review and adversary**

- **FR-013**: A reviewer agent with read-only tools MUST review the diff against the acceptance criteria before a pull request is created, and return `approve` or `request_changes` with structured findings.
- **FR-014**: The reviewer and the adversary MUST each run on a model family different from the builder's. A same-family configuration MUST be refused before any model call.
- **FR-015**: A `request_changes` verdict, a `blocked` red-team verdict or an unparseable verdict MUST stop pull request creation and return the findings to the builder within the retry budget, then to a person.
- **FR-016**: The red-team scan MUST run a real adversary against the candidate diff in a sandbox, and record verdict, kind, evidence and both model ids.
- **FR-017**: A stand-in verdict MUST NOT be recorded as `survived`. With no adversary configured the run stops, unless the workspace explicitly turns the scan off, in which case the record and the attestation say `not_run`.

**Attestation**

- **FR-018**: When a governed run creates a pull request, forge MUST store a signed attestation that identifies the head commit and records builder model, sandbox tier, policy hash, tool names, check results, reviewer verdict and red-team verdict.
- **FR-019**: The pull request description MUST carry the attestation id, payload digest and key id.
- **FR-020**: With no signing key configured, forge MUST use a key that is stable across restarts and shared by the processes that sign and verify. A malformed configured key MUST fail closed.
- **FR-021**: Approving the `pr` gate MUST still record an attestation that names the approver.

**Held-out scenarios**

- **FR-022**: Users with write access MUST be able to attach held-out scenarios to a task. [NEEDS CLARIFICATION: who may author them: people only, or also a separate scenario-writing agent? Default assumed: people and API clients only.]
- **FR-023**: Held-out scenario content MUST NOT appear in the builder's objective, context bundle, tool outputs, run events, the repository or the pull request.
- **FR-024**: The verifier MUST run each held-out scenario against the delivered commit and record a pass or fail per scenario. Feedback to the builder MUST be limited to counts.

### Key Entities

- **Workflow run**: the governed lifecycle of one task. Holds state, transitions and the links to its agent runs, checks, verdicts, approvals and attestations.
- **Agent run**: one execution of an agent in a role (builder, reviewer, adversary), with model, sandbox tier, steps and outcome. Belongs to a workflow run when governed.
- **Check result**: one command forge ran on a commit, with outcome and evidence.
- **Review verdict**: the reviewer's decision and findings for one commit.
- **Red-team record**: the adversary's verdict and evidence for one commit.
- **Approval request**: a human gate (`spec`, `plan`, `pr`) tied to a workflow run.
- **Attestation**: a signed statement about one commit of one governed run.
- **Held-out scenario**: an executable acceptance scenario attached to a task and hidden from the builder.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On the durable engine, 100 percent of pull requests forge creates are linked to a workflow run and a `pr` approval request.
- **SC-002**: 100 percent of governed pull requests carry an attestation that verifies offline after a service restart.
- **SC-003**: In a test corpus of changes whose agent-reported checks are green and whose real checks fail, forge creates zero pull requests.
- **SC-004**: Zero records in a governed run show `survived` or `approve` without a model run on a different family behind them.
- **SC-005**: Across the test suite for this feature, zero occurrences of held-out scenario text in captured prompts, tool outputs and events.
- **SC-006**: Retrying any activity of a governed run ten times produces exactly one agent run, one pull request and one approval request per gate.
- **SC-007**: A maintainer can answer "what verified this change" for any governed pull request from the run page or the attestation alone.

## Assumptions

- The durable engine (Temporal) is the governed engine. The Postgres state machine stays as a tracker driven by callers and does not run agents.
- Forge does not integrate changes into the base branch in this feature. A person does that in the code host after the `pr` gate. Automatic integration is roadmap milestone M4.
- GitHub is the only code host in scope. Credentials come from the worker environment as today; per-workspace credentials are R16.
- The sandbox default tier is unchanged here; containment is milestone M3.
- Reviewer and adversary models are resolved through the existing per-role model configuration.
- Figures in the research notes are leads, not facts, and no requirement depends on one.
