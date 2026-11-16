# Feature Specification: Standards conformance

**Feature Branch**: `007-standards-conformance`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M7 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`), issues R34 to R38: "Swarm Code Factory speaks the published interoperability, telemetry, provenance and security standards at pinned versions, and can show it."

**Builds on**: lettered spec I (`docs/superpowers/specs/2026-11-07-forge-mcp-server-design.md`), `docs/trust-layer.md`, ADR-0006 (SCF serves MCP itself), ADR-0007 (production paths fail closed), ADR-0010 (licence and clean-room policy), ADR-0016 (interoperability standards are pinned, status Proposed).

## Why

SCF already speaks several standards, but at versions that have since moved, and nothing in the repository proves conformance. A factory that asks to be trusted has to be checkable by tools its users already own: an MCP client, an OpenTelemetry collector, an in-toto verifier, an A2A client, a security reviewer's checklist. This milestone brings each surface to a pinned, published version and adds a check for each.

## Standards and versions

Checked against the primary specification pages on 2026-11-14. Every version below must be checked again when the issue that depends on it is implemented, because ADR-0016 requires each upgrade to be an explicit change.

| Standard | Confirmed at the primary source | Not confirmed |
|---|---|---|
| Model Context Protocol | The revision marked Current is `2026-07-28`. It removes the `initialize` handshake and protocol sessions, makes `server/discover` mandatory for servers, requires the `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` request headers on Streamable HTTP, requires a `resultType` field on results, and requires `ttlMs` and `cacheScope` on `tools/list` results. The conformance suite is the npm package `@modelcontextprotocol/conformance`, run as `server --url <endpoint>` with `--spec-version` and an `--expected-failures` baseline | Which suite release is the first to cover `2026-07-28` in full. The authorization pages were not read |
| A2A | The latest released version is `1.0.0`. The Agent Card is served at `/.well-known/agent-card.json`. `supportedInterfaces` is required and required arrays must hold at least one element. Signing is optional (MAY), uses JWS (RFC 7515) over the card canonicalised with JCS (RFC 8785), in a `signatures` array whose entries have `protected` and `signature`. The protected header must carry `alg` and `kid`. A compatibility kit exists (`a2aproject/a2a-tck`); it marks signed-card verification as not automatable | The set of operations a v1.0 server must implement. Any patch release after 1.0.0 |
| OpenTelemetry GenAI conventions | The conventions moved to the repository `open-telemetry/semantic-conventions-genai`. That repository has no tag or release. Agent spans are in Development status. The internal agent span is named `{gen_ai.operation.name} {gen_ai.agent.name}`, has kind INTERNAL, requires `gen_ai.operation.name`, and recommends `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`. No cost attribute is defined | The attribute table for the execute-tool span and the inference span. The last tagged core release that still contained `gen_ai.*` |
| in-toto | Statement v1: `_type` is `https://in-toto.io/Statement/v1`; `subject` and `predicateType` are required and each subject must carry a `digest` | Naming guidance for custom predicate types |
| SLSA | Version 1.2 is the approved specification | Source track level definitions |
| CycloneDX | SCF's committed SBOMs declare `specVersion` 1.7 (`release/sbom/forge-source.cdx.json`, `deploy/sbom/*.cdx.json`) | Whether a later CycloneDX version has been published |
| `Assisted-by` commit trailer | The Linux kernel's coding-assistants page forbids AI agents from adding `Signed-off-by` | The `Assisted-by` trailer and its format. The two kernel pages opened do not mention it. The research note's claim that it is the convergent trailer is reported only |
| OWASP Top 10 for Agentic Applications for 2026 | Published by the OWASP GenAI Security Project, dated 9 December 2025, entries ASI01 to ASI10 | The full text of each entry |
| Agent Baseline | `v1.0-draft`, 30 July 2026, 35 controls in six outcomes (Discover 7, Constrain 4, Authorize 9, Validate 4, Observe 6, Respond 5). Prose is CC BY 4.0, schemas Apache-2.0 | The text of each control. Whether a later version exists |

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A current MCP client connects to SCF (Priority: P1)

A developer points an MCP client that implements the current protocol revision at SCF's `/mcp` endpoint with a token. The client discovers the server, lists tools and calls one without a handshake. A client that still uses the earlier handshake keeps working.

**Why this priority**: MCP is the lowest-friction way into SCF (ADR-0006) and the seam tervin uses (spec L seam 1). SCF implements revisions `2025-06-18` and `2025-03-26` today, two revisions behind the current one.

