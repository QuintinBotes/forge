# Feature Specification: Container and Kubernetes Execution

**Feature Branch**: `011-container-and-kubernetes-execution`

**Created**: 2026-11-14

**Status**: Draft

**Input**: Owner request: "support kubernetes and run agents in pods", with "a spec and github issues for the kubernetes and docker stuffs". The outcome wanted is that a self-hoster can run agent work behind a real isolation boundary on either a Docker host or a Kubernetes cluster, and that a contributor with Docker can run the whole test suite locally.

**Builds on**: `specs/003-containment/spec.md` (sandbox default, escape tests, credentials), ADR-0007 (production paths fail closed), ADR-0008 (agents act through a policy-gated tool belt with scoped credentials), ADR-0013 (one run-event vocabulary, the worker is the single writer of a run's events), ADR-0012 (two executor modes, proposed), `docs/self-hosting/security.md` (sandbox tiers), `docs/self-hosting/kubernetes.md`, `docs/security/threat-model.md` (surface S6), lettered spec G (`docs/superpowers/specs/2026-11-07-agents-that-act-design.md`, sections 3.3 and 4) and the roadmap (`docs/superpowers/plans/2026-11-14-industry-standard-roadmap.md`, milestone M3).

## Why this exists

Swarm Code Factory isolates the commands an agent runs through a sandbox tier. Three of the four tiers
(`container`, `gvisor`, `microvm`) need a Docker endpoint, so a Kubernetes install can only use the
host-process tier, which is no boundary at all. On a Docker host the container tier exists but the
main run path does not yet use it correctly: a run's worktree is created in the worker's temporary
directory, not on the worktree volume, and no caller passes the per-run mount subpath. The agent
loop itself (model calls, file tools, run events) always runs inside a shared worker process, so
run capacity is bounded by worker replicas and one run can starve another. Finally, a contributor
cannot run the full test suite without knowing two container names that no command creates.

This spec covers where agent work runs. Spec 003 covers what containment must hold.

## Relationship to spec 003

- Spec 003 FR-001 to FR-005 (container default, no fallback, tier recorded) stay authoritative. This spec restates them only where the Kubernetes backend changes how they are met.
- Spec 003 FR-005 left the Helm default tier open. This spec answers it: on Kubernetes the default tier is `container`, served by the pod backend (FR-005 here, delivered by #234).
- Spec 003 FR-006 to FR-010 (escape and exhaustion tests, published levels) are extended here to the pod backend (FR-009).
- Spec 003 FR-011 to FR-015 (per-workspace, short-lived credentials) are unchanged. This spec adds where a credential may and may not be placed when work runs in a container or pod (FR-010 to FR-013).
- Nothing here adds a sandbox tier. A backend says where a tier runs. An executor says where the agent loop runs. Neither changes the tier ordering.

## Isolation levels: what each stops and what it does not

The tier names the isolation class. It means the same on a Docker host and on Kubernetes.

| Tier | Boundary | Stops | Does not stop |
|---|---|---|---|
| `worktree` | None. Commands are processes of the worker. | Nothing. | Reading the worker's files, environment and network. Development only. |
| `container` | Linux namespaces and control groups, shared host kernel. A container on a Docker host, a pod on Kubernetes. | Reading the worker's files and environment. Reaching another run's worktree. Network access unless the egress allowlist is enabled. Gaining privileges. Exhausting host processes, memory or scratch disk. | A defect in the shared host kernel or the container runtime. Timing and hardware side channels. Sending data to a host on the egress allowlist when egress is enabled. |
| `gvisor` | A userspace kernel between the workload and the host kernel. | Everything `container` stops, plus direct use of most host kernel interfaces. | A defect in the userspace kernel. Hardware side channels. |
| `microvm` | A hardware virtual machine with its own guest kernel. | Everything `gvisor` stops, plus host kernel defects reachable from a guest process. | A defect in the hypervisor. Hardware side channels. |

Running the agent loop in its own container or pod (User Story 3) is not a tier. It bounds the
resources of one run and keeps delivery credentials and the database credential out of the process
that talks to the model. It does not sandbox commands; those still run in a sandbox session of the configured tier.

No tier stops a harmful change from being proposed in the diff. Review, gates and approvals in
specs 001, 003 and 004 handle that. No tier hides prompt content from the configured model
provider.

Backend residuals differ. On a Docker host, whoever controls the worker can ask the container
daemon, through the socket proxy, to create containers. On Kubernetes, whoever controls the worker
can create pods in the sandbox namespace only, within the namespace's pod security level. The
published isolation levels (spec 003 FR-010) record these per backend.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Agent commands run in a container on a Docker host (Priority: P1)

A self-hoster runs SCF with Docker Compose on one machine. Every command an agent runs executes
in a locked-down container that sees only that run's repository checkout. The run's checkout and
the repository mirror live on the shared worktree volume, so the sandbox container can mount
exactly one run's directory.

**Why this priority**: this is the default install and the path the owner can exercise today. The
container tier is not real until the run's files are where the container can mount them.

**Independent Test**: on a Docker host, start a run against a repository whose policy declares a
command that lists the working directory and writes a file. The command sees the repository files,
the written file appears in the run's branch, and a second concurrent run's directory is not
visible.

**Acceptance Scenarios**:

1. **Given** shipped Compose defaults, **When** an agent run executes a declared command, **Then** the command runs in a container whose only writable mount is that run's checkout.
2. **Given** two concurrent runs, **When** each lists its workspace, **Then** neither sees the other's files.
3. **Given** a container-backed tier and a checkout that is not on the worktree volume, **When** the run starts, **Then** it fails with an error that names the setting, and no container is created with a wider mount.
4. **Given** the container daemon is unreachable, **When** a run starts, **Then** the run fails with a clear error and never uses the host-process tier.

---

### User Story 2 - Agent commands run in a pod on Kubernetes (Priority: P1)

An operator installs SCF with the Helm chart. Each sandbox session is one pod in a dedicated
namespace: no service account token, non-root, read-only root filesystem, all capabilities dropped,
resource limits, and a network policy that denies ingress and limits egress. `gvisor` and `microvm`
map to runtime classes. The worker needs permission in that one namespace and no container socket.

**Why this priority**: without it a Kubernetes install has no isolation boundary. It is the owner's
stated request.

**Independent Test**: install the chart on a kind cluster with the pod backend enabled, start a
run, observe its commands execute in a sandbox pod with the locked-down settings, and observe the
pod is gone after the run.

**Acceptance Scenarios**:

1. **Given** the pod backend, **When** a run executes a command, **Then** it runs in a pod created for that sandbox session and labelled with the run.
2. **Given** a sandbox pod, **When** its specification is inspected, **Then** it holds no service account token, no credential value, and no host mount.
3. **Given** the cluster API is unreachable, the namespace is missing, or a required runtime class does not exist, **When** a run starts, **Then** it fails with a clear error and never uses a weaker tier.
4. **Given** a run has finished or its worker has died, **When** the reaper runs, **Then** the run's sandbox pods are deleted and no other pod is touched.
5. **Given** a sandbox pod, **When** it tries to reach the cluster API or the cloud metadata address, **Then** the connection is refused by policy.

---

### User Story 3 - Each agent run executes in its own container or pod (Priority: P2)

An operator wants run capacity to follow the cluster, not the number of worker replicas. With the
run executor set to `docker` or `kubernetes`, the worker launches one container or one Job per run
and returns. Decided 2026-11-15: this is the shipped default. The Helm chart defaults to one Job
per run and Compose defaults to one container per run. `inline` stays available as an explicit
setting. The run process executes the agent loop and streams events. When it exits, SCF's
control plane closes the run out: delivery, terminal events, recorded outcome and tracker
write-back. The run process never holds delivery credentials or the database credential. It reaches
its run record with a short-lived token scoped to its own run, used against SCF's API.

**Why this priority**: it removes the shared-process bottleneck and the short task time limit, and
it narrows what a misbehaving loop can reach. It builds on stories 1 and 2, so it comes second.

**Independent Test**: set the executor to `docker` on a Docker host, start two runs, and observe
two run containers, events arriving in each run's stream while it executes, both runs reaching a
terminal state with the same recorded outcome an in-worker run gives, and both containers removed.

**Acceptance Scenarios**:

1. **Given** a non-inline executor, **When** a run is dispatched, **Then** the worker launches one unit for it and its task slot is released before the run finishes.
2. **Given** a launched run, **When** the agent takes a step, **Then** the step's events appear in the run's event stream before the run ends.
3. **Given** a run process has exited with a result, **When** close-out runs, **Then** delivery, terminal events, outcome and tracker write-back happen exactly once, even if close-out is triggered twice.
4. **Given** a run's unit is killed, evicted or lost, **When** the reconciler next runs, **Then** the run is recorded as failed with a reason and does not stay pending or running.
5. **Given** the executor's runtime is unavailable, **When** a run is dispatched, **Then** the run fails with a clear error and is never executed inside the worker instead.
6. **Given** a run process, **When** its environment and specification are inspected, **Then** they hold no source-control application key, no master secret key, no webhook secret and no database credential.
7. **Given** shipped defaults, **When** SCF is installed with the Helm chart, **Then** each agent run is one Job. **When** it is started with Compose, **Then** each agent run is one container. A run executes inline only where `inline` is set explicitly.
8. **Given** a run process, **When** it uses its token on another run's record or on any other API route, **Then** the call is refused.

---

### User Story 4 - A contributor runs the full test suite locally with Docker (Priority: P1)

A contributor with Docker installed runs one documented command. It creates and starts the Postgres
and Redis test services on loopback, at the versions CI uses, and a second command runs the whole
Python suite against them. Without the services, tests that need them are reported as skipped with
a reason, not as connection failures.

**Why this priority**: every issue in the roadmap is proved by `python-unit-tests`. A suite that
fails for an environmental reason hides real failures, for people and for the autonomous loop.

**Independent Test**: on a machine with Docker and no test containers, run the start command twice
and then the suite: the services exist once, the Postgres and Redis tests run. Stop the services
and run the suite again: those tests are skipped with reasons that name the setting and the
command.

**Acceptance Scenarios**:

1. **Given** no test containers exist, **When** the contributor runs the start command, **Then** both services are created, healthy and bound to loopback only.
2. **Given** the services already exist, stopped or running, **When** the start command runs again, **Then** it succeeds and changes nothing else.
3. **Given** Redis is not reachable, **When** the suite runs, **Then** every test that needs Redis is skipped with a reason and none fails with a connection error.
4. **Given** Redis is reachable, **When** the suite runs, **Then** those tests run and assert exactly what they assert today.

---

### Edge Cases

- The Docker host is a desktop virtual machine: the `container` tier works; `gvisor` and `microvm` are reported as unavailable, never substituted.
- The container engine is older than the version that supports mounting a volume subpath: the run fails at preflight with the required version.
- The worker and the sandbox run as different users: the sandbox user must be able to write the run's checkout, and the worker must be able to read the result.
- Git metadata is not inside the sandbox mount: a declared command that needs repository history fails visibly; this is documented, not hidden.
- The pod backend is selected with the `worktree` tier: `worktree` is unaffected by the backend and stays development-only.
- A sandbox pod is unschedulable or its image cannot be pulled: the run fails within a bounded time with the scheduler's reason, and the pod is deleted.
- The executor is `kubernetes` and the sandbox backend is `docker`, or the reverse, or a non-inline executor is combined with the `worktree` tier: the worker refuses the configuration at start.
- A run process exits successfully but close-out fails at delivery: the run is recorded as failed with the delivery error, as today.
- A run exceeds its wall-clock limit: the unit is stopped and the run is recorded as escalated.
- The worker restarts while units are running: runs continue, and the reconciler closes them out.
- Port 5432 or 6379 is already in use on the contributor's machine: the start command says which port is taken and how to choose another.
- `FORGE_TEST_DATABASE_URL` points at a database that is not the disposable test database: the documentation warns that fixtures drop tables; the start command only ever prints its own loopback address.

## Requirements *(mandatory)*

### Functional Requirements

**Sandbox backends**

- **FR-001**: A backend setting MUST choose where the container-backed tiers run: a container daemon or a Kubernetes cluster. The tier MUST keep naming the isolation class, and tier selection and the no-downgrade ordering MUST give the same answers on both backends.
- **FR-002**: On a Docker host with shipped defaults, agent commands MUST run in the `container` tier; `worktree` MUST be explicit and development-only (spec 003 FR-001, FR-002).
- **FR-003**: A run's checkout and its repository mirror MUST be stored where the sandbox can mount them, and a container-backed sandbox MUST mount only that run's checkout. A run whose checkout cannot be mounted that narrowly MUST fail before any sandbox is created.
- **FR-004**: On Kubernetes, each sandbox session MUST be one pod in a configured namespace with no service account token, a non-root user, a read-only root filesystem apart from the workspace, all capabilities dropped, no privilege escalation, resource limits and labels that identify the run. `gvisor` and `microvm` MUST map to configured runtime classes.
- **FR-005**: The Helm chart MUST provide the sandbox namespace, permissions for the worker limited to that namespace and excluding secrets, and a network policy that denies ingress to sandbox pods, limits egress to the configured allowlist and never allows the cluster API or the cloud metadata address. With the pod backend enabled the chart's default tier MUST be `container`.
- **FR-006**: When the selected tier, backend, namespace, runtime class or daemon is unavailable, the run MUST fail with an error that names what is missing. The system MUST never fall back to a weaker tier or to the host-process tier.
- **FR-007**: A sandbox MUST be removed when its session ends, and sandboxes whose run is finished or unknown MUST be reaped on both backends without touching anything else.
- **FR-008**: Every run MUST record the tier it executed under (spec 003 FR-004).
- **FR-009**: The escape and exhaustion suite and the published isolation levels (spec 003 FR-006 to FR-010) MUST cover the pod backend as well as the container daemon backend. A tier and backend pair that has not been run against a real runtime MUST be published as not tested.

**Credentials**

- **FR-010**: No credential value MUST appear in a sandbox container's or pod's specification, image, environment, labels or annotations, or in any file an agent command can read.
- **FR-011**: A run process MUST receive only what the agent loop needs: access to the model provider, the run's own record and event stream, and the sandbox backend. It MUST NOT receive the source-control application key, the master secret key, webhook secrets or the database credential, and it MUST refuse to start if it finds one.
- **FR-012**: Delivery of a change and tracker write-back MUST be performed by SCF's control plane after the run process exits, never by the run process.
- **FR-013**: On Kubernetes a run process MUST receive secrets only by reference to a cluster secret. No secret value MUST appear in a Job or pod specification, argument, label or annotation. Decided 2026-11-15: a run process reaches the run record with a short-lived token scoped to its own run, used against SCF's API. The run process MUST never hold the database credential. Open: the decision does not say how the token is handed to a run unit (a per-run token is not an existing cluster secret), or which API routes carry events and the handoff.

**Run executor**

- **FR-014**: An executor setting MUST choose where the agent loop runs: `inline` (inside the worker, today's behaviour), `docker` (one container per run) or `kubernetes` (one Job per run). Decided 2026-11-15: the shipped default is per-run isolation everywhere. The Helm chart defaults to `kubernetes` (one Job per agent run) and Compose defaults to `docker` (one container per run). `inline` stays available as an explicit setting. Open: the decision does not say what an unset value means outside the chart and Compose, or whether the development Compose overlay and the single-user profile follow the same default.
- **FR-015**: A run executed by a run process MUST produce the same recorded outcome, steps, artifacts and event sequence as the same run executed inline.
- **FR-016**: With a non-inline executor, dispatching a run MUST launch its unit and return without waiting for the run to finish.
- **FR-017**: Run events MUST reach the run's event stream while the run executes, numbered and redacted as today (ADR-0013).
- **FR-018**: Close-out of a launched run MUST happen exactly once and MUST be safe to trigger more than once.
- **FR-019**: A launched run whose unit fails, disappears or exceeds its wall-clock limit MUST reach a terminal state with a recorded reason. No run MUST stay pending or running without a live unit.
- **FR-020**: A run unit MUST be locked down (non-root, read-only root filesystem, capabilities dropped, no privilege escalation, resource limits), labelled with its run, removed after close-out, and reaped if orphaned.
- **FR-021**: When the executor's runtime is unavailable the run MUST fail with a clear error. The system MUST never execute a run inline because a non-inline executor failed.
- **FR-022**: A non-inline executor MUST be refused at worker start when combined with the `worktree` tier or with a sandbox backend on a different platform. Commands from a run process MUST still execute in a sandbox session of the configured tier.
- **FR-023**: Launch and close-out of a run unit MUST be recorded in the audit chain with the executor, the unit identifier and the outcome, and without any secret.
- **FR-024**: The Helm chart MUST provide what per-run Jobs need: permission for the worker to manage Jobs in one namespace, a separate identity for run pods limited to managing sandbox pods, a network policy for run pods, and storage that the worker, run pods and sandbox pods can all reach. Decided 2026-11-15: the storage model is a shared read-write-many volume. It keeps every credential out of run and sandbox pods, and the chart refuses to render per-run execution without it.

**Local test services**

- **FR-025**: The repository MUST provide one documented command that creates and starts the Postgres and Redis test services, bound to loopback, and that is safe to run repeatedly. A matching command MUST stop and remove only those services.
- **FR-026**: The test services MUST use the same images as the CI test job, and a check MUST fail when they drift.
- **FR-027**: One documented command MUST run the whole Python suite against those services.
- **FR-028**: A test that needs Redis MUST be skipped with a reason naming the setting and the start command when Redis is unreachable, and MUST run unchanged when Redis is reachable. No assertion is removed or weakened.
- **FR-029**: Contributor documentation MUST list which test lanes still skip on a machine with only Docker (real-daemon sandbox tests, kind, gVisor, Firecracker, live providers), why, and how to opt in.

**Documentation**

- **FR-030**: Self-hosting documentation MUST state, for a Docker host and for Kubernetes, the prerequisites, what each tier stops and does not stop on that backend, and how to enable per-run execution.

### Delivery map

| Requirement | Delivered by |
|---|---|
| FR-002, FR-008, and FR-006 for the container daemon backend | #184 |
| FR-009 for the container daemon backend (and spec 003 FR-006 to FR-010) | #185 |
| FR-001, FR-004, FR-007 for pods, FR-006 for pods, FR-010 for sandbox pods | #233 |
| FR-005, and FR-030 for the pod backend | #234 |
| FR-003 | #237 |
| FR-009 for the pod backend | #243 |
| FR-011, FR-012, FR-015, FR-017 | #238 |
| FR-014, FR-016, FR-018, FR-019, FR-021, FR-022, FR-023 | #239 |
| FR-020 and FR-010 for run containers, FR-030 for Docker per-run execution | #240 |
| FR-013 (secrets by reference), FR-020 for run Jobs | #241 |
| FR-013 (the run-scoped token and the API routes a run process uses) | #256; the client a run process uses is #257 |
| FR-024, FR-030 for Kubernetes per-run execution | #242 |
| FR-025 to FR-029 | #236 |

#236 to #243 are the issue drafts written with this spec. #184 should follow #237: making `container`
the default before a run's checkout is on the worktree volume would leave real runs unable to see
their repository.

### Key Entities

- **Sandbox tier**: the ordered isolation class (host process, container, userspace kernel, microVM). Unchanged by this spec.
- **Sandbox backend**: where container-backed tiers run: a container daemon or a Kubernetes cluster.
- **Sandbox session**: one container or one pod bound to one run's checkout, removed when the run ends.
- **Run executor**: where the agent loop runs: inline, a container per run, or a Job per run.
- **Run unit**: the container or Job that executes one run's loop, identified by the run.
- **Run handoff**: the result a run process records for close-out, before any terminal state is set.
- **Close-out**: the control-plane step that delivers the change, writes terminal events and outcome, and writes back to the tracker.
- **Test services**: the disposable Postgres and Redis containers used by the local and CI test lanes.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a Docker host with shipped defaults, 100 percent of agent runs record a tier of `container` or stronger, and a declared command sees its repository files.
- **SC-002**: On a kind cluster with the pod backend, a run's commands execute in a sandbox pod and zero sandbox pods remain five minutes after the run ends.
- **SC-003**: Zero sandbox or run pod specifications, container create requests, images or labels in the test suites contain a credential value.
- **SC-004**: With a non-inline executor, the dispatch task for a run completes in under ten seconds regardless of how long the run takes.
- **SC-005**: For a scripted run, inline execution and per-run execution record identical status, steps, artifacts and event type sequence.
- **SC-006**: In fault tests (unit killed, unit lost, runtime unavailable), 100 percent of runs reach a terminal state with a reason, and zero run inline as a substitute.
- **SC-007**: On a machine with only Docker, two commands take a fresh checkout from no test services to a full suite run, with zero tests failing for a missing Postgres or Redis.
- **SC-008**: With the test services stopped, the suite reports zero connection errors; every affected test is skipped with a reason.

## Assumptions

- Runs driven by the Temporal worker (`apps/worker/scf_worker/temporal_main.py`) are out of scope. This spec covers the Celery task `forge.agent.run`.
- The delegated executor of ADR-0012 (third-party command-line agents inside the sandbox) is out of scope. "Executor" here means where SCF's own loop runs.
- The run process is SCF's own code and is trusted to the same degree as the worker. It is isolated to bound resources and to hold fewer credentials, not because it is treated as hostile. Agent commands are the untrusted part and stay in a sandbox session.
- The worker image is reused for run units with a different entry point. No new image is published.
- An operator or CRD, autoscaling policy and multi-cluster scheduling are out of scope.
- Tests that need a real daemon, kind, gVisor or Firecracker stay in the existing opt-in lanes. The default lane proves behaviour against fakes and reports the rest as skipped.
- Per-workspace, short-lived source-control credentials are delivered by spec 003 (R16). Until then the control plane's delivery step uses the credentials it uses today.
