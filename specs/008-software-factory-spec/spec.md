# Feature Specification: The open Software Factory Specification

**Feature Branch**: `008-software-factory-spec`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M8 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`), issues R39 to R41: "Publish the 'issue in, attested change out' contract as an open specification with levels and an evidence format, a conformance suite that forge passes in CI, and a name, licence and governance decision."

**Builds on**: `docs/trust-layer.md` (attested changesets, audit chain), `packages/contracts/forge_contracts/attestation.py`, ADR-0002 (forge is the system of record), ADR-0005 (machines propose, people decide), ADR-0007 (production paths fail closed), ADR-0010 (licence and clean-room policy), ADR-0016 (interoperability standards are pinned), the programme documents `docs/superpowers/specs/2026-09-19-software-factory-programme.md` and `2026-11-07-unified-factory-programme.md`, and specs 001 (governed pipeline), 003 (containment), 005 (proof) and 007 (standards conformance).

## Why

The roadmap's thesis is that forge wins on verifiable delivery. A claim of verifiable delivery is only useful if someone outside forge can check it, and only becomes a standard if someone else can implement it. The projects the roadmap takes as its model each published a narrow contract and a conformance suite, with a working implementation behind them.

The research notes report that no ratified software-factory standard exists, that the published maturity ladders define levels in prose with no test, and that other projects already use the name "forge". These are reported, not confirmed. A web search on 2026-11-14 listed several public repositories named forge that describe themselves as software factories; none was opened. This milestone does not depend on the reports being exact: it publishes a small specification, makes it testable, and puts the naming question to the owner.

The specification is narrower than forge. It says what a factory must output and prove. It does not say how a factory is built, which agent it uses, or which tracker it reads.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An implementer learns what a conforming factory must produce (Priority: P1)

An engineer who builds a different factory reads one versioned document and learns the contract: what counts as the work item going in, what counts as the change coming out, which evidence must accompany the change, and what each conformance level requires. They can produce a conforming evidence bundle without reading forge's code.

**Why this priority**: Everything else in this milestone tests or governs this document.

**Independent Test**: A reviewer who has not worked on forge reads the specification and the example bundles and writes down, for each level, the evidence required. Their list matches the specification's requirement index.

**Acceptance Scenarios**:

1. **Given** the specification, **When** a reader looks up a level, **Then** they find a numbered list of requirements, each with an identifier and a normative keyword.
2. **Given** the evidence format, **When** a reader validates the published example bundles against the published schemas, **Then** the valid examples pass and the invalid examples fail for the stated reason.
3. **Given** the specification text, **When** a reader searches it for a requirement that only forge could meet, **Then** they find none: no requirement names a forge component, endpoint or table.

---

### User Story 2 - Anyone checks an evidence bundle against a level (Priority: P1)

A buyer, an auditor or another implementer runs one command on an evidence bundle and gets a report: the highest level the bundle meets and, for each requirement, pass, fail or not tested. The command needs no network access and no forge installation.

**Why this priority**: A level that cannot be tested is another prose ladder. The suite is what makes the specification different from what is reported to exist.

**Independent Test**: Run the suite on the published valid example and on each tampered example; the first passes at its stated level and each of the others fails on the requirement it was built to break.

**Acceptance Scenarios**:

1. **Given** a valid bundle that claims a level, **When** the suite runs, **Then** it exits with success and reports that level.
2. **Given** a bundle whose signed statement was altered after signing, **When** the suite runs, **Then** it fails on the signature requirement.
3. **Given** a bundle that claims a level but lacks an evidence item that level requires, **When** the suite runs, **Then** it fails and names the missing requirement.
4. **Given** a bundle in which the reviewer identity equals the builder identity, **When** the suite checks the level that requires independent review, **Then** it fails.
5. **Given** a requirement in the specification's index, **When** the suite's coverage is listed, **Then** the requirement is either covered by a check or listed as not machine-checkable with a reason.

---

### User Story 3 - Forge proves its own claim on every change (Priority: P2)

A forge maintainer sees a failing check when a change would make forge's exported evidence stop conforming, or when forge's published claim is higher than what the suite measures.

**Why this priority**: Forge is the reference implementation. A reference implementation that does not pass its own suite, or that claims more than it passes, discredits the specification.

**Independent Test**: In the test suite, export an evidence bundle from a governed run produced by forge's real services, run the conformance suite on it, and compare the result with forge's published claim.

**Acceptance Scenarios**:

1. **Given** a completed governed run, **When** the evidence bundle is exported, **Then** the conformance suite passes it at the level forge claims.
2. **Given** forge's published claim, **When** it names a level above what the suite reports for the exported bundle, **Then** the test suite fails.
3. **Given** a run that is missing evidence a level requires, **When** the bundle is exported, **Then** the export does not invent the evidence and the suite reports the lower level.

---

### User Story 4 - The owner settles name, licence and governance (Priority: P3)

The project owner reads one decision record that lays out the options and the evidence for the name of the project and of the specification, the licence of the specification text and schemas, how changes to the specification are decided, and a draft proposal to a foundation's entry stage. The owner records a decision.

**Why this priority**: It decides how the specification is offered to others, but it changes no behaviour and needs a person.

**Independent Test**: The decision record exists, names the chosen option for each question, and the repository's governance files reflect it.

**Acceptance Scenarios**:

1. **Given** the decision record, **When** a contributor reads it, **Then** they know the name, the licence of the specification, who decides changes to it and how to propose one.
2. **Given** the draft foundation proposal, **When** it is compared with the foundation's published entry criteria at the time of writing, **Then** each criterion is answered or marked as not yet met.

---

### Edge Cases

- A change is produced with no human in the loop. The evidence must say so; it must not name an approver.
- A change is produced by several agents and several models.
- A work item is edited after the run started. The evidence binds the revision the run read.
- A bundle is signed with a key the verifier does not hold. The suite reports "cannot verify", which is distinct from "tampered".
- A bundle claims a specification version the suite does not know.
- A bundle contains an evidence item whose digest does not match the manifest.
- A factory meets every requirement of a higher level but misses one of a lower level. Levels are cumulative, so it conforms at the level below the first miss.
- Evidence contains secrets or private source. The format carries digests and references so a bundle can be shared without the material itself.
- Two implementations use the same field differently. The schema, not the prose, decides.
- The name of the specification changes after publication of v0.1.

## Requirements *(mandatory)*

### Functional Requirements

**The specification (R39)**

- **FR-001**: The project MUST publish a versioned specification document, version 0.1, under `docs/`, that defines the contract "work item in, attested change out": the inputs a factory accepts, the change it produces, and the evidence that must accompany the change.
- **FR-002**: The specification MUST use normative keywords, and every normative requirement MUST have a stable identifier.
- **FR-003**: The specification MUST define conformance levels that are cumulative, and for each level the requirements a factory must meet. The default is four levels: traceable (the change links to its work item and run record), attested (the change carries a signed statement of what produced it), verified (checks and review are independent of the agent that made the change), contained (the run's isolation level, credentials scope and human decisions are declared and evidenced). The number and names of the levels are [NEEDS CLARIFICATION: owner to confirm the four-level default before R39 is written; published ladders use three to seven levels].
- **FR-004**: The specification MUST define an evidence format: a bundle with a manifest that names the specification version, the claimed level, the factory, the work item, the change, and each evidence item with its type and digest.
- **FR-005**: The evidence format MUST be defined by machine-readable schemas published with the specification, and the schemas MUST be the authority when prose and schema disagree.
- **FR-006**: The specification MUST publish example bundles: at least one valid example per level and invalid examples that each break one named requirement.
- **FR-007**: The specification MUST reuse existing standards where one exists and MUST NOT define a new wire protocol or signature format. Signed statements are in-toto statements in DSSE envelopes.
- **FR-008**: The specification MUST be implementation-neutral: no requirement may name a forge component, and the document MUST state what it does not cover.
- **FR-009**: The specification MUST state how a factory makes a conformance claim, and how the specification itself is versioned and changed.
- **FR-010**: The specification MUST define how metrics are reported in evidence, using the definitions from spec 005, without setting thresholds. Whether a level requires metrics or whether they are informative is [NEEDS CLARIFICATION: requiring metrics at a level ties the specification to five definitions that have not been used outside forge].
- **FR-011**: The specification MUST define how isolation is declared in evidence, using the isolation levels published by spec 003.
- **FR-012**: The specification's requirement identifiers MUST be listed in one machine-readable index that matches the document.

**The conformance suite (R40a)**

- **FR-013**: The project MUST publish a conformance suite that takes an evidence bundle and a level and reports, per requirement identifier, pass, fail, or not tested, and the highest level met.
- **FR-014**: The suite MUST run offline, MUST NOT import or call forge, and MUST use only the published schemas, the bundle and a verification key.
- **FR-015**: The suite MUST verify signatures and digests, and MUST fail a bundle that was altered, that lacks evidence its claimed level requires, or that claims a level it does not meet.
- **FR-016**: The suite MUST distinguish "does not conform" from "cannot be checked" in its report and exit status.
- **FR-017**: Every requirement identifier in the index MUST be covered by a check or listed as not machine-checkable with a reason.
- **FR-018**: The suite MUST refuse a bundle that names a specification version it does not implement.

**Forge as reference implementation (R40b)**

- **FR-019**: Forge MUST be able to export an evidence bundle for a completed run in the specification's format, from real records only. Missing evidence stays missing.
- **FR-020**: Forge's test suite MUST export a bundle from a governed run and pass it through the conformance suite on every change.
- **FR-021**: Forge MUST publish the level it claims, and a test MUST fail when the claim is higher than the level the suite reports.

**Name, licence, governance (R41)**

- **FR-022**: The owner MUST record a decision on the name of the project and of the specification, with the evidence considered.
- **FR-023**: The owner MUST record the licence of the specification text and of its schemas. The licence of forge's code is Apache-2.0 today (`LICENSE`); the licence of the specification is [NEEDS CLARIFICATION: owner decision in R41; one comparable baseline publishes prose under CC BY 4.0 and schemas under Apache-2.0].
- **FR-024**: The repository MUST contain a governance document that says who decides changes to the specification, how a change is proposed, and how a second implementer gains a say.
- **FR-025**: The project MUST prepare a draft proposal to a neutral foundation's entry stage that answers that foundation's published entry criteria as they stand when the draft is written. Submitting it is the owner's act.

### Key Entities

- **Work item**: the request a factory acts on: an identifier, a source system, and the revision the run read.
- **Change**: what the factory produced: repository, base revision, head revision.
- **Evidence bundle**: a manifest plus evidence items for one change. Portable and verifiable offline.
- **Evidence item**: one typed record in a bundle: a signed statement, check results, a review verdict, an isolation declaration, a decision record, a metrics snapshot. Each has a digest.
- **Conformance level**: a named, cumulative set of requirements.
- **Requirement**: one normative statement with an identifier, a level and a keyword.
- **Conformance claim**: a factory's statement of the specification version and level it meets, and the evidence for it.
- **Conformance report**: the suite's output for one bundle.
- **Decision record**: the owner's recorded choices on name, licence and governance.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A reader new to forge can list the evidence required at each level from the specification alone, in under 30 minutes.
- **SC-002**: 100 percent of normative requirements have an identifier, and the document and the index agree.
- **SC-003**: Every published valid example passes the suite at its stated level, and every invalid example fails on exactly the requirement it was built to break.
- **SC-004**: The suite checks a bundle in under 10 seconds on a laptop with no network access.
- **SC-005**: 100 percent of requirement identifiers are either covered by a suite check or listed as not machine-checkable with a reason.
- **SC-006**: Forge's exported bundle passes the suite at forge's claimed level on every change, and the claim is never above the measured level.
- **SC-007**: The specification text contains zero references to forge components.
- **SC-008**: The decision record answers each of the three questions (name, licence, governance) with one chosen option.

## Assumptions

- R08 (signed attestation on every agent change), R15 (published isolation levels) and R23 (metric definitions) are complete before R39 is written. The specification describes what forge does, not what it plans to do.
- Version 0.1 is a draft for comment. It makes no compatibility promise.
- The suite verifies bundles. It does not drive a live factory, score an agent, or measure quality. The end-to-end benchmark is spec 005.
- The implementation loop cannot add a workspace package or a third-party dependency, so the suite uses what the repository already has for schema validation and signature verification.
- A second independent implementation is the strongest evidence that the specification is implementable. Finding one is outside this milestone.
- A foundation's entry stage may require transferring trademarks and project assets and adopting the foundation's charter. That consequence is part of the owner's decision, not a detail of the proposal.
- Hosted offerings, pricing and community programmes are out of scope, as the roadmap states.