**Independent Test**: Send a `server/discover` request and a `tools/call` request in the current revision's shape to the endpoint with no prior `initialize`, and receive valid results. Then run the published conformance suite against a running SCF and read its report.

**Acceptance Scenarios**:

1. **Given** a running SCF and a valid token, **When** a client sends `server/discover` with no earlier request, **Then** the result lists the protocol versions SCF supports, its capabilities and its identity.
2. **Given** a request in the current revision's shape, **When** a required request header is missing or disagrees with the body, **Then** SCF rejects it with the status and error code the revision defines, and no tool runs.
3. **Given** a client that sends `initialize` with an earlier supported revision, **When** it lists and calls tools, **Then** behaviour is unchanged from today.
4. **Given** the published conformance suite at a pinned release, **When** it runs against SCF's endpoint, **Then** every applicable server scenario passes, or the scenario is listed in a baseline with a written reason.

---

### User Story 2 - An operator sees agent work in their own telemetry backend (Priority: P2)

An operator who runs an OpenTelemetry collector sees one trace per agent run, with a span for the run, a span per model call and a span per tool call, named and attributed the way the GenAI conventions describe, and a cost figure on each model call.

**Why this priority**: Buyers ask for attribution and cost governance. SCF has real OTLP export and a cost ledger, but no agent or tool call is a span, so the two never meet.

**Independent Test**: Run a scripted agent run in a test with the in-memory span sink and assert the span tree, names and attributes.

**Acceptance Scenarios**:

1. **Given** an agent run with two model calls and one tool call, **When** the run finishes, **Then** the trace has one agent span that is the parent of two inference spans and one tool span.
2. **Given** a model with a known price, **When** a model call completes, **Then** its span carries token counts under the convention's names and a cost under a forge-owned name.
3. **Given** a model with no known price, **When** a model call completes, **Then** the span carries no cost attribute, never a zero.
4. **Given** default settings, **When** spans are recorded, **Then** no prompt text, completion text or tool argument appears in any attribute.

---

### User Story 3 - An auditor verifies who and what produced a change (Priority: P2)

An auditor holding only a commit and SCF's public key retrieves the signed statement for that commit, verifies it offline, and reads which agent, model, policy and sandbox tier produced it. The commit itself says an agent assisted, and carries no human sign-off the agent could not give.

**Why this priority**: The attestation exists but its subject is a hash of its own predicate, the signed envelope cannot be retrieved over the API, and agent commits name no model.

**Independent Test**: Create an attestation for a run with a known head commit in a test, fetch the envelope, verify it with `scf-verify --attestation`, and compare the subject digest with the commit.

**Acceptance Scenarios**:

1. **Given** an attested agent change, **When** the statement is decoded, **Then** a subject digest equals the head commit of the change.
2. **Given** an attestation id in the caller's workspace, **When** the caller requests the envelope, **Then** SCF returns the DSSE envelope and offline verification succeeds with the public key.
3. **Given** an agent commit, **When** its message is read, **Then** it carries one assistance trailer naming the agent and model, and no `Signed-off-by` line.
4. **Given** the committed SBOM files, **When** the test suite runs, **Then** each declares CycloneDX at the pinned version.

---

### User Story 4 - Another agent platform discovers and delegates to SCF (Priority: P3)

An A2A client fetches SCF's Agent Card from the well-known location, verifies its signature, and uses the interface the card declares to start an SCF run and read its state.

**Why this priority**: A2A is the agent-to-agent counterpart of MCP. It is lower priority because no SCF user depends on it yet. It is larger than the roadmap line suggests: a conformant card must declare at least one interface, so SCF must serve one.

**Independent Test**: Fetch the card in a test, validate it against the pinned A2A schema, verify the signature with the published key, and call the declared interface.

**Acceptance Scenarios**:

1. **Given** a running SCF, **When** a client requests `/.well-known/agent-card.json`, **Then** it receives a card with every required field and at least one interface that SCF serves.
2. **Given** a stable signing key, **When** the card is fetched, **Then** it carries a signature that verifies over the canonicalised card with the key SCF publishes.
3. **Given** no stable signing key, **When** the card is fetched, **Then** it carries no signature. SCF never signs with a key that changes on restart.
4. **Given** a valid token, **When** a client sends a message through the declared interface, **Then** an SCF run is created through the same service the REST API uses, with the same authorisation and audit.

---

### User Story 5 - A security reviewer checks SCF against published control lists (Priority: P3)

