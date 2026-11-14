# Feature Specification: Risk and merge

**Feature Branch**: `004-risk-classification`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M4 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`, issues R20 to R22): "Classify a change into a risk class. Apply controls proportionate to the class. Integrate low-risk changes automatically through a queue, and remove the mock."

**Builds on**: lettered spec A, the risk classification spine (`docs/superpowers/specs/2026-09-19-risk-classification-spine-design.md`), which is approved and is the binding design for classification and gate integration. Also ADR-0005 (machines propose, people decide), ADR-0007 (production paths fail closed), ADR-0009 (migration numbers are assigned when a change lands), `specs/003-containment/spec.md` (the impact gate) and the M1 service-side checks (R05).

## Why this exists

Forge treats every change alike. The deploy gate keys off the target environment, never off how
dangerous the change is, so a documentation fix and a change to an authentication path take the
same route at the same speed. There is no risk class anywhere in the code, and the automation
action that integrates a change automatically is a documented mock that records what it would have
done. Without a risk class there is no principled way to let a small safe change through quickly
while holding a dangerous one for more scrutiny.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Every change gets a derived risk class (Priority: P1)

A repository owner writes risk rules in the repository policy. For any change, forge derives a risk
class from the diff: which paths it touches, whether it contains a migration or a contract change,
how large it is. A change may declare a higher class. It may declare a lower one only with a named
approver and a reason.

**Why this priority**: every later control reads this class. It is pure computation with no effect
on a running system, so it can ship first and be checked in isolation.

**Independent Test**: give the classifier a set of diffs and a rule set, and compare the classes
with the expected ones. No database, network or gate is needed.

**Acceptance Scenarios**:

1. **Given** rules that mark a path as critical, **When** a change touches that path and others, **Then** the derived class is critical.
2. **Given** a change that matches no rule, **When** it is classified, **Then** it takes the policy default.
3. **Given** a diff that cannot be read, **When** it is classified, **Then** it takes the fallback class, which defaults to critical, and the reason is recorded.
4. **Given** a declared class below the derived class and no override, **When** the change is evaluated, **Then** it is refused with an error naming both classes.
5. **Given** a declared class below the derived class with an override naming an approver and a reason, **When** the change is evaluated, **Then** the lower class applies and the approver and reason are part of the result.
6. **Given** a policy with no risk rules, **When** any change is processed, **Then** nothing is classified and behaviour is identical to today.

---

### User Story 2 - Controls scale with the class, and only ever tighten (Priority: P1)

An operator turns on risk rules in shadow mode. Every classification and every gate evaluation is
recorded, with what the gate decided and what risk would have decided, and no gate outcome changes.
When the agreement rate satisfies the operator, they switch a repository to enforcing. From then on
the class can add approvals, require an adversarial review or force a gated deploy. It can never
make a gate more permissive than it was.

**Why this priority**: this is the point of classification. Shadow mode first means it can be
turned on without endangering a working system.

**Independent Test**: run the existing gate test matrix with risk rules in shadow mode and assert
no decision changes; then in enforcing mode assert every changed decision is stricter.

**Acceptance Scenarios**:

1. **Given** shadow mode, **When** any shipping gate evaluates a classified change, **Then** its decision is unchanged and an observation records both decisions and whether they agree.
2. **Given** enforcing mode and a high-risk change, **When** the approval gate evaluates it, **Then** it requires at least the approvals the class demands.
3. **Given** enforcing mode and any combination of existing decision and class requirement, **When** a gate evaluates, **Then** the outcome is never more permissive than the existing decision.
4. **Given** shadow mode, **When** the risk subsystem itself fails, **Then** the gate decision is unaffected and the failure is logged.
5. **Given** recorded observations, **When** the operator asks for the agreement rate per gate over a period, **Then** forge returns a rate computed from those observations.
6. **Given** a classification or observation record, **When** anything attempts to change or delete it, **Then** the attempt is rejected.

---

### User Story 3 - Low-risk changes integrate automatically, one at a time (Priority: P2)

A repository owner opts in to automatic integration. A change whose final class is at or below the
policy's automatic threshold, with all required checks green, the independent review passed and an
attestation present, enters a queue. Forge takes one queued change at a time per target branch,
confirms it is still green against the current head of that branch, and integrates it. Anything
above the threshold, or anything that fails a precondition, waits for a person.

**Why this priority**: it is the visible payoff, and it is the action that most needs the two
stories before it. It replaces a mock, so today nothing depends on it working.

**Independent Test**: with a fake repository host, queue two low-risk changes and one high-risk
change. Observe that the low-risk changes integrate in order, each after a fresh check against the
branch head, and that the high-risk change is never integrated automatically.

**Acceptance Scenarios**:

1. **Given** automatic integration is off (the default), **When** any change becomes eligible, **Then** nothing is integrated automatically.
2. **Given** it is on and a change's final class is within the threshold with every precondition met, **When** it reaches the head of the queue, **Then** forge integrates it and records the result with the change's classification.
3. **Given** a change above the threshold, **When** it becomes otherwise eligible, **Then** it is not queued and waits for a person.
4. **Given** two queued changes for the same branch, **When** the first integrates, **Then** the second is re-verified against the new head before it integrates.
5. **Given** a queued change whose checks turn red or whose branch conflicts with the head, **When** it reaches the head of the queue, **Then** it leaves the queue with a recorded reason and the next change proceeds.
6. **Given** risk rules are absent, in shadow mode, or the classification used the fallback class, **When** a change becomes eligible, **Then** it is not integrated automatically.
7. **Given** the repository host refuses the integration, **When** forge attempts it, **Then** the action reports an error and never reports success.

---

### Edge Cases

- A rule names a class that does not exist: the policy is rejected when it is loaded, not when a change is evaluated.
- Two rules match the same change with different classes: the higher class wins.
- A change spans several repositories: the class is the highest across them.
- The classifier is given empty or malformed input: it returns a result and never raises.
- An agent declares its own change as lower risk: refused without a human override (ADR-0005).
- Enforcement is switched from enforcing back to shadow: later evaluations record the mode in force at the time; earlier records are unchanged.
- The worker restarts while a change is at the head of the queue: on restart the queue resumes without integrating the same change twice and without losing the entries behind it.
- A person integrates a queued change by hand: the queue notices and drops the entry.
- The impact gate from `specs/003-containment/spec.md` classifies automatic integration into a protected branch as irreversible: [NEEDS CLARIFICATION: does opting in to automatic integration for low-risk changes count as the standing human decision the impact gate needs, or is each one still held?].

## Requirements *(mandatory)*

### Functional Requirements

**Classification (R20)**

- **FR-001**: The system MUST derive a risk class for a change from facts about its diff, using rules in the repository policy, with the ordered classes minimal, low, high and critical.
- **FR-002**: The derived class MUST be the highest class of any matching rule, or the policy default when none matches.
- **FR-003**: The system MUST apply the fallback class, critical unless configured otherwise, when the diff facts are unavailable, and MUST record that it did.
- **FR-004**: A declared class above the derived class MUST be accepted. A declared class below it MUST be refused unless an override names an approver and a reason.
- **FR-005**: Classification MUST be deterministic and MUST never raise during evaluation. Invalid rules MUST be rejected when the policy is loaded.
- **FR-006**: Path matching in risk rules MUST behave identically to path matching in existing policy rules.
- **FR-007**: A policy without risk rules MUST produce no classification and no change in behaviour.
- **FR-008**: Classification MUST NOT take the author, team, or whether the author is an agent as an input.

**Proportionate controls (R21)**

- **FR-009**: The system MUST record every classification and every gate observation in append-only records that reject change and deletion.
- **FR-010**: Each shipping gate (deploy, multi-repository integration, approval, adversarial review, self-evaluation) MUST accept an optional risk input and MUST behave exactly as today when it is absent.
- **FR-011**: In shadow mode a gate MUST return its own decision unchanged and record what risk would have decided.
- **FR-012**: In enforcing mode a gate MUST combine risk requirements with its own decision so that the result is never more permissive than its own decision.
- **FR-013**: A failure of the risk subsystem in shadow mode MUST NOT affect a gate decision.
- **FR-014**: The system MUST expose recorded classifications, a per-gate agreement rate over a period, and a dry-run classification that records nothing.
- **FR-015**: Shadow mode MUST be the default enforcement mode.

**Automatic integration (R22)**

- **FR-016**: The system MUST replace the mock integration dispatcher with one that performs the real integration on the repository host.
- **FR-017**: Automatic integration MUST be off by default and MUST require both the automation rule's opt-in and the policy's allowance, as today.
- **FR-018**: The system MUST integrate a change automatically only when its final class is at or below the policy's automatic threshold, risk rules are enforcing, the classification did not use the fallback, required checks are green on the change's current head, the independent review passed and an attestation exists. The default threshold is [NEEDS CLARIFICATION: `minimal` only, or `minimal` and `low`? Spec A's example gives `low` one required approval, which suggests `minimal` only].
- **FR-019**: The system MUST integrate at most one change at a time per repository and target branch, in queue order, and MUST re-verify each change against the branch's current head before integrating it. Whether the queue is forge's own or the repository host's native queue is [NEEDS CLARIFICATION: a forge-side serial queue is assumed, because the host's native queue is not available on every plan and R13 already introduces a serialised step].
- **FR-020**: A change that fails a precondition at the head of the queue MUST leave the queue with a recorded reason and MUST NOT block the changes behind it.
- **FR-021**: Each automatic integration and each refusal MUST be recorded in the audit chain with the classification it relied on.
- **FR-022**: A failed or refused integration MUST be reported as an error, never as success.

### Key Entities

- **Risk class**: one of minimal, low, high, critical, ordered.
- **Risk rules**: the policy block: enforcement mode, default, fallback, path rules, and the requirements of each class.
- **Change signals**: facts read from a diff: paths, migration present, contract touched, lines added and removed, repository count.
- **Risk evaluation**: derived class, declared class, final class, matched rules, override approver and reason, whether the fallback applied.
- **Gate requirements**: what a class demands of each gate: approvals, deploy mode, adversarial review, self-evaluation.
- **Risk classification record**: one append-only record per classified change, with change identity and the enforcement mode in force.
- **Risk gate observation**: one append-only record per gate evaluation: actual decision, risk decision, agreement.
- **Integration queue entry**: a change waiting for automatic integration: repository, target branch, change, position, state and reason.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With no risk rules, the full existing gate test suite passes unchanged.
- **SC-002**: In shadow mode, 100 percent of gate evaluations of classified changes produce an observation and zero gate decisions differ from the unclassified run.
- **SC-003**: Across every combination of existing decision and class requirement, zero enforcing outcomes are more permissive than the shadow outcome.
- **SC-004**: An unreadable diff is classified critical in 100 percent of cases.
- **SC-005**: Zero changes above the automatic threshold are integrated without a human decision.
- **SC-006**: In a test with two queued changes on one branch, each is verified against the head it actually integrates onto.
- **SC-007**: No code path reports a successful automatic integration without a call to the repository host that succeeded.

## Assumptions

- Spec A is binding for classification, persistence and gate integration. Where it names migration number 0041, the next free number applies instead (ADR-0009); 0041 to 0044 are taken.
- Spec A's Risk panel in the web settings is not part of this milestone. The API it would read is.
- No default rules ship for existing repositories. Rules are written per repository and opt-in (spec A section 11).
- Canary, rings and feature flags are milestone M10. The deploy requirement a class produces is recorded here and consumed there.
- The deploy dispatch mock stays a mock in this milestone. Only the integration dispatcher becomes real.
- The independent review and attestation preconditions come from milestone M1 (R05, R06, R08). Where one is not yet available in a deployment, automatic integration stays off.
- Adding `packages/risk-core` adds a workspace member. The issue that does so is explicitly authorised to change the workspace tables and lockfile.
