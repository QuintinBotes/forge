# F19 — Container Sandboxing (Docker per-task isolation)

> Phase: v2 · Spec module(s): Technology Stack → *Sandbox (V2): Docker containers / Firecracker* + Execution Agent Runtime (`packages/agent-runtime/forge_agent`) + Security → *Sandbox isolation: Git worktrees (V1), Docker containers (V2) — no cross-task filesystem access* + Self-Hosting & Deployment (`deploy/`, hardening contract owned by `v1/F14`) · Status target: **Done** = the execution agent can run a task's verification/build/dependency commands inside a per-task, locked-down Docker container instead of as a host subprocess; selection is policy-/workspace-driven with `worktree` (V1) remaining the default and `container` strictly opt-in; arbitrary repo code (tests, build scripts, transitive deps) executes with **no network by default, dropped Linux capabilities, non-root uid, read-only root fs, and CPU/memory/PID limits**; each run's container mounts **only that run's worktree** (no cross-task filesystem access, verified); containers are created/exec'd/torn-down through a hardened `docker-socket-proxy` (the worker never touches the raw Docker socket); orphaned sandbox containers are reaped on a schedule and on worker startup; and the `worktree`/host-subprocess path is refactored behind the same `SandboxCommandRunner` seam so F06/F08 consumers are unchanged. Acceptance criteria 1–18 pass; the `packages/agent-runtime` sandbox suite (unit + `@pytest.mark.docker` integration) and the `deploy/` compose-contract additions are green (ruff + types + pytest).

---

## 1. Intent — what & why

In V1 (F06) the execution agent runs a task inside a **git worktree** and executes verification/build commands (`lint`, `type_check`, `test`, `coverage`, `install`) as **host subprocesses** via a `SandboxCommandRunner`. The spec is explicit that this is a convenience boundary, not a security boundary: *"Git worktrees are isolation, not a security boundary — subprocesses can still touch the worktree FS and (unless blocked) network"* (F06 §9), and the Technology Stack table marks **Sandbox (V2) = Docker containers / Firecracker — Stronger isolation for multi-tenant**, with the Security table requiring **"no cross-task filesystem access"**.

The fundamental risk this slice closes: **running untrusted code**. A task's test suite, build script, and (transitively) its third-party dependencies execute arbitrary code with whatever privileges the worker process has. On a multi-tenant self-hosted Forge that is unacceptable — one task's `pytest` could read another task's worktree, exfiltrate the BYOK model key from the worker's environment, or reach internal network services.

F19 introduces **per-task Docker container isolation** for command execution. The design keeps git operations (clone/worktree/commit/diff/push, which need GitHub App credentials that must *not* leak into the sandbox) on the host worker, and moves **only the execution of repo/agent commands** into a disposable, locked-down container whose filesystem is exactly the task's worktree mounted at `/workspace`. This is the smallest change that contains arbitrary code execution while preserving the existing agent loop, verification pipeline, and audit trail.