A reviewer opens one document that maps each OWASP agentic risk and each Agent Baseline control to what SCF does, with a link to the code and the test that proves it, and an honest status where SCF does nothing yet.

**Why this priority**: It turns existing work into evidence a buyer can read. It depends on M3 (R15, R17) for the controls it cites.

**Independent Test**: Run the test that checks the mapping: every listed identifier appears once, every evidence link resolves, and no entry claims a control without a test.

**Acceptance Scenarios**:

1. **Given** the mapping, **When** the check runs, **Then** each of ASI01 to ASI10 and each Agent Baseline control identifier appears exactly once.
2. **Given** an entry marked implemented, **When** the check runs, **Then** it links to at least one existing test file.
3. **Given** an evidence link to a file that has been moved or deleted, **When** the check runs, **Then** it fails.

---

### Edge Cases

- A client sends a current-revision request with a session header from an earlier revision. The header is ignored and no session is created.
- A request declares a protocol version SCF does not support. SCF answers with the list of versions it does support and runs nothing.
- A legacy client sends `ping`. It keeps working for legacy requests even though the current revision removed it.
- The telemetry SDK is not installed or no collector is configured. Spans are still recorded in memory and a run never fails because of telemetry.
- A tool call is denied by policy. It is not reported as an executed tool.
- An agent supplies a commit message that already contains a `Signed-off-by` line.
- A run produced no commit. Its statement must not claim a commit digest.
- An attestation id from another workspace is requested. It reads as not found.
- A published control list changes after the mapping was written. The mapping records the version and date it was checked against.
- The A2A card is requested when SCF sits behind a path prefix or a reverse proxy that routes `/` to the web app.

## Requirements *(mandatory)*

### Functional Requirements

**MCP (R34)**

