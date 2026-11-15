# Feature Specification: Containment

**Feature Branch**: `003-containment`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M3 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`, issues R14 to R19): "Make a container sandbox the default tier. Test sandbox escape and publish isolation levels. Issue short-lived, per-run scoped credentials from per-workspace GitHub App credentials. Gate agent diffs on supply-chain checks. Close the three critical pentest items. Hold irreversible automatic actions for a person, and record a rationale on approvals."

**Builds on**: ADR-0005 (machines propose, people decide), ADR-0007 (production paths fail closed), ADR-0008 (agents act through a policy-gated tool belt with scoped credentials), ADR-0014 (untrusted content cannot drive privileged tools), ADR-0003 and ADR-0010 (absorbed ideas are rebuilt natively, nothing is copied), `docs/architecture/unified-factory.md`, `docs/security/threat-model.md` (surface S6), `docs/security/pentest-punch-list.md` (PT-01, PT-02, PT-03, PT-13), `docs/self-hosting/security.md` (sandbox tiers), lettered spec G (`docs/superpowers/specs/2026-11-07-agents-that-act-design.md`) and lettered spec K section 4.7 (`docs/superpowers/specs/2026-11-07-context-memory-and-lines-design.md`).

## Why this exists

Forge runs code that an agent wrote, inside repositories it does not control. Today the default
sandbox tier (`worktree`) runs those commands as host subprocesses of the worker, the stronger
tiers are opt-in, the sandbox items on the pentest punch list have no adversarial tests, GitHub
App credentials are one process-wide set read from the worker environment, nothing scans an agent
diff for secrets or unsafe dependencies before it is offered for review, and an automation rule
can trigger an outbound action with no person in the loop. A factory that ships agent changes has
to show that a bad change, or a bad agent, stays contained.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Agent commands run inside a container by default (Priority: P1)

An operator installs forge with the shipped defaults. Every command an agent runs executes inside
a locked-down container, not as a process of the worker. The host-process tier is still available
for local development, but only when the operator asks for it by name and the deployment is a
development environment.

**Why this priority**: every other containment control assumes agent code cannot reach the worker's
filesystem, environment or network. Without this the rest is advisory.

**Independent Test**: start a worker with no sandbox setting in a production-like environment and
observe that a run's commands execute in a container; set the host-process tier in the same
environment and observe that the worker refuses to start.

**Acceptance Scenarios**:

1. **Given** a deployment with no sandbox tier configured, **When** an agent run starts, **Then** its commands execute in the container tier.
2. **Given** a non-development environment, **When** the operator configures the host-process tier, **Then** the worker refuses to start and names the setting and the reason.
3. **Given** a development environment, **When** the operator explicitly configures the host-process tier, **Then** runs use it and each run records that it ran without an isolation boundary.
4. **Given** the container tier is selected and the container runtime is unreachable, **When** a run starts, **Then** the run fails with a clear error and never falls back to the host-process tier.

---

### User Story 2 - Isolation is tested adversarially and published (Priority: P1)

A security reviewer wants to know what each sandbox tier does and does not stop. Forge ships a test
suite that attempts to leave the sandbox and to exhaust its resources, covering punch-list items
PT-02 and PT-03, and a published table that states, per tier, which attempts are stopped and which
are not.

**Why this priority**: an isolation claim nobody tried to break is not evidence. The open standard
planned in a later milestone cites these levels.

**Independent Test**: run the suite. Each tier reports, per attempt class, "contained",
"not contained" or "not run on this host", and the published table matches the suite's result.

**Acceptance Scenarios**:

1. **Given** the container tier, **When** the suite attempts each class of filesystem, privilege and network breakout listed for PT-02, **Then** every attempt is contained and the run stays usable or is terminated cleanly.
2. **Given** any tier with resource limits, **When** the suite attempts each class of resource exhaustion listed for PT-03, **Then** the limit holds, the command ends within its time limit, and cleanup leaves no orphaned sandbox.
3. **Given** the agent tool belt, **When** a tool argument names a location outside the run's repository root by any indirect means, **Then** the tool refuses and nothing outside the root is read or written.
4. **Given** a host without a runtime for a tier, **When** the suite runs, **Then** that tier is reported as not run, never as passed.

---

### User Story 3 - Credentials are per workspace, per run and short-lived (Priority: P1)

A workspace administrator connects their own GitHub App installation. Each run receives a
credential that reaches only that run's repository, carries only the permissions the step needs,
and expires soon after. One workspace's runs can never use another workspace's installation.

**Why this priority**: a process-wide credential means one workspace's run can act with another
workspace's access. That blocks any multi-tenant use.

**Independent Test**: configure two workspaces with different installations, start a run in each,
and inspect the credential requests: each names only its own installation, one repository and the
step's permissions.

**Acceptance Scenarios**:

1. **Given** two workspaces with different GitHub App installations, **When** each starts a run, **Then** each run's credential is issued from its own workspace's installation.
2. **Given** a workspace with no GitHub App credentials, **When** a run reaches the delivery step in a non-development environment, **Then** the outcome is "not configured" and no process-wide credential is used.
3. **Given** a run, **When** it reads the repository and when it delivers a change, **Then** the read credential cannot write and the delivery credential reaches only that repository.
4. **Given** any tier, **When** an agent command inspects its environment, **Then** no GitHub App secret and no issued token is present.

---

### User Story 4 - Agent diffs pass supply-chain gates (Priority: P2)

A reviewer receives an agent change. Before it is offered for review, forge has scanned the diff
for secrets, checked that every newly added dependency exists in its registry and is older than a
configured minimum age, and run static analysis on the changed files. A failing gate blocks the
change and tells the agent and the reviewer why.

**Why this priority**: agents invent package names and copy credentials. These are cheap,
deterministic checks that catch the common cases. It depends on the service-side check step from
milestone M1 (R05).

**Independent Test**: submit three agent diffs, one adding a secret-shaped string, one adding a
dependency that does not exist, one adding code that violates a static-analysis rule. Each is
blocked with a finding that names the file and the rule.

**Acceptance Scenarios**:

1. **Given** an agent diff that adds a credential-shaped value, **When** the gates run, **Then** the change is blocked and the finding names the file and line without echoing the value.
2. **Given** an agent diff that adds a dependency unknown to its registry, **When** the gates run, **Then** the change is blocked.
3. **Given** an agent diff that adds a dependency published more recently than the minimum age, **When** the gates run, **Then** the change is blocked unless the repository policy allows that package by name.
4. **Given** the registry cannot be reached, **When** the gates run, **Then** the dependency gate reports "could not verify" and blocks. It never passes by default.

---

### User Story 5 - The three critical punch-list items have regression evidence (Priority: P2)

The maintainer needs the three items pre-assessed as Critical (PT-01, PT-02, PT-13) to be closed as
far as automation can close them: an exhaustive regression suite per item, every defect that suite
finds fixed, and the punch list updated to say what is now covered and what still needs a human
tester.

**Why this priority**: the release gate R26 depends on it. A human penetration test remains a
separate, owner-run activity.

**Independent Test**: run the three suites. Each enumerates its surface from the running
application rather than from a hand-written list, and fails if a new route or resource type is
added without coverage.

**Acceptance Scenarios**:

1. **Given** every authenticated route, **When** it is called by each role, **Then** a role without the required permission is refused and no route is missing from the role matrix (PT-01).
2. **Given** every workspace-scoped resource type exposed by the API, **When** a valid principal of another workspace requests it by identifier, **Then** the response is the same as for a resource that does not exist (PT-13).
3. **Given** the PT-02 suite from User Story 2, **When** it passes on the default tier, **Then** the punch list records PT-02's automated coverage and its residual.

---

### User Story 6 - Irreversible automatic actions wait for a person, and approvals can carry a reason (Priority: P2)

An automation rule or an agent is about to take an action whose effects leave forge and cannot be
undone. Forge holds the action and asks a person, however confident the system is and whatever the
policy allows. When the person decides, they may state why. A reason is recorded when given and
is never required. Later, when a similar action is held, forge shows the earlier decisions with
any reasons given, and an agent's proposal can cite them.

**Why this priority**: confidence is the wrong test for an action that cannot be taken back. A
recorded reason turns one decision into guidance for the next. This is a native rebuild of two
ideas noted in the world-model research (ADR-0003, ADR-0010); nothing is copied.

**Independent Test**: enable an automation rule that triggers an irreversible external action.
Observe that it is held as an approval request, that a decision without a reason is accepted, that
a reason is recorded when one is given, and that the next held action of the same kind shows the
earlier decision with its reason.

**Acceptance Scenarios**:

1. **Given** an automatic action classified as irreversible and external, **When** it is about to run, **Then** it is held, an approval request is created, and the action runs only after a person approves it.
2. **Given** a held action, **When** a person decides without stating a reason, **Then** the decision is accepted and recorded with no reason. When they state one, it is recorded.
3. **Given** earlier human decisions on the same kind of action in the same workspace, **When** a new action is held, **Then** the request shows those decisions with decider and date, and with the reason where one was given.
4. **Given** an earlier approval of the same kind, **When** a new irreversible action is held, **Then** it is still held. Precedent informs the person and never approves on their behalf.
5. **Given** an action classified as reversible or internal, **When** it runs, **Then** the impact gate does not hold it and existing policy gates apply unchanged.

---

### Edge Cases

- The operator sets a tier name that does not exist: the worker refuses to start.
- A repository policy asks for a weaker tier than the workspace minimum: the stronger one applies (existing behaviour, must not regress).
- A run's credential expires mid-run: the next step obtains a new one. A cached token is never reused across runs or workspaces.
- A workspace's GitHub App credentials are rotated or revoked during a run: delivery fails with a recorded reason and the run is not reported as delivered.
- A diff adds a dependency to a manifest format the gate does not understand: the gate reports "unsupported manifest" and blocks rather than passing.
- A secret scan matches a deliberate test fixture: the repository policy can allow a named path, never a whole tree, and the allowance is recorded.
- An unknown or new automatic action type reaches the impact gate: it is treated as irreversible until classified.
- The person who would approve a held action is the one who configured the automation rule: existing self-approval rules apply.
- A held action is never decided: it expires under the existing approval SLA and does not run.

## Requirements *(mandatory)*

### Functional Requirements

**Sandbox default (R14)**

- **FR-001**: The system MUST run agent commands in the container tier when no sandbox tier is configured.
- **FR-002**: The system MUST refuse to start a worker that is configured for the host-process tier outside a development environment, and MUST accept it in a development environment only when it is set explicitly.
- **FR-003**: The system MUST never fall back to a weaker tier when the selected tier's runtime is unavailable.
- **FR-004**: Every run MUST record the isolation tier it actually executed under.
- **FR-005**: Shipped deployment defaults and operator documentation MUST state the container tier as the default and the host-process tier as development-only. The Helm chart default is [NEEDS CLARIFICATION: the chart renders sandbox settings but ships no container runtime endpoint for the worker. Does it default to `container` with a documented prerequisite, or to `gvisor` through a RuntimeClass?].

**Escape and exhaustion tests (R15)**

- **FR-006**: The system MUST ship a test suite that attempts to break filesystem, privilege and network confinement for each sandbox tier, mapped to PT-02.
- **FR-007**: The system MUST ship tests that attempt process, memory, disk and time exhaustion inside a run, mapped to PT-03, and that verify cleanup after each.
- **FR-008**: The tool belt MUST refuse any tool argument that resolves outside the run's repository root, including through links.
- **FR-009**: A tier whose runtime is absent on the test host MUST be reported as not run.
- **FR-010**: The project MUST publish, per tier, the isolation level and the attempt classes it contains, in a document generated from or checked against the suite.

**Credentials (R16)**

- **FR-011**: GitHub App credentials MUST be stored per workspace, encrypted at rest, and resolved by the run's workspace.
- **FR-012**: Each issued credential MUST be limited to one repository, to the permissions of the step that uses it, and to a short lifetime.
- **FR-013**: Outside a development environment the system MUST NOT use process-wide GitHub App credentials for a run.
- **FR-014**: No GitHub App secret and no issued token MUST be visible to agent commands, in logs, in run events or in error messages.
- **FR-015**: Every credential issue MUST be recorded with workspace, run, repository, permissions and expiry, without the token.

**Supply-chain gates (R17)**

- **FR-016**: The system MUST scan each agent diff for secrets before the change is offered for review, and MUST block on a finding.
- **FR-017**: The system MUST verify that each dependency a diff adds exists in its registry, and MUST block one that does not.
- **FR-018**: The system MUST block a newly added dependency version younger than a configured minimum age, unless the repository policy allows that package by name. The default minimum age is [NEEDS CLARIFICATION: no default has been chosen. Seven days is the working assumption].
- **FR-019**: The system MUST run static analysis on the changed files and block on findings at or above a configured severity.
- **FR-020**: A gate that cannot complete MUST block and say why. Findings MUST name the file, line and rule and MUST NOT echo a secret value.
- **FR-021**: Gate results MUST be recorded with the run so they can be attested later.

**Critical punch-list items (R18)**

- **FR-022**: The system MUST have a regression suite that derives the route list from the application and asserts, for every role, that a role without the route's permission is refused (PT-01).
- **FR-023**: The system MUST have a regression suite that derives the workspace-scoped resource types from the application and asserts that a principal of another workspace cannot read, change or delete any of them, and cannot tell that they exist (PT-13).
- **FR-024**: A new route or resource type without a declared expectation MUST fail these suites.
- **FR-025**: Every defect these suites reveal MUST be fixed with the safe behaviour, and the punch list MUST record automated coverage and the residual for PT-01, PT-02 and PT-13.

**Impact gate and rationale (R19)**

- **FR-026**: The system MUST classify every automatic action type as reversible or irreversible and as internal or external, and MUST treat an unclassified type as irreversible and external.
- **FR-027**: The system MUST hold an irreversible external action as an approval request and run it only after a person approves, regardless of confidence, policy allowance or precedent.
- **FR-028**: A reason on an approval decision is always optional. The system MUST record a reason when one is given and MUST NOT require one on any decision, including a decision on a held action and a rejection. Decided 2026-11-15.
- **FR-029**: The system MUST return, for a given action kind and workspace, the earlier human decisions with decider and time, and with the reason where one was given, to people in the approval view and to agents through forge's own tool surface.
- **FR-030**: Only decisions made by a person count as precedent. Precedent MUST NOT approve an action.
- **FR-031**: A held action and its decision MUST be recorded in the audit chain.

### Key Entities

- **Sandbox tier**: one of the ordered isolation levels (host process, container, userspace kernel, microVM), with the isolation class it provides.
- **Isolation level record**: per tier, the attempt classes contained, not contained or not tested.
- **Workspace GitHub App credential**: a workspace's App identity, installation and private key, stored encrypted.
- **Run credential issue**: an audit record of one short-lived token: workspace, run, repository, permissions, expiry.
- **Supply-chain finding**: gate, file, line, rule, severity, and for dependencies the package, version and reason.
- **Action impact class**: the reversibility and externality assigned to an automatic action type.
- **Impact hold**: an approval request created for one irreversible action, with the action's parameters.
- **Decision rationale**: the reason a person gave for a decision, when they gave one, quotable as precedent for later holds.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With shipped defaults, 100 percent of agent runs record a tier of container or stronger.
- **SC-002**: The escape suite covers every attempt class listed for PT-02 and PT-03, and the default tier contains all of them on a host with a container runtime.
- **SC-003**: In a two-workspace test, zero credential requests name the other workspace's installation.
- **SC-004**: Every issued credential in the test suite names exactly one repository and expires within one hour.
- **SC-005**: Seeded diffs containing a secret, a nonexistent dependency, a too-new dependency and a static-analysis violation are each blocked, with zero secret values echoed.
- **SC-006**: The PT-01 and PT-13 suites cover 100 percent of routes and workspace-scoped resource types, and fail when an uncovered one is added.
- **SC-007**: Zero irreversible external actions run without a human approval. A decision is never refused for lacking a reason.
- **SC-008**: A second hold of the same action kind shows the first decision, with its reason when one was given.

## Assumptions

- "Development environment" means the environment names forge already treats as development (`FORGE_ENVIRONMENT` unset or one of the names in `apps/api/forge_api/auth/service.py`).
- The container tier's existing hardening (dropped capabilities, no network by default, non-root user, read-only root, resource limits) is the baseline. This feature tests it and makes it the default; it does not redesign it.
- Tests that need a real container, gVisor or Firecracker runtime stay in the existing opt-in lanes. The default test lane proves the hermetic part and reports the rest as skipped.
- A human penetration test is out of scope. R18 closes the items as far as automated regression evidence can.
- The supply-chain gates attach to the service-side check step delivered by R05. Scanners already used by `make security` are reused.
- The deploy dispatch mock is not made real here. The impact gate holds it whether it is a mock or real.
- Ideas taken from the world-model research are rebuilt from the description of their behaviour. No code, prompt text or documentation is copied (ADR-0010).