Design principles for this slice (derived from the spec's Security table and Production Docker Compose Requirements):
- **Opt-in, never silent downgrade.** Default stays `worktree` (V1). `container` is selected by workspace/policy. If `container` is requested but the daemon is unreachable, the run **fails loudly** — it must never silently fall back to host execution (that would defeat the isolation the operator asked for).
- **No raw Docker socket in the worker.** The worker reaches the daemon only through a `tecnativa/docker-socket-proxy` restricted to the minimal verbs. F14 today grants exactly one container the raw socket — the `autoheal` sidecar, read-only, root (its documented §8 exception); F19 introduces the socket-proxy as a strictly stronger pattern so the worker never holds the raw socket at all.
- **Least privilege by default.** No network, `cap_drop: ALL`, `no-new-privileges`, non-root uid, read-only root fs + tmpfs scratch, CPU/memory/PID limits, curated image allowlist.
- **Same seam.** Container execution implements the existing `SandboxCommandRunner` Protocol, so F06's `run_tests` tool and F08's `VerificationService` consume it unchanged.

## 2. User-facing behavior / journeys

F19 has no end-user GUI of its own; its "users" are (a) the **execution agent / workflow** (machine), (b) the **self-hosting operator** who enables and sizes container isolation, and (c) the **reviewer/auditor** who sees sandbox lifecycle events on the run trace (F10).

1. **Operator enables container isolation (workspace-wide).** The operator sets `FORGE_SANDBOX_KIND=container` (and the sandbox image/limits envs) in `.env.production`, ships the curated sandbox images, and restarts. From then on every agent run executes commands in a per-task container. The board/run UX is unchanged; the run trace now shows a `sandbox` lifecycle (image, limits, network mode) and per-command exec records.
2. **Per-repo opt-in / override.** A repo's `.forge/policy.yaml` `sandbox:` block selects `isolation: container`, a specific curated `image`, `network: none|egress`, and resource limits. A repo can request **stronger** isolation than the workspace minimum but never weaker (no downgrade).
3. **Happy path (machine).** Workflow reaches `task_ready → executing`. The agent (F06) creates the worktree on host, then F19 starts a sandbox container bound to that worktree. The agent writes code (host-side, path-confined) and runs `run_tests` → the command executes via `docker exec` inside the container with no network; results are parsed exactly as today; on terminal status the container is torn down and the worktree is committed/diffed on the host.
4. **Dependency install needing network.** A repo whose `install` (`uv sync`, `npm ci`) needs the registry sets `network: egress` with an `egress_allowlist`. The container is attached to a restricted egress network where the *only* reachable route is an allowlisting forward proxy; pulls from allow-listed hosts succeed, everything else is blocked.
5. **Resource abuse contained.** A runaway test forks a fork-bomb or allocates unbounded memory → PID/memory limits trip; the command returns a failed `CommandOutput` (`oom_killed=True` or nonzero), the agent treats it as a verification failure and retries/escalates — **the worker host is unaffected**.
6. **Crash recovery (operator-invisible).** A worker crashes mid-run leaving an orphan container. On the next worker boot and on a periodic beat, the reaper removes any `forge.sandbox=true` container whose run is terminal or older than the TTL.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One Alembic migration in `packages/db/forge_db/migrations/versions/xxxx_container_sandboxing.py`.

**Extends `agent_runs`** (base model created by `v1/F00-foundation-substrate`; runtime columns added by F06):
| Column | Type | Notes |
|---|---|---|
| `sandbox_kind` | enum `sandbox_kind` (`worktree`,`container`) default `worktree` | which provider ran this run |
| `sandbox_image` | `text` null | digest-pinned image used (null for `worktree`) |
| `sandbox_container_id` | `text` null | docker container id (null for `worktree`) |

**Creates `sandbox_instances`** (operational + audit; drives orphan reaping and run-trace lifecycle):
| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `agent_run_id` | uuid FK `agent_runs.id` ON DELETE CASCADE | |
| `workspace_id` | uuid FK `workspaces.id` | tenant scoping for reaper queries |
| `kind` | enum `sandbox_kind` | |
| `container_id` | `text` null | daemon container id |
| `container_name` | `text` null | `forge-sbx-<agent_run_id>` (unique handle for reaping) |
| `image` | `text` null | digest-pinned image |
| `network` | enum `sandbox_network` (`none`,`egress`) default `none` | |
| `status` | enum `sandbox_status` (`creating`,`running`,`exited`,`removed`,`failed`) | |
| `exit_reason` | `text` null | `completed`,`timeout`,`oom`,`teardown`,`reaped`,`startup_error` |
| `host_worktree_path` | `text` null | for cross-reference with `agent_runs.worktree_path` |
| `limits` | `jsonb` default `{}` | `{cpus, memory_mb, pids_limit, tmpfs_mb}` (snapshot for audit) |
| `created_at` / `removed_at` | `timestamptz` | reaper uses `created_at` + TTL |

Indexes: `ix_sandbox_instances_run (agent_run_id)`, `ix_sandbox_instances_status (status)`, `ux_sandbox_instances_container_name (container_name) UNIQUE WHERE container_name IS NOT NULL`.

Per-command execution remains audited as `agent_steps` rows (the existing F06 `tool_result`/`verification` step for `run_tests`), now carrying `output.sandbox = {kind, container_id, network, oom_killed, timed_out, duration_ms}`. No new per-command table.

### 3.2 Backend (FastAPI routes + services/packages)

No new business routes. The existing `GET /api/v1/agent-runs/{id}` (F06) response gains a `sandbox` block (kind, image, network, limits, status) read from `sandbox_instances`, consumed by the **Run Trace Viewer (F10)**. The `AgentRunRead` schema in `apps/api/forge_api/schemas/agent.py` adds an optional `sandbox: SandboxInstanceRead | None` field. That is the only API surface change.

The **policy schema** gains a `sandbox:` block. The schema is owned by `policy-sdk` (`v1/F04-repo-policy`), whose `Policy` model lives in `packages/contracts/forge_contracts/policy.py` (re-exported by `forge_policy.schema`); F19 adds `PolicySandboxBlock` there plus the `Policy.sandbox: PolicySandboxBlock | None` field and validation (see §4) and the merge/precedence rule `resolve_sandbox_settings(workspace_default, policy_block)` (in `forge_agent.sandbox.selection`).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

This is the core of the slice. Package: **`packages/agent-runtime/forge_agent/sandbox/`** (F06's single-module `sandbox.py` becomes a package; `WorktreeSandbox`/`WorktreeHandle` move to `sandbox/worktree.py` unchanged).

New/changed modules:
- `sandbox/base.py` — `SandboxProvider`, `SandboxSession` Protocols; `CommandOutput` extensions; sandbox errors.
- `sandbox/local.py` — `LocalSandboxProvider` / `LocalSandboxSession`: **the F06 host-subprocess `SandboxCommandRunner` refactored into the new Protocol** (behavior-preserving; this is the `worktree` default).
- `sandbox/container.py` — `ContainerSandboxProvider` / `ContainerSandboxSession`: per-task Docker container via the Docker Engine API (`docker` SDK pointed at `DOCKER_HOST=tcp://docker-proxy:2375`).
- `sandbox/images.py` — `resolve_image(language, policy_block, settings)` + image **allowlist** enforcement.
- `sandbox/selection.py` — `resolve_sandbox_kind/settings(...)` precedence (workspace minimum vs policy request; never downgrade).
- `sandbox/reaper.py` — `reap_orphans(provider, db)` used by the Celery beat + startup hook.
- `sandbox/factory.py` — `build_sandbox_provider(settings) -> SandboxProvider` (returns local or container provider).

**Integration into F06's loop** (the named refactor F19 owns):
- `RuntimeDeps` (F06) gains `sandbox_provider: SandboxProvider` (built by `build_sandbox_provider`).
- `nodes.load_context` (F06): after `WorktreeSandbox.create(...)` → `session = await deps.sandbox_provider.create(SandboxSpec(...))`; the `session` (which *is* a `SandboxCommandRunner`) is placed on `ToolContext.command_runner` and `ToolContext.workspace_dir`.
- `tools/run_tests.py` (F06): instead of constructing a host subprocess runner, calls `ctx.command_runner.run(...)` (the session). `PolicyGuard.check_command` (F06) still gates: only literal `policy.commands` strings are ever passed; **no model-authored shell**.
- `nodes.finalize` / cleanup (F06): `await session.teardown(reason=...)` is called **before** `WorktreeSandbox.cleanup(...)` (git diff/commit happen on host while the worktree still exists; teardown only kills the container, not the worktree).
- F08's authoritative gate (the `run_checks` effect, owned by `v1/F08-plan-execute-verify-pr-approval`) **materialises its own fresh verify worktree** off the agent's branch via `WorktreeSandbox.create` precisely so the gate cannot be self-certified by the agent (F08 §3.3, "verification independence"). F19 preserves that independence: when container isolation is active, `run_checks` obtains a **separate** `SandboxSession` from the **same** `SandboxProvider` (a new `SandboxSpec` bound to the verify worktree), so the gate runs under identical container hardening but **never reuses the agent's session/container**. F08's `WorktreeCommandRunner(SandboxCommandRunner)` is constructed from that session; the seam shape F08 consumes is unchanged.

**Celery tasks** in `apps/worker/forge_worker/tasks/sandbox.py`:
```python
@celery_app.task(name="sandbox.reap_orphans")
def reap_orphans_task() -> dict: ...   # beat: every FORGE_SANDBOX_REAP_INTERVAL_SECONDS
```
plus a worker `@worker_ready` signal hook that runs one reap pass on boot. Beat schedule entry added in `forge_worker.beat`.

`AgentSettings` (F06) is extended with a `sandbox: SandboxSettings` sub-model (§4).

### 3.4 Frontend / UI (Next.js routes/components)

Minimal. The **Run Trace Viewer (F10)** gains a small **Sandbox** panel rendering the `sandbox` block from `GET /agent-runs/{id}` (kind badge `worktree`/`container`, image digest short form, network mode, CPU/mem/PID limits, status, exit reason). Component `apps/web/components/runs/SandboxPanel.tsx`; wired into the existing run-trace detail. No new routes, no state machine. (If F10 is not yet built, this is a contract addition consumed later — mark the panel as the only UI deliverable.)

### 3.5 Infra / deploy (compose, helm, caddy)

Extends the F14 compose substrate (`deploy/docker-compose.yml`); all additions satisfy F14's hardening contract (digest-pinned, healthcheck, limits, capped logs, non-root where possible, no published ports, autoheal label).

**New services:**
- `docker-proxy` — `tecnativa/docker-socket-proxy@sha256:…`. Mounts `/var/run/docker.sock:ro`. Env enables **only** `CONTAINERS=1`, `IMAGES=1`, `POST=1`, `EXEC=1`, `INFO=1`; everything else `0` (no `NETWORKS` create from worker, no `VOLUMES` delete, no swarm). On the `sandbox_ctl` network only. The **only** component permitted near the socket (root, documented exception like `autoheal`).
- `sandbox-proxy` — an allowlisting forward proxy (`ubuntu/squid@sha256:…` or `tinyproxy`) for `network: egress` mode. Dual-homed: `sandbox_egress` (internal) + a network with default egress. Config `deploy/sandbox/squid.conf` with a domain `acl` allowlist (PyPI, npm, etc.) sourced from `FORGE_SANDBOX_EGRESS_ALLOWLIST`. Non-root.

**Changed:**
- `worker` service: joins `sandbox_ctl` (to reach `docker-proxy:2375`); env `DOCKER_HOST=tcp://docker-proxy:2375`, `FORGE_SANDBOX_*` (see §4). Worker still has **no** `/var/run/docker.sock` mount (asserted by compose contract).
- The `forge_repos`/worktree storage must be reachable by **sibling** sandbox containers. Sandbox containers mount **only the run's subpath** of the worktree named volume: `type=volume, source=${FORGE_WORKTREE_VOLUME}, target=/workspace, volume-subpath=<run-relative-path>`. This requires **Docker Engine ≥ 26.1** (`volume-subpath`); `deploy/scripts/preflight.sh` adds an engine-version check. (Fallback for older engines documented in §9: a per-run named volume.)

**New networks** (both `internal: true`):
- `sandbox_ctl` — worker ↔ docker-proxy only.
- `sandbox_egress` — sandbox containers ↔ sandbox-proxy only (the proxy bridges out).

**Sandbox images** (curated, digest-pinned, shipped by Forge): `deploy/sandbox/base.Dockerfile` (non-root uid/gid 10001, `git`, `ca-certificates`, `tini`, `coreutils` for `timeout`, no Docker client, minimal), and language images `python.Dockerfile` (uv + cpython), `node.Dockerfile`, `go.Dockerfile` built FROM base. Pinned in env as `FORGE_SANDBOX_IMAGE_PYTHON` etc. and the workspace allowlist `FORGE_SANDBOX_ALLOWED_IMAGES`.

**Helm:** N/A — Kubernetes/Helm is V2 and tracked separately; the K8s analogue (a per-task `Job`/Pod with a restricted `PodSecurityContext` + `NetworkPolicy`) is noted in §12 as future, not built here.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

> New types marked **(contracts)** are frozen in `packages/contracts/forge_contracts/sandbox.py`. `CommandOutput`/`SandboxCommandRunner` already live in `packages/contracts/forge_contracts/agent.py` (defined by F06); F19 **extends `CommandOutput` in place there** (additive, backward-compatible) and leaves the `SandboxCommandRunner` signature byte-for-byte identical.

**Existing seam (F06/F08), extended by F19 (contracts):**
```python
class CommandOutput(BaseModel):
    exit_code: int
    stdout: str                      # capped at FORGE_SANDBOX_OUTPUT_CAP_BYTES (default 256 KiB)
    stderr: str
    duration_ms: int
    timed_out: bool = False
    # --- added by F19 ---
    oom_killed: bool = False
    stdout_artifact_ref: str | None = None   # MinIO ref when stdout exceeds cap
    stderr_artifact_ref: str | None = None
    sandbox_kind: "SandboxKind" = "worktree"
    container_id: str | None = None

class SandboxCommandRunner(Protocol):                 # IDENTICAL shape to F06/F08; both providers implement
    async def run(
        self, command: str, *,                        # exact allowlisted policy.commands string
        cwd: str,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandOutput: ...                            # runs `command` via `sh -lc`; never model-authored
# Network is fixed per-session at create() time (SandboxSpec.network), NOT per run() call —
# so the seam signature stays byte-for-byte identical to F06/F08 and F08's
# WorktreeCommandRunner remains source-compatible.
```

**New sandbox contracts:**
```python
class SandboxKind(str, Enum):
    worktree = "worktree"            # V1: host subprocess (LocalSandbox)
    container = "container"          # V2: per-task Docker container

class SandboxNetwork(str, Enum):
    none = "none"                    # no egress (default)
    egress = "egress"                # via allowlisting proxy only

class SandboxResourceLimits(BaseModel):
    cpus: float = 2.0
    memory_mb: int = 4096
    pids_limit: int = 512
    tmpfs_mb: int = 1024             # size of /tmp tmpfs

class SandboxSpec(BaseModel):
    agent_run_id: UUID
    workspace_id: UUID
    kind: SandboxKind
    host_worktree_path: str          # absolute host path of the worktree (WorktreeHandle.worktree_path)
    worktree_volume: str             # named volume backing worktrees
    worktree_subpath: str            # subpath within the volume == this run's worktree dir
    image: str | None = None         # digest-pinned; required for kind=container, must be allow-listed
    network: SandboxNetwork = SandboxNetwork.none
    egress_allowlist: list[str] = []
    limits: SandboxResourceLimits = SandboxResourceLimits()
    env: dict[str, str] = {}         # non-secret env injected into the container
    setup_commands: list[str] = []   # run once at session start (e.g. [policy.commands.install])
    exec_timeout_seconds: int = 1800
    run_as_uid: int = 10001
    run_as_gid: int = 10001

class SandboxSession(SandboxCommandRunner, Protocol):
    sandbox_id: str                  # container id, or local pseudo-id
    kind: SandboxKind
    workspace_dir: str               # worktree path *inside* the sandbox: "/workspace" (container) or host path (local)
    host_worktree_path: str
    async def setup(self) -> None: ...                    # run setup_commands; idempotent
    async def teardown(self, *, reason: str = "completed") -> None: ...
    # `run(...)` inherited from SandboxCommandRunner

class SandboxProvider(Protocol):
    kind: SandboxKind
    async def create(self, spec: SandboxSpec) -> SandboxSession: ...   # builds + starts session
    async def reap_orphans(self) -> int: ...                           # returns count removed

# Errors (forge_agent.sandbox.base):
class SandboxError(Exception): ...
class SandboxStartupError(SandboxError): ...      # daemon/proxy unreachable, create failed
class SandboxImageNotAllowed(SandboxError): ...   # image not on FORGE_SANDBOX_ALLOWED_IMAGES
class SandboxExecError(SandboxError): ...
```

**API read DTO (`packages/contracts/forge_contracts/sandbox.py`; surfaced on `AgentRunRead.sandbox`):**
```python
class SandboxInstanceRead(BaseModel):
    kind: SandboxKind
    image: str | None                # digest-pinned image (null for worktree)
    network: SandboxNetwork
    status: str                      # creating | running | exited | removed | failed
    exit_reason: str | None
    limits: SandboxResourceLimits
    container_id: str | None
    created_at: datetime
    removed_at: datetime | None
```

**Selection / precedence (`forge_agent.sandbox.selection`):**
```python
def resolve_sandbox_kind(workspace_min: SandboxKind, policy_request: SandboxKind | None) -> SandboxKind:
    """Returns the STRONGER of the two. container > worktree. A policy may strengthen
    but never weaken below the workspace minimum (no downgrade)."""

def resolve_sandbox_settings(settings: "SandboxSettings", policy_block: "PolicySandboxBlock | None",
                             *, language: str) -> SandboxSpec: ...
```

**Settings (`forge_agent.sandbox.settings.SandboxSettings`, env-bound; sub-model of `AgentSettings`):**
| Env | Default | Notes |
|---|---|---|
| `FORGE_SANDBOX_KIND` | `worktree` | workspace **minimum** isolation |
| `FORGE_SANDBOX_DOCKER_HOST` | `tcp://docker-proxy:2375` | never the raw socket |
| `FORGE_SANDBOX_IMAGE_PYTHON` / `_NODE` / `_GO` | digest pins | default image per language |
| `FORGE_SANDBOX_ALLOWED_IMAGES` | the three above | comma list; `policy.sandbox.image` must be a member |
| `FORGE_SANDBOX_WORKTREE_VOLUME` | `forge_repos` | named volume for `volume-subpath` mount |
| `FORGE_SANDBOX_CPUS` / `_MEMORY_MB` / `_PIDS_LIMIT` / `_TMPFS_MB` | `2.0` / `4096` / `512` / `1024` | default limits |
| `FORGE_SANDBOX_NETWORK` | `none` | default network mode |
| `FORGE_SANDBOX_EGRESS_NETWORK` | `forge_sandbox_egress` | network name for egress mode |
| `FORGE_SANDBOX_EGRESS_ALLOWLIST` | `pypi.org,files.pythonhosted.org,registry.npmjs.org` | proxy allowlist |
| `FORGE_SANDBOX_EXEC_TIMEOUT_SECONDS` | `1800` | per-command ceiling |
| `FORGE_SANDBOX_OUTPUT_CAP_BYTES` | `262144` | stdout/stderr cap before artifact offload |
| `FORGE_SANDBOX_RUN_UID` / `_GID` | `10001` / `10001` | must match worktree ownership |
| `FORGE_SANDBOX_REAP_INTERVAL_SECONDS` | `300` | beat cadence |
| `FORGE_SANDBOX_MAX_TTL_SECONDS` | `21600` | reaper hard cap (6h) |

**policy.yaml `sandbox:` block (`PolicySandboxBlock`, owned by F04, modeled here):**
```yaml
sandbox:
  isolation: container          # worktree | container (request; cannot go below workspace minimum)
  image: ghcr.io/forge-platform/forge-sandbox-python@sha256:…   # optional; must be allow-listed
  network: none                 # none | egress
  egress_allowlist: [pypi.org, files.pythonhosted.org]          # only meaningful when network: egress
  cpus: 2
  memory: 4g
  pids_limit: 512
  exec_timeout_seconds: 1800
  setup_commands: [uv sync]     # defaults to [commands.install] from the policy
```

**Container create (the concrete `ContainerSandboxProvider.create` daemon call, for buildability):**
```python
container = client.containers.create(
    image=spec.image,
    command=["tini", "--", "sleep", "infinity"],     # long-lived; commands run via exec
    name=f"forge-sbx-{spec.agent_run_id}",
    labels={"forge.sandbox": "true",
            "forge.agent_run_id": str(spec.agent_run_id),
            "forge.workspace_id": str(spec.workspace_id)},
    user=f"{spec.run_as_uid}:{spec.run_as_gid}",
    working_dir="/workspace",
    mounts=[Mount(target="/workspace", source=spec.worktree_volume, type="volume",
                  read_only=False, volume_options={"subpath": spec.worktree_subpath})],
    network_mode="none" if spec.network is SandboxNetwork.none else None,   # egress => attach egress net post-create
    read_only=True,                                  # read-only root fs
    tmpfs={"/tmp": f"size={spec.limits.tmpfs_mb}m", "/home/forge": "size=64m"},
    mem_limit=f"{spec.limits.memory_mb}m",
    nano_cpus=int(spec.limits.cpus * 1_000_000_000),
    pids_limit=spec.limits.pids_limit,
    cap_drop=["ALL"],
    security_opt=["no-new-privileges:true"],
    environment=spec.env,                            # NEVER the BYOK key / git creds
    auto_remove=False,                               # explicit teardown for audit; reaper GCs orphans
)
```
`run(command)` executes `["/bin/sh","-lc", f"timeout --kill-after=10s {t}s sh -lc {shlex.quote(command)}"]` via `exec_create`/`exec_start` (container-side `timeout` → deterministic exit 124 on timeout); `oom_killed` read from `container.attrs["State"]["OOMKilled"]` after exec; output streamed and capped at `FORGE_SANDBOX_OUTPUT_CAP_BYTES` with overflow pushed to MinIO via `deps.object_store`.

## 5. Dependencies — features/slices that must exist first

| Ref | Why F19 needs it | Hard/Soft |
|---|---|---|
| `v1/F06-single-execution-agent` | Owns `WorktreeSandbox`, the `SandboxCommandRunner` seam, `run_tests` tool, `RuntimeDeps`, `ToolContext`, `AgentSettings`, `agent_runs` runtime columns. F19 refactors/extends these. | **Hard** |
| `v1/F08-plan-execute-verify-pr-approval` | Owns the authoritative `run_checks` effect + `VerificationService` + `WorktreeCommandRunner(SandboxCommandRunner)`. F19 lets `run_checks` build a **separate** container session (fresh verify worktree) from the same provider, preserving F08's gate independence. | **Hard** (peer; F19 must also work without F08 for its own tests) |
| `v1/F04-repo-policy` | `policy.commands` (the only allowlisted commands), `PolicyGuard.check_command`, and the `Policy` model in `forge_contracts/policy.py` that F19 adds `PolicySandboxBlock` / `Policy.sandbox` to. | **Hard** |
| `v1/F14-docker-compose-selfhost` | The compose substrate + hardening contract + documented raw-socket exception (`autoheal`, §8) + `preflight.sh` + the `forge_repos` named worktree volume that F19 extends with `docker-proxy`, `sandbox-proxy`, networks, sandbox images, engine-version check. | **Hard** |
| `v1/F00-foundation-substrate` | `agent_runs` base model, `forge_contracts` package, `object_store` (MinIO) for output offload, Celery app + beat substrate (`forge_worker.app` / `beat.py`). | **Hard** |
| `cross-cutting/F37-auth-secrets-byok` | BYOK vault resolver (the model key F19 must keep out of the sandbox) + the canonical `SecretRedactor` F06's `redaction.py` wraps and F19 reuses for `sandbox_instances` / `agent_steps` / audit metadata. | **Hard** |
| `cross-cutting/F39-audit-log` | Central, immutable `audit_log` + `AuditEvent` / `AuditSink` (`deps.audit`); F19 emits a sandbox-lifecycle `AuditEvent` per create / exec / teardown / reap, satisfying the audit-log non-negotiable. | **Hard** |
| `v1/F11-skill-profiles` | `verification_steps`/coverage drive which commands run; unchanged, but F19's container must reproduce identical verification. | **Soft** |
| `v1/F10-run-trace-viewer` | Renders the new `sandbox` block; F19 only adds the panel + API field. | **Soft** (contract addition usable later) |

External libs: `docker` (Python SDK) pinned; `tini` + language toolchains baked into sandbox images. Docker **Engine ≥ 26.1** on the host (for `volume-subpath`).

## 6. Acceptance criteria (numbered, testable)

1. **Selection precedence / no downgrade.** `resolve_sandbox_kind(container, worktree) == container` and `resolve_sandbox_kind(worktree, container) == container`; a policy requesting `worktree` when the workspace minimum is `container` still resolves to `container`. Default (both unset) → `worktree`. *(unit)*
2. **Local provider parity.** With `kind=worktree`, `LocalSandboxProvider` reproduces F06 host-subprocess behavior: F06's existing `run_tests`/verification integration tests pass unchanged after the refactor (no behavior change for the V1 path). *(unit + reuse F06 suite)*
3. **Container hardening.** `ContainerSandboxProvider.create` produces a container whose inspect shows: `User=10001:10001`, `ReadonlyRootfs=true`, `CapDrop=[ALL]`, `SecurityOpt` contains `no-new-privileges`, `Memory`/`NanoCpus`/`PidsLimit` set, and labels `forge.sandbox=true` + `forge.agent_run_id`. *(integration, `@pytest.mark.docker`)*
4. **Single-worktree mount (no cross-task FS access).** The container's `Mounts` contains exactly one entry targeting `/workspace` (the run's worktree subpath) and nothing else; a file written into run A's worktree is **not** visible inside run B's container. *(integration)*
5. **No network by default.** With `network=none`, a command performing DNS/outbound (`getent hosts pypi.org` / `curl`) fails inside the container (nonzero exit / no route). *(integration)*
6. **Egress allowlist.** With `network=egress`, a fetch from an allow-listed host through the proxy succeeds; a fetch from a non-allow-listed host is blocked by `sandbox-proxy`; a direct (proxy-bypassing) connection fails (internal egress network has no other route). *(integration)*
7. **Command allowlist preserved.** Only literal `policy.commands` strings reach `session.run`; `run_tests` with a `command_key` absent from `policy.commands` is rejected by `PolicyGuard.check_command` and no exec occurs; no model-authored shell is ever executed. *(unit)*
8. **Timeout.** A command exceeding `exec_timeout_seconds` is killed; `CommandOutput.timed_out=True`, `exit_code==124`; the session stays usable for a subsequent `run`. *(integration)*
9. **OOM containment.** A command exceeding the memory limit yields `CommandOutput.oom_killed=True` and a failed result; the worker process does not crash and the next run proceeds. *(integration)*
10. **Output capping + offload.** `stdout` larger than `FORGE_SANDBOX_OUTPUT_CAP_BYTES` is truncated in `CommandOutput.stdout` and the full output is stored in MinIO and referenced by `stdout_artifact_ref`. *(unit with fake object store + integration)*
11. **Teardown.** On terminal status, `session.teardown()` removes the container (inspect → `NotFound`) and sets `sandbox_instances.status=removed` with `exit_reason`; the host worktree still exists at teardown time so F06's `commit_all`/`diff_stat` succeed (ordering: teardown before `WorktreeSandbox.cleanup`). *(integration)*
12. **Orphan reaping.** A `forge.sandbox=true` container whose `agent_run` is terminal, or older than `FORGE_SANDBOX_MAX_TTL_SECONDS`, is removed by `reap_orphans()` (returns the count); the worker `worker_ready` hook runs one pass on boot. *(integration)*
13. **uid permission round-trip.** A file written by the container (e.g. `coverage.xml` from `pytest`) is read back by the host worker (uid 10001 match) so F06/F08 coverage parsing works on container-produced output. *(integration)*
14. **Image allowlist.** A `policy.sandbox.image` not in `FORGE_SANDBOX_ALLOWED_IMAGES` raises `SandboxImageNotAllowed` at create; no container is started. *(unit)*
15. **No raw socket in worker.** Compose contract: the `worker` service has no `/var/run/docker.sock` mount; `docker-proxy` is the only service mounting it (read-only) and exposes only `CONTAINERS/IMAGES/POST/EXEC/INFO`; `worker.DOCKER_HOST=tcp://docker-proxy:2375`. *(contract, extends F14 suite)*
16. **Audit + redaction.** Each create/exec/teardown/timeout/oom produces a `sandbox_instances` row, an `agent_steps` event, **and** a central `audit_log` `AuditEvent` emitted via `deps.audit` (`cross-cutting/F39-audit-log`); the BYOK model key and git credentials never appear in `spec.env`, `agent_steps`, `sandbox_instances`, the `AuditEvent.metadata`, or logs — verified after passing through F37's `SecretRedactor` (assert against serialized records + captured log buffer). *(unit + integration)*
17. **Compose/network contract.** `docker-proxy` and `sandbox-proxy` satisfy F14's hardening contract (digest-pinned, healthcheck, limits, capped logs, no published ports, autoheal label); `sandbox_ctl` and `sandbox_egress` networks are `internal: true`; `preflight.sh` fails on Docker Engine < 26.1. *(contract)*
18. **No silent downgrade on failure.** When `kind=container` is resolved but the daemon/proxy is unreachable, `create` raises `SandboxStartupError`; the agent run finalizes `failed`/`awaiting_input` with `needs_human_reason` naming the sandbox failure — execution **never** falls back to a host subprocess. *(unit with unreachable docker host)*

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Layout under `packages/agent-runtime/tests/sandbox/`. Two tiers: **unit** (fake Docker client / pure logic, always in CI) and **integration** (`@pytest.mark.docker`, real daemon, in the Docker-enabled CI job). Compose-contract additions live in `deploy/tests/` (extending F14's suite). Write tests first; drive each AC red→green.

**Key fixtures (`conftest.py`):**
- `fake_docker_client` — records `containers.create/get/remove`, `api.exec_create/exec_start/exec_inspect`, returns scripted inspect dicts (`State.OOMKilled`, `ExitCode`, `Mounts`, `HostConfig`). Lets every container code path run without a daemon.
- `real_docker` (`@pytest.mark.docker`) — a live `docker` client (skipif daemon/engine<26.1 absent); auto-removes any `forge.sandbox=true` container in teardown to avoid leakage between tests.
- `tmp_worktree_volume` — creates a named volume + seeds two run subpaths (`runA/`, `runB/`) for cross-task isolation tests.
- `policy_with_sandbox` — `PolicySnapshot` carrying a `sandbox:` block (image, network, limits, setup_commands).
- `fake_object_store` — captures artifact offloads to assert capping/refs.
- `redacting_log_capture` — reused from F06 to assert secret redaction.
- `scripted_runner` — a `SandboxCommandRunner` fake mapping command substrings to `CommandOutput` (parity tests against F08's `fake_runner`).

**Unit tests:**
- `test_selection.py` — `resolve_sandbox_kind` precedence matrix; `resolve_sandbox_settings` merges policy over workspace defaults; downgrade rejected (AC1).
- `test_images.py` — `resolve_image` picks per-language default; `policy.sandbox.image` honored when allow-listed; non-allow-listed → `SandboxImageNotAllowed` (AC14).
- `test_local_provider.py` — `LocalSandboxProvider.run` parity: exit codes, stdout/stderr, timeout via host `asyncio` ceiling, env passthrough; `workspace_dir == host_worktree_path` (AC2).
- `test_container_create_args.py` — against `fake_docker_client`, assert `create` kwargs: user, read_only, cap_drop ALL, no-new-privileges, mem/nano_cpus/pids, single `/workspace` volume-subpath mount, labels, `network_mode=none` (AC3,4).
- `test_run_command_shape.py` — `run` wraps command as `timeout --kill-after … sh -lc <quoted>`; only the literal command string is interpolated (no extra args); exit 124 → `timed_out=True` (AC7,8).
- `test_output_capping.py` — stdout > cap → truncated + `stdout_artifact_ref` set via `fake_object_store` (AC10).
- `test_redaction.py` — `SandboxSpec.env`, exec env, and persisted records never contain the BYOK key / git creds (AC16).
- `test_no_silent_downgrade.py` — `ContainerSandboxProvider.create` against an unreachable `DOCKER_HOST` raises `SandboxStartupError`; runtime maps it to a terminal run with reason, not a `LocalSandbox` fallback (AC18).
- `test_reaper_logic.py` — given scripted container list + `sandbox_instances`/`agent_runs` rows, `reap_orphans` selects terminal/TTL-exceeded containers only and returns the count (AC12).

**Integration tests (`@pytest.mark.docker`, real daemon):**
- `test_container_inspect_hardening.py` — create against `real_docker`; inspect asserts hardening matrix (AC3).
- `test_cross_task_isolation.py` — two sessions on `runA`/`runB`; file in A not visible in B; `Mounts` has exactly `/workspace` (AC4).
- `test_network_none.py` — `getent hosts pypi.org` fails with `network=none` (AC5).
- `test_network_egress_allowlist.py` — with `sandbox-proxy` up: allow-listed host OK, non-allow-listed blocked, direct connect fails (AC6).
- `test_timeout.py` / `test_oom.py` — sleeping/allocating commands → `timed_out`/`oom_killed`; session/worker survive (AC8,9).
- `test_uid_roundtrip.py` — container writes `coverage.xml`; host reads it; F06 coverage parser yields a value (AC13).
- `test_teardown_then_git.py` — teardown removes container; worktree still present; `WorktreeSandbox.commit_all` succeeds; `sandbox_instances.status=removed` (AC11).
- `test_reap_orphans_real.py` — leave an orphan container; `reap_orphans()` removes it (AC12).
- `test_graph_with_container.py` — F06 graph end-to-end with `kind=container` + `ScriptedModelClient`: write_code (host) → run_tests (container, passes) → finalize succeeded; assert exec ran in the container (AC2-parity at the loop level).

**Compose-contract additions (`deploy/tests/test_compose_contract.py`, extends F14):**
- `test_worker_has_no_docker_socket` (AC15), `test_docker_proxy_minimal_verbs` (AC15), `test_sandbox_networks_internal` (AC17), `test_sandbox_proxy_hardened` (AC17), `test_preflight_engine_version` (AC17 — `preflight.sh` rejects engine < 26.1 via a stubbed `docker version`).

**Coverage gate:** F19 is built under `backend-tdd` — ≥80% line coverage on `forge_agent.sandbox`, ruff + types green, before "done". Real-daemon tests are required at release (the Docker-gated CI job), unit tests on every PR.

## 8. Security & policy considerations

- **Containment of arbitrary code is the whole point.** All untrusted execution (repo tests, build scripts, transitive deps) runs inside the per-task container with `cap_drop: ALL`, `no-new-privileges`, non-root uid, read-only root fs, tmpfs scratch, and CPU/memory/PID limits — satisfying the spec's "Docker containers (V2) — no cross-task filesystem access" and "Stronger isolation for multi-tenant."
- **No cross-task filesystem access (verified).** Each container mounts **only** its run's worktree subpath at `/workspace` (AC4). Other tasks' worktrees, the worker's filesystem, and host paths are never mounted. The agent's own file tools (`read_repo`/`write_code`) remain path-confined to the worktree by F06's `PolicyGuard` (they execute no arbitrary code), so keeping them host-side does not widen the blast radius.
- **No network by default.** `network=none` means untrusted code cannot exfiltrate data or reach internal services. `egress` mode routes the container's only path to the internet through an allowlisting forward proxy (`HTTP(S)_PROXY` env + an `internal: true` egress network with no other route), with a domain allowlist — preventing arbitrary egress even when dependency installation needs the network.
- **Docker socket is never exposed to the worker.** The worker reaches the daemon only via `docker-socket-proxy` restricted to `CONTAINERS/IMAGES/POST/EXEC/INFO`; it cannot delete volumes, manage networks, or run privileged/host-mounting containers. The socket-mounting proxy is the single privileged surface (documented like F14's `autoheal` exception), pinned and on an isolated `internal` control network.
- **Curated image allowlist.** The sandbox image must be a member of `FORGE_SANDBOX_ALLOWED_IMAGES` (AC14). A repo cannot point the sandbox at an arbitrary/malicious image, and the agent never builds arbitrary Dockerfiles via the daemon (that would be host RCE) — `commands.build` runs *inside* the existing sandbox, not against the host daemon.
- **Secrets never enter the sandbox.** The BYOK model key (resolved in-memory on the worker, F06) and GitHub App credentials (used only for host-side git/PR ops) are **never** placed in `SandboxSpec.env`, exec env, or any persisted record; redaction (F06's `redaction.py`) covers `sandbox_instances` and the new `agent_steps.output.sandbox` block (AC16).
- **No silent isolation downgrade.** If `container` is requested and the daemon/proxy is down, the run fails loudly (AC18) — Forge never quietly executes untrusted code on the host when the operator asked for containment.
- **Audit.** Two layers, mirroring F06: (1) `sandbox_instances` (append-mostly; only `status`/`removed_at`/`exit_reason` mutate) plus the per-exec `agent_steps` events give the operational + run-trace record; (2) every security-relevant lifecycle transition (create / exec / teardown / timeout / oom / reap) is also emitted as an `AuditEvent` through `deps.audit` into the central, tamper-evident `audit_log` (`cross-cutting/F39-audit-log`), satisfying the non-negotiable "audit log for every agent action / tool call — immutable, queryable". `AuditEvent.metadata` passes through F37's `SecretRedactor` before persistence.
- **Policy precedence.** Workspace `FORGE_SANDBOX_KIND` is the *minimum* isolation; per-repo policy may strengthen but never weaken it (`resolve_sandbox_kind`), so a compromised/careless repo policy cannot opt out of containment an operator mandated.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** The provider abstraction + the F06/F08 refactor behind the shared seam, a correct and hardened Docker provider (mounts, limits, exec-with-timeout, oom/exit semantics, output capping), the egress proxy + network segmentation, the reaper, the new compose services + sandbox images + preflight, and a real-daemon integration suite each carry weight. Rough split: provider abstraction + local refactor (M), container provider + exec semantics (M), networks/proxies/images/compose (M), reaper + audit + selection + settings (S), integration test harness (M).

Key risks:
- **Docker-out-of-Docker path/volume translation.** Sibling containers mount via `volume-subpath` on the host daemon, requiring Engine ≥ 26.1. *Mitigation:* preflight version gate; documented fallback of a per-run named volume (create/destroy per run) for older engines; never bind-mount a translated host path (keeps F14's "named volumes only" rule and per-task isolation).
- **`docker exec` has no native timeout.** *Mitigation:* run via container-side `timeout --kill-after` (deterministic exit 124), plus an outer `asyncio` wall-clock guard that `docker kill`s the exec/container if `timeout` itself wedges.
- **uid/permission mismatches between host worker and container.** Files written in-container must be readable by the worker for coverage/diff. *Mitigation:* pin both to uid/gid 10001; worktree dirs created 10001-owned; AC13 guards it; sandbox base image's `forge` user fixed at 10001.
- **Socket-proxy blast radius.** Even minimal verbs allow container create/exec. *Mitigation:* `internal` control network, read-only socket, no `PRIVILEGED`/`NETWORKS`/`VOLUMES`-delete verbs, label-scoped reaping, pinned digest; documented in `security.md`; optional Sysbox/gVisor runtime called out as future hardening.
- **Performance overhead per task.** Container start + dep install adds latency vs a host subprocess. *Mitigation:* long-lived session per run (one container, many `exec`s), `setup_commands` run once, base images pre-pulled; container kind stays opt-in so latency-sensitive single-tenant deployments keep V1 worktrees.
- **Egress allowlisting correctness.** A too-broad proxy allowlist re-opens exfiltration. *Mitigation:* default `none`; egress requires explicit per-repo allowlist; squid `acl` is domain-exact; tested by AC6.
- **CI Docker availability/flakiness.** Real-daemon tests are slow and need a privileged runner. *Mitigation:* unit tier (fake client) gates every PR; Docker tier required only at release, with per-test orphan cleanup.

## 10. Key files / paths (exact)

```
packages/agent-runtime/forge_agent/sandbox/
├── __init__.py
├── base.py            # SandboxProvider, SandboxSession Protocols; errors; CommandOutput re-export
├── worktree.py        # WorktreeSandbox, WorktreeHandle (MOVED from F06 sandbox.py, unchanged)
├── local.py           # LocalSandboxProvider / LocalSandboxSession (F06 host-subprocess refactor)
├── container.py       # ContainerSandboxProvider / ContainerSandboxSession (docker SDK)
├── images.py          # resolve_image() + allowlist enforcement (SandboxImageNotAllowed)
├── selection.py       # resolve_sandbox_kind / resolve_sandbox_settings
├── settings.py        # SandboxSettings (env-bound; nested in AgentSettings)
├── reaper.py          # reap_orphans(provider, db)
└── factory.py         # build_sandbox_provider(settings) -> SandboxProvider

packages/agent-runtime/forge_agent/nodes.py            # load_context: create session; finalize: teardown (EDIT, F06)
packages/agent-runtime/forge_agent/tools/run_tests.py  # call ctx.command_runner.run (EDIT, F06)
packages/agent-runtime/forge_agent/runtime.py          # RuntimeDeps += sandbox_provider; ToolContext += command_runner/workspace_dir (EDIT, F06)
packages/agent-runtime/tests/sandbox/                  # unit + @pytest.mark.docker integration (see §7)

packages/contracts/forge_contracts/sandbox.py          # SandboxKind/Network/Spec/Session/Provider, errors; CommandOutput ext
packages/contracts/forge_contracts/policy.py           # + PolicySandboxBlock + Policy.sandbox field (EDIT, F04; re-exported via forge_policy.schema)

apps/worker/forge_worker/tasks/sandbox.py              # reap_orphans_task + worker_ready hook
apps/worker/forge_worker/beat.py                       # beat schedule: sandbox.reap_orphans (EDIT)
apps/api/forge_api/schemas/agent.py                    # AgentRunRead.sandbox: SandboxInstanceRead | None (EDIT, F06)
apps/web/components/runs/SandboxPanel.tsx              # F10 run-trace sandbox panel

packages/db/forge_db/migrations/versions/xxxx_container_sandboxing.py  # agent_runs cols + sandbox_instances

# Deploy (extends F14 substrate):
deploy/docker-compose.yml                              # + docker-proxy, sandbox-proxy, sandbox_ctl/sandbox_egress nets, worker DOCKER_HOST (EDIT)
deploy/sandbox/base.Dockerfile                         # forge-sandbox-base (uid/gid 10001, git, tini, timeout)
deploy/sandbox/python.Dockerfile                       # forge-sandbox-python (uv + cpython)
deploy/sandbox/node.Dockerfile
deploy/sandbox/go.Dockerfile
deploy/sandbox/squid.conf                              # egress allowlist proxy config
deploy/scripts/preflight.sh                            # + Docker Engine >= 26.1 check (EDIT, F14)
deploy/.env.production.example                          # + FORGE_SANDBOX_* keys (EDIT, F14)
deploy/tests/test_compose_contract.py                  # + sandbox service/network contract tests (EDIT, F14)
docs/self-hosting/security.md                          # + container-sandbox + socket-proxy hardening section (EDIT, F14)
```

## 11. Research references (relevant links from the spec/research report)

- **Sandbox V2 = Docker containers / Firecracker; "Stronger isolation for multi-tenant":** FORGE_SPEC.md §"Technology Stack" (Sandbox V1/V2 rows).
- **Security requirement — "Sandbox isolation: Git worktrees (V1), Docker containers (V2) — no cross-task filesystem access":** FORGE_SPEC.md §"Security" table.
- **Phase 2 roadmap item — "Container sandboxing (Docker-based per-task isolation)":** FORGE_SPEC.md §"Phase 2 — Depth (V2)".
- **V1 worktree sandbox + the explicit "worktrees are isolation, not a security boundary" gap this slice closes, and the `SandboxCommandRunner` seam:** `docs/implementation-slices/v1/F06-single-execution-agent.md` (§3.3, §8, §9) and `docs/implementation-slices/v1/F08-plan-execute-verify-pr-approval.md` (`VerificationService`, `SandboxCommandRunner`).
- **Open SWE — "isolated sandboxes per task":** https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents · https://github.com/langchain-ai/open-swe (research report cite:35).
- **Docker Compose production hardening (digest pinning, non-root, network segmentation, log caps, and the documented raw-socket exception (`autoheal`, §8) this slice hardens further via a dedicated `docker-proxy`):** FORGE_SPEC.md §"Production Docker Compose Requirements" + `docs/implementation-slices/v1/F14-docker-compose-selfhost.md` (§8 autoheal/socket exception) ; https://distr.sh/blog/running-docker-in-production/ (research report cite:120).
- **Socket-proxy hardening (Tecnativa docker-socket-proxy):** https://github.com/Tecnativa/docker-socket-proxy.
- **External canonical refs needed at implementation time (not in spec):** Docker `volume-subpath` mounts (Engine 26+) https://docs.docker.com/engine/storage/volumes/ ; Docker Engine security / `--cap-drop`, `--read-only`, `--pids-limit`, `--security-opt no-new-privileges` https://docs.docker.com/engine/security/ ; Docker Python SDK https://docker-py.readthedocs.io/ ; Sysbox/gVisor as a future runtime-level hardening (V3 Firecracker/gVisor item) https://github.com/nestybox/sysbox · https://gvisor.dev/.
- **V3 escalation — "Firecracker / gVisor sandbox isolation":** FORGE_SPEC.md §"Phase 3 — Scale (V3)".

## 12. Out of scope / future

- **Firecracker / gVisor / microVM isolation** — V3 (`FORGE_SPEC.md` Phase 3). F19 stops at Docker containers; the `SandboxProvider` Protocol is the seam a future `FirecrackerSandboxProvider` plugs into.
- **Kubernetes per-task isolation** — the K8s analogue (per-task `Job`/Pod with restricted `PodSecurityContext` + `NetworkPolicy`, replacing DooD) is a V2 Helm-chart concern tracked with the Kubernetes slice, not built here.
- **Sysbox/gVisor runtime-level hardening** of the worker/daemon — noted as future hardening in `security.md`; default ships the socket-proxy + locked-down container model.
- **Per-language sandbox image curation beyond python/node/go** — additional images are additive (allowlist + new `FORGE_SANDBOX_IMAGE_*` envs); not part of this slice.
- **In-container file tools** (routing `read_repo`/`write_code` through the container) — unnecessary while the worktree is a shared mount and file tools execute no arbitrary code; revisit only if git operations also move into the sandbox.
- **Snapshot/restore of warm sandbox containers across runs** (pool reuse) — a performance optimization left to a later slice; V1 of F19 uses one disposable container per run.
- **The V1 worktree path itself, the agent loop, verification parsing, PR flow** — owned by F06/F08; F19 only refactors the command-execution seam and adds the container provider.