- **FR-001**: SCF's MCP server MUST support the protocol revision marked Current on the MCP versioning page at implementation time, and MUST name that revision in one place in code and in ADR-0016.
- **FR-002**: The server MUST answer `server/discover` without any earlier request, with its supported versions, capabilities and identity.
- **FR-003**: The server MUST accept current-revision requests that carry their protocol version and client information per request, and MUST validate the required request headers against the body, rejecting a mismatch as the revision defines.
- **FR-004**: The server MUST keep accepting the handshake-based revisions it supports today, with unchanged behaviour, until a later ADR removes them.
- **FR-005**: Results in the current revision MUST carry the fields that revision requires, including the result type and, on tool listings, the cache hints. Tool listings are filtered by the caller's permissions, so they MUST be marked as not cacheable by shared intermediaries.
- **FR-006**: Authentication, workspace scoping, write-tool hiding, Origin checking, body limits, redaction and one audit event per tool call (spec I section 4) MUST hold for every revision.
- **FR-007**: The repository MUST contain a way to run the published MCP conformance suite at a pinned release against an SCF endpoint, with a baseline of expected failures in which every entry has a written reason.
- **FR-008**: Whether SCF publishes OAuth protected-resource metadata for its MCP endpoint is [NEEDS CLARIFICATION: ADR-0006 defers OAuth discovery until SCF issues audience-bound tokens; the current revision's authorization requirements were not read. Decide whether R34 stays on bearer API keys only].

**A2A (R35a, R35b)**

- **FR-009**: SCF MUST serve an Agent Card at the well-known location, valid against the pinned A2A version, that declares only interfaces SCF serves.
- **FR-010**: SCF MUST serve at least one A2A interface through which an authenticated client can start a run and read its state, using the same services, authorisation, workspace scoping and audit as the REST API. Which A2A operations are mandatory and how a message names the target repository is [NEEDS CLARIFICATION: the required operation set of a v1.0 server was not confirmed, and SCF runs need a repository as well as an objective].
- **FR-011**: When a stable signing key is configured, the card MUST carry a JWS signature over the canonicalised card, and SCF MUST publish the verification key. Without a stable key the card MUST be unsigned.
- **FR-012**: The card MUST NOT disclose workspace data, tokens or internal addresses.

**Telemetry (R36)**

- **FR-013**: Every agent run MUST produce one agent span, one span per model call and one span per executed tool call, with the parent and child relationships of the run.
- **FR-014**: Span names and `gen_ai.*` attribute names MUST come from one pinned snapshot of the GenAI conventions, identified in code and docs by repository and tag or commit.
- **FR-015**: Cost MUST be reported under a forge-owned attribute name, never under `gen_ai.*`, and MUST be absent when the model has no known price.
- **FR-016**: Prompt text, completion text and tool arguments MUST NOT be captured by default. Existing redaction MUST apply to every span.
- **FR-017**: Spans MUST be recorded when no exporter is configured, and a telemetry failure MUST NOT fail or slow a run.

**Provenance (R37)**

- **FR-018**: The in-toto statement for an agent change MUST name the change as its subject: a digest of the head commit. A run with no commit MUST NOT claim one.
- **FR-019**: A caller MUST be able to retrieve the signed DSSE envelope of an attestation in their workspace and verify it offline with SCF's existing verifier.
- **FR-020**: Every commit an agent makes MUST carry one trailer that names the agent and the model, and MUST NOT carry a `Signed-off-by` line.
- **FR-021**: Changes to the predicate MUST be additive, so that statements signed before this feature still verify.
- **FR-022**: The SBOMs SCF commits MUST declare CycloneDX at one pinned version, checked by a test that needs no external tool. Whether each agent change needs its own SBOM is [NEEDS CLARIFICATION: the roadmap says "CycloneDX SBOM" without saying per change or per release; per-release generation exists today].

**Control mapping (R38)**

- **FR-023**: SCF MUST publish a mapping from each OWASP agentic risk and each Agent Baseline control to SCF's controls, with a status and evidence links, in a machine-readable file and a readable document.
- **FR-024**: A test MUST fail when an identifier is missing or duplicated, when an evidence link does not resolve, or when an entry is marked implemented without a linked test.
- **FR-025**: The mapping MUST record the version and date of each list it was checked against, MUST use identifiers and short titles with attribution, and MUST NOT copy the lists' descriptive text.
- **FR-026**: The mapping MUST refer to open security findings by their identifier in `SECURITY_FINDINGS.md` and MUST NOT describe how to exploit them.

**All**

- **FR-027**: Each standard SCF speaks MUST be pinned to a version in code and in ADR-0016, and a version change MUST be an explicit change with tests.
- **FR-028**: No surface may advertise a capability SCF does not have (ADR-0007).

### Key Entities

- **Pinned standard**: a standard, the version SCF implements, where the pin lives in code, and the date it was last checked against the primary source.
- **Conformance baseline**: the list of published-suite scenarios SCF does not pass, each with a reason.
- **Agent Card**: SCF's public self-description for A2A: identity, version, interfaces, capabilities, skills, optional signatures.
- **GenAI span**: a span for an agent run, a model call or a tool call, with convention attributes and SCF's cost attribute.
- **Changeset statement**: the in-toto statement for an agent change: subject (the commit), predicate (agent, model, policy, sandbox tier, tools, approver).
- **Control mapping entry**: an external control identifier, SCF's status against it, evidence links and a gap note.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The published MCP conformance suite, at the pinned release and the current revision, reports no failing server scenario outside the baseline, and the baseline holds no entry without a reason.
- **SC-002**: A client using only the current MCP revision completes discover, list and call against SCF in three requests with no handshake.
- **SC-003**: For a test run with N model calls and M executed tool calls, the trace holds exactly 1 + N + M GenAI spans.
- **SC-004**: An auditor can go from a commit to a verified statement about it with two commands and no database access.
- **SC-005**: 100 percent of agent commits created after this feature carry the assistance trailer and none carries `Signed-off-by`.
- **SC-006**: The Agent Card validates against the pinned A2A schema and its signature verifies with the published key.
- **SC-007**: The control mapping covers 10 of 10 OWASP agentic risks and 35 of 35 Agent Baseline controls, with zero broken evidence links.
- **SC-008**: Every standard in the table above has its pin in exactly one place in code.

## Assumptions

- ADR-0016 stays the governing decision. This milestone updates its version list; it does not change the rule.
- SCF keeps its hand-written MCP server (spec I section 2). No MCP SDK dependency is added.
- The implementation loop cannot add third-party dependencies. JWS signing uses `pyjwt` and Ed25519 from `cryptography`, both already dependencies of `forge-api`.
- The published MCP suite and the A2A compatibility kit need Node or a separate checkout and network access. They are run by a person or a CI job, and their results are recorded. Behaviour is proven in SCF's own test suite.
- R37 runs after R08, which attaches attestations to agent changes. R38 runs after R15 and R17, which create the controls it cites.
- MCP Tasks, MCP Apps, enterprise-managed authorization, Agent Skills loading, AGENTS.md and an ACP endpoint are out of scope here. ACP belongs to milestone M6.
- Regulatory mappings (EU AI Act, Cyber Resilience Act, ISO/IEC 42001, SOC 2) are out of scope.
