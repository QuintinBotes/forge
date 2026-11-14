# Feature Specification: Tervin bridge

**Feature Branch**: `006-tervin-bridge`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M6 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`, issues R28 to R33) and roadmap decisions D1 and D2: "Forge runs appear in tervin as ACP sessions and are approved there. Tervin as forge's local runner is a prototype only." Target: tervin's published v2.2.0.

**Builds on**: lettered spec L (`docs/superpowers/specs/2026-11-07-tervin-bridge-design.md`), ADR-0002 (forge is the system of record, tervin its desktop face), ADR-0013 (one run-event vocabulary), ADR-0015 (desktop integration contract), ADR-0012 (two executor modes, Proposed), ADR-0007 (production paths fail closed).

## Why this exists

The roadmap's goal says forge "runs with tervin". Forge's side of spec L seams 1 to 3 is built: MCP tools, `forge-cli --json` and the run event stream. Three things stand between that and a developer watching and approving a forge run from tervin.

First, the two projects disagree. Forge's ADR-0002 makes forge the system of record and tervin its client. The roadmap records that tervin's spec 023 contradicts ADR-0002, ADR-0013 and ADR-0015. Spec L also claims that seam 1 needs only one `mcp.json` entry on tervin's side, while the roadmap records that tervin's MCP configuration is stdio-only and forge's MCP server speaks HTTP. A person has to settle this before code is written.

Second, the one integration that needs no change in tervin does not exist. Tervin accepts any agent that speaks the Agent Client Protocol (ACP) over stdio. Forge has no ACP code.

Third, the supporting pieces of spec L are missing: a token narrowed to what a desktop client needs, a way to obtain it without pasting a secret, a forge small enough to run beside tervin on one machine, and a record of which surface decided an approval.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - One written position on how forge and tervin fit (Priority: P1)

The owner reads one ADR that lists the ways the two projects can fit together, states the default (forge runs appear in tervin as ACP sessions and are approved there), says what happens to each contradicted statement, and corrects spec L's seam 1 claim.

**Why this priority**: Every other story in this spec assumes the outcome. Building first and deciding later risks code that the other repository will not consume.

**Independent Test**: A reviewer can read the ADR and answer, without other sources: which project owns the record, how a forge run reaches tervin, how forge's MCP tools reach an agent inside tervin, and which earlier statements are superseded.

**Acceptance Scenarios**:

1. **Given** the ADR, **When** a reader looks for the options, **Then** each option is listed with what it needs from each project and its main risk, and one is marked as the default.
2. **Given** the accepted ADR, **When** a reader opens spec L section 2, **Then** the seam 1 row no longer says tervin needs only one `mcp.json` entry and no code change, and it points to the ADR.
3. **Given** the accepted ADR, **When** a reader opens ADR-0002, ADR-0013 or ADR-0015, **Then** each states whether it stands, is amended or is superseded.

---

### User Story 2 - Watch and approve a forge run from an ACP client (Priority: P1)

A developer adds `forge acp` as an agent in tervin, or in any other ACP client. They type a request. A forge run starts on the forge server. The run's messages and tool activity appear in the client as the session progresses, each marked as gated by forge or only observed. When the run reaches an approval that the developer is allowed to decide, the client asks them, and their answer is recorded in forge.

**Why this priority**: This is the owner's definition of "runs with tervin" (D1), and it is the only shape in which one side already has everything it needs.

**Independent Test**: Drive `forge acp` over stdio with a scripted ACP client against a stubbed forge server: initialise, open a session, send a prompt, and observe session updates, one permission request, the decision posted to forge, and the end of the turn.

**Acceptance Scenarios**:

1. **Given** a configured server and token, **When** the client sends a prompt, **Then** exactly one forge run is created and its events arrive in the client in order.
2. **Given** a run with a pending approval tied to it, **When** the shim sees it, **Then** the client receives a permission request, and the client's answer is posted to forge as that approval's decision.
3. **Given** a tool call that forge's policy already allowed or denied, **When** it reaches the client, **Then** it is shown as an update marked gated by forge, and the client is never asked to permit it.
4. **Given** the connection to forge drops mid-run, **When** the shim reconnects, **Then** the session continues with no missing and no repeated event.
5. **Given** the client cancels the session, **When** the shim receives the cancellation, **Then** it stops following the run and reports that the run continues on the server unless forge exposes cancellation.
6. **Given** no token in the named environment variable, **When** the shim starts, **Then** it reports a configuration error and never prompts for or prints a secret.

---

### User Story 3 - The record says who gated what and where it was decided (Priority: P2)

A security reviewer reads a run's events and its approval audit entries. Each event states who observed it. Each approval decision states the surface it came from (forge's web app, the API, an ACP client such as tervin). An event can name a gate other than forge without forge claiming that it enforced the action.

**Why this priority**: ADR-0013 promises that enforcement is "set by whichever side actually gated each action", and spec L's acceptance criterion 3 requires the audit entry to name tervin as the surface. The current contract cannot express either.

**Independent Test**: Decide one approval through the ACP shim and one through the API, then read the audit chain and the `/ws` events for both.

**Acceptance Scenarios**:

1. **Given** an approval decided through the ACP shim, **When** the audit entry is read, **Then** it names the deciding surface and the authenticated principal, and the audit chain still verifies.
2. **Given** a decision that names no surface, **When** it is recorded, **Then** the surface is recorded as the API, never as a desktop client.
3. **Given** an approval opened by any producer, **When** it is created and when it is decided, **Then** one event for each reaches every `/ws` connection of that workspace and no connection of another workspace.
4. **Given** an event whose gate was not forge, **When** it is stored or streamed, **Then** `observed_by` names that gate and forge does not mark the event as enforced by forge.
5. **Given** an event that claims an observer forge does not know, **When** it is validated, **Then** it is rejected.

---

### User Story 4 - Run forge beside tervin on one machine with a narrow token (Priority: P2)

A developer starts a small forge (API, worker and database only) with one command, signs a desktop client in by confirming a short code in the browser, and ends up with a token that can read the board, specs, approvals and runs, start runs and decide approvals, and nothing else.

**Why this priority**: Without this, trying the bridge means running the full stack and handing a desktop client an API key that carries a whole role's permissions.

**Independent Test**: Start the single-user profile, complete the device-code flow, call one allowed route and one route outside the token's scope.

**Acceptance Scenarios**:

1. **Given** the single-user profile, **When** it starts, **Then** only the API, the worker, the database and what those strictly need are running, and the API reports healthy.
2. **Given** a token scoped to reading and to deciding approvals, **When** it calls an administrative route, **Then** the call is refused, whatever the role of the person who created the token.
3. **Given** a device-code request, **When** the person confirms the code while signed in, **Then** the waiting client receives a scoped token once, and a second poll with the same code receives nothing.
4. **Given** a device code that is not confirmed in time, **When** the client polls, **Then** it is told the code expired.
5. **Given** an existing API key created before scoped tokens, **When** it is used, **Then** it behaves as before.

---

### User Story 5 - Learn whether tervin can be forge's local runner (Priority: P3)

The owner wants to know what it would take for a forge run to execute on a developer's machine behind tervin's rules (spec L seam 5). A prototype on forge's side shows a run being parked for a runner, leased, streamed back and completed, and a written finding says whether to build it properly.

**Why this priority**: D1 marks this shape as a prototype only. It needs new work in both repositories and is tied to a single-user desktop app.

**Independent Test**: With the prototype switched on, a scripted runner leases a parked run, posts events and a result, and the run ends with those events in its stream. With the prototype switched off, none of the routes exist.

**Acceptance Scenarios**:

1. **Given** the prototype is off (the default), **When** any runner route is called, **Then** it is not found, and runs execute on the worker as before.
2. **Given** the prototype is on and a run is marked for a runner, **When** a registered runner leases it, **Then** no other runner can lease the same run.
3. **Given** a leased run, **When** the runner posts events, **Then** they appear in the run's event stream with `observed_by` naming the runner.
4. **Given** a lease that is not renewed, **When** it expires, **Then** the run ends as failed with a stated reason and is not silently retried.

---

### User Story 6 - Prove it on a Mac with both apps (Priority: P2)

A tester runs forge's single-user profile and tervin v2.2.0 on one macOS machine, adds `forge acp` as an agent, starts a run, sees it progress, approves a gate in tervin, and finds the decision in forge's audit chain naming tervin as the surface.

**Why this priority**: Stories 2 to 4 are tested against stubs. Only a paired run shows that the two real programs agree.

**Independent Test**: Follow the written test script and fill in the test record.

**Acceptance Scenarios**:

1. **Given** the test script, **When** a tester follows it on a clean macOS account, **Then** each step has an expected observation and the record states pass or fail for each.
2. **Given** a completed test, **When** the record is read, **Then** it states the forge commit, the tervin version, the macOS version, the date, and the evidence for each step.
3. **Given** a step that fails, **When** the record is filed, **Then** an issue is linked for it.

---

### Edge Cases

- The developer who prompts is not allowed to decide the approval that the run opens. The client is not asked. The run's update says an approval is pending for someone else.
- An approval tied to the run is decided on another surface while the client is showing the request. The shim withdraws or completes the request with the recorded outcome and does not post a second decision.
- Forge's native loop treats a "requires approval" policy result as a denial and does not pause the run. Such an event is shown as denied, never as a question.
- The run ends while a permission request is open. The request is closed and the turn ends with the run's terminal state.
- The event stream closes at its lifetime limit during a long turn. The shim reconnects from its last event id.
- An ACP client offers a protocol version the shim does not support. The shim answers with the version it supports and the client decides.
- A second prompt arrives in a session whose run is still active. It is refused with a clear message. One prompt is one run.
- A scoped token is used on `/ws`. It receives approval events only for its own workspace.
- A device code is guessed. Codes are single-use, short-lived and rate-limited.
- The single-user profile is started on a machine where the full stack is already running. Project names and ports do not collide.
- Tervin is absent. Every forge feature in this spec still works with any other ACP client and with `forge-cli`.

## Requirements *(mandatory)*

### Functional Requirements

**Decision record (R28)**

- **FR-001**: An ADR MUST list the integration options (forge as an ACP agent that the desktop client gates; the desktop client as a reader of forge's record through seams 1 to 4; the desktop client as forge's local runner; each project keeping its own core), state for each what it needs from forge and from tervin and its main risk, and name the default from roadmap decision D1.
- **FR-002**: The ADR MUST state, for ADR-0002, ADR-0013 and ADR-0015, whether each stands, is amended or is superseded, and MUST state which tervin release the bridge targets (D2).
- **FR-003**: Spec L MUST be corrected: seam 1 no longer claims that one `mcp.json` entry and no code change are enough, and states how an HTTP MCP server reaches a client whose MCP configuration is stdio-only. [NEEDS CLARIFICATION: whether forge ships a stdio-to-HTTP MCP proxy command for this, or documents that agents inside tervin reach forge's MCP server through their own HTTP MCP configuration. The owner decides in R28.]
- **FR-004**: The ADR and spec L MUST describe tervin only from forge's public documents and the roadmap. They MUST NOT quote tervin's source, paths or internals.

**ACP shim (R29, seam 6)**

- **FR-005**: `forge-cli` MUST offer an `acp` command that speaks the Agent Client Protocol as an agent over stdio, at one pinned protocol version stated in the documentation.
- **FR-006**: A prompt MUST create exactly one forge run through the same route every other client uses, and the session MUST follow that run's event stream.
- **FR-007**: Each run event MUST reach the client as a session update that carries the event's enforcement (gated or observed) and its observer.
- **FR-008**: The shim MUST ask the client for permission only for an approval that exists in forge, is pending, is tied to the session's run, and that the token's principal may decide. It MUST post the client's answer as that approval's decision and MUST treat a conflict (already decided) as the recorded outcome.
- **FR-009**: The shim MUST NOT ask the client to permit an action that forge's policy has already decided.
- **FR-010**: The shim MUST resume the event stream after a disconnect with no gap and no duplicate.
- **FR-011**: The server address and the name of the environment variable that holds the token MUST be configurable. The token MUST never be printed, logged or accepted on the command line.
- **FR-012**: Standard output MUST carry protocol messages only. Diagnostics go to standard error.
- **FR-013**: The shim MUST work with any ACP client and MUST NOT depend on tervin.

**Provenance and approval record (R30, seam 4)**

- **FR-014**: `RunEvent.observed_by` MUST be able to name an observer other than forge, from a closed, validated set of forms defined by the R28 ADR. An event not observed by forge MUST NOT be marked as enforced by forge.
- **FR-015**: Events that forge emits itself MUST keep the ADR-0013 mapping unchanged.
- **FR-016**: Every approval creation and every approval decision MUST produce one event on `/ws` for the approval's workspace, whichever producer opened the gate.
- **FR-017**: An approval decision MUST record its deciding surface in the audit chain together with the authenticated principal. An undeclared surface MUST be recorded as the API. [NEEDS CLARIFICATION: whether the surface is taken from the credential (a token registered for a named client) or declared by the client and recorded as a declaration. A declared value can be forged by any client holding a valid token. The owner decides in R28.]
- **FR-018**: The `/ws` approval events MUST carry the provenance fields of ADR-0015.

**Local profile and auth (R31)**

- **FR-019**: A single-user profile MUST start the API, the worker and the database, with no object store, no reverse proxy and no workflow server, and MUST be startable with one command.
- **FR-020**: A person MUST be able to create a personal access token whose permissions are a stated subset: read the board, specs, approvals and runs; start runs; decide approvals. A scoped token MUST be refused on every route outside its scope and MUST never exceed its creator's role.
- **FR-021**: Existing API keys MUST keep their current behaviour.
- **FR-022**: Forge MUST offer a device-code flow: a client requests a code, the person confirms it in a signed-in browser session, and the client receives a scoped token once. Codes MUST be single-use, short-lived and rate-limited.
- **FR-023**: The single-user profile MUST fail closed like any other profile. It MUST NOT enable a development-only bypass in a production configuration (ADR-0007).

**Runner prototype (R32, seam 5)**

- **FR-024**: Behind a switch that is off by default and refused in production, forge MUST let a registered runner lease a run, stream run events and submit a result.
- **FR-025**: A leased run MUST have one leaseholder. An expired lease MUST end the run as failed with a reason.
- **FR-026**: The prototype MUST end with a written finding that recommends building, changing or dropping seam 5.

**Paired test (R33)**

- **FR-027**: A written, repeatable test script MUST cover: start the single-user profile, obtain a token, add `forge acp` to tervin, start a run, watch it, decide an approval in tervin, and verify the audit entry.
- **FR-028**: A dated test record MUST state the versions, the outcome of each step and its evidence, with no secret and no personal path.

### Key Entities

- **Integration decision**: The ADR that fixes how the two projects fit and what earlier statements it changes.
- **ACP session**: One conversation between an ACP client and the shim. Holds at most one active forge run.
- **Run event**: One event of a run, with its enforcement and its observer (ADR-0013).
- **Approval decision**: One person's decision on a gate, with the principal and the deciding surface.
- **Scoped token**: A personal access token limited to a stated subset of permissions.
- **Device authorisation**: A pending request by a client for a scoped token, confirmed or denied by a signed-in person.
- **Runner lease**: The exclusive, expiring claim of one runner on one run (prototype).
- **Test record**: The dated outcome of the paired macOS test.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The ADR is accepted by the owner before any issue that depends on it starts.
- **SC-002**: A scripted ACP client completes initialise, new session, prompt, at least one update, one permission round trip and end of turn against a stubbed forge, with no forge-side change to tervin required.
- **SC-003**: After a forced disconnect in the middle of a run, the client has received every event exactly once.
- **SC-004**: For an approval decided through the shim, the audit chain verifies and the entry names the surface and the principal.
- **SC-005**: A scoped token is refused on every route outside its scope in the route inventory test.
- **SC-006**: From a clean checkout, the single-user profile reaches a healthy API in under five minutes on a developer machine.
- **SC-007**: The paired macOS test record shows every step passing, or links an issue for each step that did not.
- **SC-008**: With the runner prototype off, the route inventory and the behaviour of existing runs are unchanged.

## Assumptions

- Roadmap decisions D1 and D2 hold unless the owner changes them in R28.
- Tervin is a private repository. This spec relies only on forge's public statements about it: it is a macOS desktop terminal, it holds no secrets and reads tokens from named environment variables, it opens no listening port, it admits tools as a process, a JSON CLI, MCP or repository data, and it accepts ACP agents over stdio with no code change.
- No code moves between the repositories in either direction (ADR-0010, ADR-0015).
- In forge's acting path today, a policy result of "requires approval" ends the tool call as denied and flags the run for a person. It does not open an approval or pause the run. Approvals that a client can decide therefore come from gates opened by the governed pipeline (M1, M2, R12, R19). The shim is built to handle both.
- ACP is treated as an external standard pinned to one version, in the manner of ADR-0016.
- Changes inside tervin are tracked in tervin's repository and are out of scope here.
- The paired test needs a Mac, a tervin v2.2.0 build and a person. It cannot run in CI.
