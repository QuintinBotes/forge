# Feature Specification: Product surface

**Feature Branch**: `009-product-surface`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Roadmap milestone M9 (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`
section 4): "OIDC sign-in in the web app. Remove sign-in by pasted API key. Web flow to start a run
from an issue, with browser end-to-end tests. Ten-minute self-host quickstart, verified from a
clean machine."

**Builds on**: `docs/SCF_SPEC.md` Core Design Principle 9 (self-hosting is first-class) and
feature F33 (SSO); lettered spec M (`docs/superpowers/specs/2026-11-07-bring-your-own-tracker-design.md`,
part M3, run dispatch from a tracker item); lettered spec G
(`docs/superpowers/specs/2026-11-07-agents-that-act-design.md`); ADR-0007 (production paths fail
closed); ADR-0019 (bring your own tracker). Roadmap issues: R42, R43, R44.

## Context

What exists today, verified in the code:

- The API already implements OpenID Connect sign-in. `apps/api/scf_api/routers/oidc.py` serves
  `GET /auth/oidc/{slug}/login` and `/callback`, validates the ID token, provisions the user and
  sets an HTTP-only `forge_session` cookie. Workspace OIDC settings are managed through
  `apps/api/scf_api/routers/sso_admin.py` and the web settings page
  `apps/web/src/app/(board)/settings/sso/page.tsx`.
- Nothing reads that cookie. Request authentication accepts only an `Authorization: Bearer` or
  `X-API-Key` header (`apps/api/scf_api/auth/service.py`, `get_authenticated_principal`), and the
  WebSocket accepts only a `?token=` value (`apps/api/scf_api/routers/realtime.py`). So a person
  who completes the OIDC flow is still not signed in to the web app.
- The web app signs in by a pasted API key (`apps/web/src/components/auth/connect-dialog.tsx`,
  `apps/web/src/lib/api/session.ts`). The key is held in browser storage.
- The API can start a run from a tracker item (`POST /agent/runs` with `tracker_ref`,
  `apps/api/scf_api/routers/agent.py`). The web app calls `agent/runs` only for replay. There is
  no route that lists runs, and the `/runs` screen opens a run only by a typed run id.
- The web tests run in jsdom. There is no browser test harness in the repository.
- A quickstart exists (`docs/self-hosting/quickstart.md`). No test or record shows how long it
  takes on a clean machine, and it ends with a pasted admin key.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Sign in with the organisation's identity provider (Priority: P1)

A team member opens Swarm Code Factory in a browser, chooses to sign in, authenticates with the identity
provider their workspace has configured, and lands on the board as themselves. They never see,
copy or store an API key. Signing out ends the session on the server.

**Why this priority**: A credential pasted into a browser is the first thing a security reviewer
rejects, and it attributes every action to one shared admin key. Every other story in this
milestone assumes a real signed-in person.

**Independent Test**: With a workspace whose OIDC configuration points at a test identity provider,
complete the sign-in flow in a browser and load the board. Fully testable without any other story.

**Acceptance Scenarios**:

1. **Given** a workspace with OIDC enabled and a visitor with no session, **When** the visitor
   opens any screen, **Then** they see a sign-in screen that offers sign-in through the identity
   provider and no field for an API key.
2. **Given** a visitor who completes the identity provider flow, **When** they are returned to
   SCF, **Then** the board loads, the top bar shows their own identity, and live updates
   (WebSocket and run event stream) work without any token in a URL or in browser storage.
3. **Given** a signed-in person, **When** they sign out, **Then** the session no longer
   authenticates any request, including a replay of the old cookie.
4. **Given** a signed-in person whose session has expired, **When** they make a request, **Then**
   they are returned to the sign-in screen and, after signing in, to the page they were on.
5. **Given** a page on another origin, **When** it makes a state-changing request to SCF using
   the visitor's session cookie, **Then** SCF refuses the request.
6. **Given** a script, CLI or CI job, **When** it calls the API with an API key in a header,
   **Then** it authenticates exactly as it does today.
7. **Given** a fresh installation with no identity provider, **When** an operator runs the CLI
   command on the server, **Then** it prints a single-use, short-lived sign-in link, and the first
   administrator who opens it is signed in and can configure an identity provider.
8. **Given** a sign-in link that was already used or has expired, **When** it is opened, **Then**
   it is refused.

---

### User Story 2 - Start a run from an issue in the browser (Priority: P2)

A signed-in member opens the runs screen, picks a connected tracker and an issue, and starts a run.
They see the run appear in a list with its state, follow its events as they arrive, and reach the
resulting pull request link when the run finishes.

**Why this priority**: "Issue in, attested change out" is the product. Today the only ways to start
a run are a tracker label or a hand-written API call, so the web app cannot show the product's
main action.

**Independent Test**: With one connected tracker and a stubbed worker, start a run from the browser
for a known issue and observe it in the run list. Testable with API-key sessions in a test build if
Story 1 is not yet present.

**Acceptance Scenarios**:

1. **Given** a member with a connected tracker, **When** they choose an issue and confirm,
   **Then** a run is created for that issue and they are taken to its page.
2. **Given** a run in progress, **When** the member watches its page, **Then** new events appear
   without a reload, each marked as gated or observed.
3. **Given** a workspace with runs, **When** a member opens the runs screen, **Then** they see the
   workspace's recent runs with state, source issue and start time, newest first, and none from
   another workspace.
4. **Given** a viewer without permission to run agents, **When** they open the runs screen,
   **Then** the start action is unavailable and the API refuses the request if it is sent anyway.
5. **Given** an issue reference that does not resolve, a repository that is not connected, or a
   worker queue that is unavailable, **When** the member confirms, **Then** the dialog states which
   of these happened and no run is recorded as started.
6. **Given** a real browser, **When** the automated end-to-end suite runs, **Then** it signs in,
   starts a run from an issue and sees it listed, and it fails on a defect that jsdom tests cannot
   see (for example a script blocked by the content security policy).

---

### User Story 3 - Self-host SCF in ten minutes (Priority: P3)

An engineer with Docker and nothing else follows one page and, within ten minutes of the first
command, is signed in to a working SCF that can accept a run.

**Why this priority**: Adoption of a self-hosted tool is decided in the first session. It ranks
third because it documents and verifies what Stories 1 and 2 and milestones M3 and M6 deliver.

**Independent Test**: On a machine with no SCF images, volumes or checkout, run the quickstart
commands exactly as written and time them.

**Acceptance Scenarios**:

1. **Given** a clean machine that meets the stated prerequisites, **When** the engineer runs the
   quickstart commands in order, **Then** every command succeeds unmodified and a signed-in board
   is reachable in at most ten minutes on the reference machine, image downloads included.
2. **Given** a completed quickstart, **When** the engineer runs the documented verification step,
   **Then** it reports each service healthy and exits zero, and it exits non-zero naming the
   failing service otherwise.
3. **Given** a change that renames a compose service, an environment variable or a command the
   quickstart uses, **When** the test suite runs, **Then** it fails until the quickstart is updated.
4. **Given** a completed quickstart, **When** the engineer inspects the stack, **Then** agent
   commands run in the container sandbox tier and no shared admin key remains in browser storage.

---

### Edge Cases

- The workspace has no identity provider configured yet. The sign-in screen says so and points to
  the operator instructions. It does not fall back to a key field in a production build.
- The identity provider is unreachable or returns an error. The visitor sees the failure reason and
  can retry. The failure is written to the audit log, as the callback does today.
- The sign-in transaction store is in process memory. With more than one API process a callback
  can reach a process that did not start the login. The behaviour must be defined: either the
  callback succeeds or it fails with a message that tells the visitor to retry.
- A person belongs to more than one workspace. The sign-in screen must let them reach the right one
  (the workspace slug is part of the sign-in address).
- The `next` address after sign-in points off-site. Only same-origin relative paths are honoured.
- A member starts a run twice for the same issue. The second request's outcome is stated to the
  user. It is not silently merged with the first.
- The issue body is very long or contains instructions. The existing server-side handling applies
  (the objective is built from the re-fetched issue and capped). The browser sends only the
  reference.
- The quickstart machine has a port in use or too little memory. The verification step names the
  problem.

## Requirements *(mandatory)*

### Functional Requirements

**Sign-in (R42)**

- **FR-001**: The API MUST authenticate a browser request that carries a valid session cookie
  issued by the existing OIDC or SAML callback, with the same principal, role and workspace that an
  equivalent API key would resolve to.
- **FR-002**: The API MUST continue to authenticate header-borne API keys unchanged for non-browser
  clients.
- **FR-003**: The API MUST refuse a state-changing request that is authenticated only by the
  session cookie and originates from another origin.
- **FR-004**: The WebSocket and run event stream endpoints MUST accept the session cookie, so the
  browser needs no token in a URL.
- **FR-005**: Signing out MUST invalidate the session on the server and clear the cookie.
- **FR-006**: The web app MUST present a sign-in screen to a visitor without a session and MUST
  start the identity provider flow for the visitor's workspace from it.
- **FR-007**: The web app MUST NOT offer, in a production build, any way to sign in by typing or
  pasting an API key, and MUST NOT keep an SCF credential in browser storage.
- **FR-008**: The web app MUST return the visitor to the page they asked for after sign-in, limited
  to same-origin paths.
- **FR-009**: The sign-in screen MUST state plainly when the workspace has no identity provider
  configured or the provider returned an error.
- **FR-010**: A first administrator MUST be able to reach a fresh installation and configure an
  identity provider. Decided 2026-11-15: when no identity provider exists yet, a CLI command run on
  the server prints a single-use, short-lived sign-in link, and the first administrator signs in
  with it. The development-only key flag is no longer the answer. Open: the decision does not give
  the command's name or the link's lifetime, and does not say whether key entry stays in
  development builds.

**Start a run from an issue (R43)**

- **FR-011**: The API MUST list a workspace's agent runs, newest first, with state, source tracker
  reference and timestamps, visible only to principals of that workspace.
- **FR-012**: The web app MUST let a member with the run-agent permission choose a connected
  tracker and an issue and start a run for it, using the existing tracker-reference run request.
- **FR-013**: The web app MUST show the created run's state and its events as they arrive, each
  with its gated or observed marking, and the pull request link when one exists.
- **FR-014**: The web app MUST report each refusal distinctly: unknown reference, repository not
  connected, no permission, queue unavailable.
- **FR-015**: The repository MUST contain an automated end-to-end suite that drives a real browser
  through sign-in and starting a run, and the project's continuous integration MUST run it.
- **FR-016**: The end-to-end suite MUST run without live credentials, against services it starts
  itself with test doubles for the identity provider, the tracker and the worker.

**Quickstart (R44)**

- **FR-017**: The quickstart MUST take a clean machine to a signed-in board with one documented
  sequence of commands that needs no edits beyond the values it names.
- **FR-018**: The repository MUST provide one verification command that checks every service the
  quickstart starts and reports the first failure by name.
- **FR-019**: A test MUST fail when the quickstart refers to a compose file, service, environment
  variable, make target or script that does not exist.
- **FR-020**: The quickstart MUST record the measured time of its last clean-machine run, with the
  date, the commit and the machine class.
- **FR-021**: The quickstart MUST use the container sandbox tier and the single-user profile and
  scoped tokens delivered by R14 and R31, and MUST NOT instruct the reader to paste an admin key
  into the browser.

### Key Entities

- **Browser session**: a time-limited credential issued after identity provider sign-in, bound to
  one user and one workspace, revocable on sign-out. Today it is a short-lived system API key
  carried in the `forge_session` cookie.
- **Workspace identity provider configuration**: issuer, client and enabled flag for a workspace,
  addressed by the workspace slug. Exists today.
- **Agent run**: one execution for an objective, with state, source tracker reference, events and
  an optional pull request link. Exists today; this feature adds a listing.
- **Tracker reference**: `provider:key`, for example a GitHub issue in a connected repository.
  Exists today.
- **Quickstart verification record**: date, commit, machine class and elapsed time of the last
  clean-machine run.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A new user signs in through their identity provider and reaches the board without
  handling an API key, in under one minute from the sign-in screen.
- **SC-002**: A production build of the web app contains no API-key input and writes no SCF
  credential to browser storage, shown by a test.
- **SC-003**: A signed-in member starts a run from an issue in five or fewer interactions from the
  runs screen and sees its first event without reloading.
- **SC-004**: The end-to-end suite fails when the application's scripts are blocked in a real
  browser, a class of defect the jsdom suite passed in the past.
- **SC-005**: The quickstart completes in ten minutes or less on the reference machine, measured
  from the first command to a signed-in board, on three consecutive clean runs.
- **SC-006**: Every command in the quickstart is covered by the reference test in FR-019.

## Assumptions

- The existing OIDC and SAML implementations are correct and are reused. This feature wires them to
  the browser. It does not add a password login or a new identity protocol.
- The browser reaches the API through the same origin as the web app (the Caddy edge), as the
  deployment documents describe. Cross-origin browser deployments are out of scope.
- "Clean machine" means Docker Engine and the Compose plugin installed, no SCF images, volumes or
  checkout. [NEEDS CLARIFICATION: the reference machine class and network speed for the ten-minute
  bar, and whether pulling images counts. The spec assumes 4 cores, 8 GB, a broadband connection,
  and that image pulls count.]
- The browser end-to-end suite needs a browser automation dependency and a continuous integration
  job. Neither exists today, and none of the current loop check ids runs a browser.
  [NEEDS CLARIFICATION: whether the owner adds a browser check to the loop policy, or the suite is
  proven only by the hosted CI job.]
- R44 follows R14 (container sandbox by default) and R31 (single-user profile, scoped tokens,
  device-code auth). The quickstart describes those as delivered and does not re-implement them.
- Picking an issue means entering or selecting a reference in a connected tracker. Browsing and
  searching a tracker's full backlog inside SCF is out of scope.
