# F34 — Firecracker / gVisor Sandbox Isolation

> Phase: v3 · Spec module(s): Technology Stack → *Sandbox (V2): Docker containers / Firecracker — Stronger isolation for multi-tenant* + Security → *Sandbox isolation: Git worktrees (V1), Docker containers (V2) — no cross-task filesystem access* + Phase 3 — Scale (V3) → *"Firecracker / gVisor sandbox isolation"* + Execution Agent Runtime (`packages/agent-runtime/forge_agent/sandbox`, the `SandboxProvider` seam owned by `v2/F19`) · Status target: **Done** = the execution agent can run a task's untrusted command execution behind a **kernel boundary** — either gVisor (`runsc`, a userspace guest kernel) or a Firecracker microVM (a hardware-virtualized guest, exposed as an OCI runtime via Kata Containers `kata-fc`) — selected by the same workspace-minimum/per-repo-policy precedence as F19, with the isolation **lattice extended** to `worktree < container < gvisor < microvm` (a policy may strengthen but never weaken below the workspace minimum, and a requested kernel-boundary kind never silently downgrades to a weaker runtime); both new providers **reuse F19's `ContainerSandboxProvider` machinery** (single-worktree mount, `cap_drop: ALL`, non-root uid, read-only rootfs, CPU/memory/PID limits, exec-with-timeout, OOM semantics, output capping, teardown, orphan reaping, audit) and differ only in the OCI `runtime` selected at container create plus runtime-specific preflight and VM/kernel metadata; the host daemon is preflight-gated for `/dev/kvm` + nested virt (microvm) and for runtime registration (`runsc`/`kata-fc` present in `docker info` runtimes); the run trace and `sandbox_instances` record the runtime, the isolation class, and (for microvm) the guest kernel version; and the F19 sandbox suite passes **parametrized over the new runtimes** in a virtualization-enabled CI job. Acceptance criteria 1–17 pass; `packages/agent-runtime` sandbox suite (unit + `@pytest.mark.gvisor` / `@pytest.mark.firecracker` integration) and the `deploy/` + `deploy/helm/` contract additions are green (ruff + types + pytest).

---

## 1. Intent — what & why

F19 moved untrusted command execution (a task's tests, build scripts, and transitively its third-party dependencies) off the host worker and into a per-task Docker container with `cap_drop: ALL`, `no-new-privileges`, non-root uid, read-only rootfs, tmpfs scratch, no network by default, and CPU/memory/PID limits. That contains *most* of the blast radius — but a Docker container **shares the host kernel**. The entire isolation rests on Linux namespaces, cgroups, and seccomp; a single kernel-level vulnerability (a privilege-escalation syscall bug, a namespace escape) lets a malicious dependency break out of every Docker sandbox on the node and read other tenants' worktrees, the BYOK model key, and internal network services. For a multi-tenant self-hosted Forge running genuinely untrusted repos, namespace isolation is not a sufficient trust boundary.

F34 closes that gap by putting a **real kernel boundary** between untrusted code and the host, with two complementary mechanisms (exactly the two the spec names):

- **gVisor (`runsc`)** — an application kernel implemented in userspace that intercepts the sandbox's syscalls and services them itself, so the guest never makes direct host syscalls. It is an OCI runtime: selecting it is a per-container `HostConfig.Runtime` choice. Low operational cost (no KVM required with the `systrap`/`ptrace` platform), modest per-syscall overhead, dramatically reduced host kernel attack surface.
- **Firecracker microVM** — a minimal KVM-based VMM that boots a guest with its **own Linux kernel** in ~125 ms. We expose it through **Kata Containers with the Firecracker hypervisor** (`kata-fc`), which presents an OCI-compatible runtime so the same Docker create/exec/mount/teardown flow F19 already built works unchanged — the container simply runs inside a microVM. This is hardware-virtualized isolation: the guest kernel is a separate kernel; an escape requires breaking the VMM/hypervisor, a far smaller and more hardened surface.

The design is deliberately **additive and minimal-diff**. F19 already abstracted everything behind the frozen `SandboxProvider`/`SandboxSession` Protocols and explicitly left the seam for "a future `FirecrackerSandboxProvider`" (F19 §12). F34 does not rewrite the agent loop, the verification pipeline, the mount strategy, the egress proxy, the reaper, or the audit trail. It:
1. extends the `SandboxKind` enum and the selection lattice with `gvisor` and `microvm`;
2. adds two thin providers that subclass F19's `ContainerSandboxProvider`, overriding only the OCI runtime and runtime-specific create options / preflight;
3. adds preflight + compose/Helm runtime-registration and `/dev/kvm` plumbing;
4. records the runtime + guest-kernel in the data model and run trace.

Design principles (carried from F19, sharpened for a kernel boundary):
- **Opt-in, monotonic isolation.** Default stays `worktree`. The lattice is `worktree < container < gvisor < microvm`. A repo policy may request *stronger* isolation than the workspace minimum, never weaker. An operator who mandates `microvm` cannot be undercut by a repo policy.
- **No silent downgrade — ever.** If `gvisor`/`microvm` is resolved but the runtime is not registered, or KVM is missing for `microvm`, the run **fails loudly** with a named reason. Forge never quietly runs untrusted code under a weaker runtime than the operator asked for. This is the whole point of the feature.
- **Reuse, don't reinvent.** Both providers inherit F19's hardening, mounts, exec, OOM/timeout, capping, teardown, reaper, and audit. F34's net-new code is the runtime selection, two small provider subclasses, preflight, and deploy wiring.

## 2. User-facing behavior / journeys

F34 has no end-user GUI. Its "users" are (a) the **execution agent / workflow** (machine), (b) the **self-hosting operator** who provisions a virtualization-capable host and chooses the isolation tier, and (c) the **reviewer/auditor** who sees the runtime + isolation class on the run trace (F10).

1. **Operator enables gVisor workspace-wide (no special hardware).** Operator runs `deploy/scripts/install-runtimes.sh --gvisor`, which installs `runsc`, registers it in `/etc/docker/daemon.json` `runtimes`, and restarts the daemon. They set `FORGE_SANDBOX_KIND=gvisor` in `.env.production` and restart the worker. Every agent run now executes commands inside a gVisor sandbox; the run trace shows `runtime: runsc`, `isolation_class: userspace_kernel`. No board/UX change.
2. **Operator enables Firecracker microVMs (virtualization-capable host).** On a bare-metal or nested-virt-enabled host, operator runs `deploy/scripts/install-runtimes.sh --firecracker`, which installs Kata + Firecracker, registers `kata-fc`, and verifies `/dev/kvm`. `deploy/scripts/preflight.sh` confirms KVM + nested virt + the registered runtime *before* `up`. They set `FORGE_SANDBOX_KIND=microvm`. Every run now executes inside a Firecracker microVM with its own guest kernel; the trace shows `runtime: kata-fc`, `isolation_class: microvm`, `guest_kernel: 6.x`.
3. **Per-repo strengthening.** A repo handling especially untrusted code sets `.forge/policy.yaml` → `sandbox.isolation: microvm` while the workspace minimum is `gvisor`. The run resolves to `microvm` (stronger wins). A repo that requests `container` while the workspace minimum is `gvisor` still resolves to `gvisor` (no downgrade).
4. **Happy path (machine).** Workflow reaches `task_ready → executing`. F06 creates the worktree on the host; F34's provider starts a sandbox **with the selected runtime** bound to that worktree; `run_tests` executes the literal `policy.commands` string via `docker exec` inside the kernel-isolated sandbox; results parse exactly as in F19; on terminal status the sandbox (and its VM, for microvm) is torn down and the worktree is committed/diffed on the host.
5. **Dependency install with egress.** A repo whose `install` needs the registry sets `network: egress`; the sandbox attaches to F19's `sandbox_egress` network and reaches only the allowlisting `sandbox-proxy`. This is unchanged by the runtime: gVisor's netstack and Kata's tap both attach to the same Docker network, so the egress story holds.
6. **Missing capability → fail loud.** Operator sets `FORGE_SANDBOX_KIND=microvm` on a host without `/dev/kvm`. `preflight.sh` fails before startup; if it is somehow bypassed, the first run resolves `microvm`, the provider raises `SandboxRuntimeUnavailable`, and the run finalizes `failed` with `needs_human_reason` naming the missing KVM/runtime — **never** a fallback to runc/gVisor/host.
7. **Crash recovery.** A worker crash leaves an orphan microVM sandbox container (and its `firecracker`/`virtiofsd` helper processes, which the Kata shim ties to the container lifecycle). F19's reaper removes the `forge.sandbox=true` container; the Kata shim tears down the VM and helpers when the container is removed; F34 adds a post-reap assertion/sweep for any leftover jailer chroot or VM process for that run id.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One Alembic migration: `packages/db/forge_db/migrations/versions/xxxx_kernel_sandbox_isolation.py`.

**Extends the `sandbox_kind` enum** (created by F19) with two values, using Postgres `ALTER TYPE sandbox_kind ADD VALUE` (additive; `worktree`/`container` unchanged):
- `gvisor`
- `microvm`

These flow into both `agent_runs.sandbox_kind` and `sandbox_instances.kind` (both already typed `sandbox_kind` by F19).

**Extends `sandbox_instances`** (created by F19) with runtime/VM provenance:
| Column | Type | Notes |
|---|---|---|
| `runtime` | `text` null | OCI runtime actually used: `runc` (container), `runsc` (gvisor), `kata-fc` (microvm); null for `worktree` |
| `isolation_class` | enum `sandbox_isolation_class` (`host_process`,`namespace`,`userspace_kernel`,`microvm`) | derived from kind; the auditable trust tier |
| `gvisor_platform` | `text` null | `systrap`\|`kvm`\|`ptrace` for gvisor; null otherwise |
| `guest_kernel_version` | `text` null | `uname -r` inside the sandbox for `microvm` (proof of a separate kernel); null otherwise |
| `vm_vcpus` | `int` null | guest vCPUs for `microvm` |
| `vm_memory_mb` | `int` null | guest RAM for `microvm` |
| `boot_ms` | `int` null | sandbox/VM start latency (observability; microvm boot, gvisor init) |

No new tables. Per-command execution remains audited as F06 `agent_steps` rows; the existing `output.sandbox` block (F19) gains `runtime` and `isolation_class` keys (additive). The `agent_runs.sandbox_*` columns from F19 are reused as-is (the enum extension is the only change there). Every kernel-boundary lifecycle transition (create / exec / teardown / timeout / oom / reap) continues to emit a central, tamper-evident `audit_log` `AuditEvent` through F19's `deps.audit` sink (`cross-cutting/F39-audit-log`); F34 adds `runtime` + `isolation_class` (+ microvm `guest_kernel_version`) to that event's `metadata`, which is passed through F37's canonical `SecretRedactor` (`cross-cutting/F37-auth-secrets-byok`) before persistence — satisfying the non-negotiable "audit log for every agent action, immutable and queryable".

Index: `ix_sandbox_instances_isolation_class (isolation_class)` (lets auditors query "which runs executed under a microVM boundary?").

### 3.2 Backend (FastAPI routes + services/packages)

No new business routes. `GET /api/v1/agent-runs/{id}` (F06) already returns the F19 `sandbox` block; F34 adds `runtime`, `isolation_class`, `gvisor_platform`, `guest_kernel_version`, `vm_vcpus`, `vm_memory_mb`, `boot_ms` to the `SandboxInstanceRead` schema in `apps/api/forge_api/schemas/agent.py` (additive; consumed by F10's Sandbox panel). That is the only API surface change.

The **policy schema** `PolicySandboxBlock` (added by F19 to the `Policy` model in `packages/contracts/forge_contracts/policy.py`, re-exported via `forge_policy.schema`; schema owned by `v1/F04-repo-policy`) gains the new enum values for `sandbox.isolation` plus optional microVM sizing fields (`vm_vcpus`, `vm_memory`) and `gvisor_platform`. F34 contributes the model extension and the lattice update to `resolve_sandbox_kind` (in `forge_agent.sandbox.selection`).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

The core of the slice. Package: **`packages/agent-runtime/forge_agent/sandbox/`** (the package F19 created).

New/changed modules:
- `sandbox/gvisor.py` — `GvisorSandboxProvider(ContainerSandboxProvider)`: overrides the container-create call to set `runtime=settings.gvisor_runtime` (`runsc`) and the gVisor platform annotation; overrides `preflight()` to require the runtime registered. **All mounts, limits, exec, OOM/timeout, capping, teardown, reaping inherited unchanged from F19.**
- `sandbox/microvm.py` — `MicroVMSandboxProvider(ContainerSandboxProvider)`: overrides container-create to set `runtime=settings.microvm_runtime` (`kata-fc`), inject Kata sizing annotations (`io.katacontainers.config.hypervisor.default_vcpus`, `..default_memory`) derived from `SandboxResourceLimits`, and (post-start) capture the guest kernel version + boot latency into `sandbox_instances`; overrides `preflight()` to require `/dev/kvm` + the registered runtime; overrides `teardown()` to additionally assert VM/helper-process cleanup. Mount strategy: virtio-fs via the inherited `volume-subpath` mount (with F19's per-run-named-volume fallback when `volume-subpath` is unsupported under the runtime — see §9).
- `sandbox/runtime.py` — `OciRuntime` registry helpers: `detect_registered_runtimes(docker_client) -> set[str]` (reads `docker info` `Runtimes`), `isolation_class_for(kind) -> SandboxIsolationClass`.
- `sandbox/selection.py` (EDIT, F19) — `resolve_sandbox_kind` lattice extended to 4 levels (see §4); `resolve_sandbox_settings` learns the new policy fields.
- `sandbox/settings.py` (EDIT, F19) — `SandboxSettings` gains the F34 envs (§4).
- `sandbox/factory.py` (EDIT, F19) — `build_sandbox_provider(settings)` returns `Gvisor`/`MicroVM` providers for the new kinds; it **also runs the provider's `preflight()`** at construction when the resolved workspace minimum is a kernel-boundary kind, so misconfiguration fails at worker boot, not mid-run.

**Integration into F06's loop:** none beyond F19's existing wiring. F19 already placed `deps.sandbox_provider` on `RuntimeDeps`, routes `run_tests` through `ctx.command_runner.run(...)`, and tears down before `WorktreeSandbox.cleanup`. F34 only changes *which* provider the factory returns; `nodes.load_context`, `tools/run_tests.py`, `nodes.finalize`, and F08's `VerificationService` are untouched. F08's verification-independence guarantee is preserved automatically: F08's authoritative `run_checks` gate obtains a **separate** `SandboxSession` from the **same** `SandboxProvider` (F19 §3.3), so when the workspace minimum is a kernel-boundary kind the independent verify run executes under the identical gVisor/microVM boundary without F34 touching F08 — the human-approval-before-merge flow (`v1/F08-plan-execute-verify-pr-approval`) is unchanged.

**Celery tasks:** none new. F19's `sandbox.reap_orphans` beat task and `worker_ready` reap hook continue to work — gVisor/Kata sandboxes are ordinary `forge.sandbox=true` Docker containers, so the existing reaper GCs them. F34 extends `reaper.reap_orphans` with a final `_sweep_vm_artifacts(run_ids_removed)` pass (remove any orphaned jailer chroot under `FORGE_SANDBOX_JAILER_ROOT` / kill any `firecracker`/`virtiofsd` whose container is gone), guarded so it is a no-op for the non-microvm kinds.

`AgentSettings.sandbox: SandboxSettings` (F06/F19) absorbs the new fields; no new settings object.

### 3.4 Frontend / UI (Next.js routes/components)

Minimal. The **Run Trace Viewer (F10)** Sandbox panel (`apps/web/components/runs/SandboxPanel.tsx`, added by F19) gains: an **isolation-class badge** (`namespace` / `userspace kernel` / `microVM`), the `runtime` string, the gVisor platform, and (for microVM) the guest kernel version + vCPU/RAM + boot ms. Pure additive rendering of the extended `sandbox` block; no new routes, no state machine. (If F10 is not yet built, this is a contract addition consumed later.)

### 3.5 Infra / deploy (compose, helm, caddy)

Builds on the F19 compose substrate (`docker-proxy`, `sandbox-proxy`, `sandbox_ctl`/`sandbox_egress` networks, curated images) and the F14 hardening contract. F34 adds **host-runtime registration and KVM plumbing**, not new long-running services.

**Host runtime registration (the central deploy change):**
- `deploy/scripts/install-runtimes.sh` (new) — idempotent installer: `--gvisor` installs `runsc` and merges a `runtimes.runsc` entry into `/etc/docker/daemon.json`; `--firecracker` installs Kata Containers + Firecracker and merges a `runtimes.kata-fc` entry; both then `systemctl restart docker`. Verifies success via `docker info --format '{{json .Runtimes}}'`.
- `deploy/docker/daemon.json` (EDIT, F14) — documents/ships the `runtimes` block so a host configured by `install-runtimes.sh` lists `runc`, `runsc`, `kata-fc`. (No runtime is the daemon *default*; the per-container `HostConfig.Runtime` selects it.)
- `docker-proxy` (F19) — **unchanged**: selecting a runtime is part of the `POST /containers/create` body (`POST=1` already enabled), so no new proxy verb is required. (Asserted by contract — see AC.)
- The **host (where dockerd runs) needs `/dev/kvm`** for `microvm`. On bare metal this is present; on a cloud VM the operator must enable nested virtualization. Preflight checks it. The *worker* container still needs no `/dev/kvm` and no Docker socket — it only issues create/exec through `docker-proxy`.

**Preflight (`deploy/scripts/preflight.sh`, EDIT, F14/F19):**
- If `FORGE_SANDBOX_KIND` ∈ {`gvisor`,`microvm`}: assert the runtime is registered (`docker info` Runtimes contains `runsc` / `kata-fc`); fail with a specific message + the `install-runtimes.sh` hint otherwise.
- If `microvm`: assert `/dev/kvm` exists and is readable by the daemon, and (best-effort) that nested virt is on (`kvm_intel nested` / `kvm_amd nested` = `Y`, or `egrep -c '(vmx|svm)' /proc/cpuinfo > 0`).
- Continues to gate Docker Engine ≥ 26.1 (F19's `volume-subpath` requirement).

**Curated sandbox images:** reuse F19's `deploy/sandbox/{base,python,node,go}.Dockerfile` unchanged. F34 adds a CI compatibility gate that the python/node/go verification commands run successfully under `runsc` (gVisor implements a syscall subset) and under `kata-fc`; any image needing a tweak for gVisor compatibility is fixed in the existing Dockerfiles (no new images).

**Helm (`deploy/helm/forge`, EDIT, F24):** F24 already renders the K8s workloads with hardened `securityContext`. F34 adds the **Kubernetes-native runtime selection**:
- `deploy/helm/forge/templates/runtimeclass-gvisor.yaml` and `runtimeclass-kata-fc.yaml` (conditional on `sandbox.runtimeClasses.*.enabled`) — `RuntimeClass` objects (`handler: runsc` / `handler: kata-fc`) with node scheduling/tolerations for runtime-capable node pools.
- `values.yaml` `sandbox` block: `kind` (workspace minimum), `runtimeClassName` per kind, node selector/tolerations for the runtime node pool, and (microvm) a guard that the cluster exposes `/dev/kvm` (documented; the worker that *launches* sandboxes does so via the K8s sandbox path tracked as future — see §12). The chart sets the worker's `FORGE_SANDBOX_*` env from this block.
- `docs/self-hosting/kubernetes.md` gains a "Stronger sandbox isolation (gVisor / Kata-Firecracker)" section.

`docs/self-hosting/security.md` (EDIT, F14/F19) gains a "Kernel-boundary sandboxing" section documenting the runtime trust tiers, the no-silent-downgrade guarantee, KVM/nested-virt requirements, and the gVisor-vs-Firecracker tradeoffs.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

> Types marked **(contracts)** live in `packages/contracts/forge_contracts/sandbox.py` (created by F19). F34 **extends** them additively; existing F19 consumers compile unchanged.

**Enum extension (contracts):**
```python
class SandboxKind(str, Enum):
    worktree = "worktree"     # V1: host subprocess (LocalSandbox)
    container = "container"   # V2: Docker container (runc, shared host kernel) — F19
    gvisor = "gvisor"         # V3: Docker container under gVisor runsc (userspace kernel) — F34
    microvm = "microvm"       # V3: Docker container under Kata+Firecracker (hardware VM) — F34

class SandboxIsolationClass(str, Enum):
    host_process = "host_process"        # worktree
    namespace = "namespace"              # container (runc)
    userspace_kernel = "userspace_kernel"  # gvisor
    microvm = "microvm"                  # microvm (firecracker)

# isolation lattice (selection precedence; higher = stronger; never downgrade)
SANDBOX_KIND_RANK: dict[SandboxKind, int] = {
    SandboxKind.worktree: 0,
    SandboxKind.container: 1,
    SandboxKind.gvisor: 2,
    SandboxKind.microvm: 3,
}
```

**`SandboxSpec` extension (contracts):** F19's `SandboxSpec` gains:
```python
    runtime: str | None = None          # resolved OCI runtime: runc|runsc|kata-fc (None => derive from kind+settings)
    gvisor_platform: str | None = None  # systrap|kvm|ptrace (gvisor only)
    vm_vcpus: int | None = None         # microvm guest vCPUs (defaults from limits.cpus)
    vm_memory_mb: int | None = None     # microvm guest RAM (defaults from limits.memory_mb)
```
`SandboxResourceLimits`, `SandboxSession`, `SandboxProvider`, `CommandOutput`, and the error hierarchy from F19 are unchanged in shape. F34 adds one error:
```python
class SandboxRuntimeUnavailable(SandboxStartupError):
    """Resolved kind requires an OCI runtime (runsc/kata-fc) or /dev/kvm that is not
    available on the daemon host. Raised by provider.preflight()/create(); NEVER downgraded."""
```

**Provider preflight (contracts addition to the `SandboxProvider` Protocol — default no-op so F19 providers are unaffected):**
```python
class SandboxProvider(Protocol):
    kind: SandboxKind
    async def preflight(self) -> None: ...                              # NEW: raise SandboxRuntimeUnavailable if unusable
    async def create(self, spec: SandboxSpec) -> SandboxSession: ...
    async def reap_orphans(self) -> int: ...
```

**Selection / precedence (`forge_agent.sandbox.selection`, EDIT of F19):**
```python
def resolve_sandbox_kind(workspace_min: SandboxKind, policy_request: SandboxKind | None) -> SandboxKind:
    """Return the STRONGER of the two by SANDBOX_KIND_RANK. A policy may strengthen
    (e.g. workspace=gvisor, policy=microvm -> microvm) but never weaken below the
    workspace minimum (workspace=gvisor, policy=container -> gvisor). Both unset -> worktree."""
    req = policy_request or workspace_min
    return workspace_min if SANDBOX_KIND_RANK[workspace_min] >= SANDBOX_KIND_RANK[req] else req
```

**Provider classes (`forge_agent.sandbox.gvisor` / `.microvm`):**
```python
class GvisorSandboxProvider(ContainerSandboxProvider):       # F19 base
    kind = SandboxKind.gvisor
    async def preflight(self) -> None:
        # raise SandboxRuntimeUnavailable unless settings.gvisor_runtime in detect_registered_runtimes()
        ...
    def _create_kwargs(self, spec: SandboxSpec) -> dict:      # F19 hook
        kw = super()._create_kwargs(spec)
        kw["runtime"] = self._settings.gvisor_runtime         # "runsc"
        kw["environment"] = {**kw["environment"], "GVISOR_PLATFORM": spec.gvisor_platform or self._settings.gvisor_platform}
        return kw

class MicroVMSandboxProvider(ContainerSandboxProvider):
    kind = SandboxKind.microvm
    async def preflight(self) -> None:
        # raise SandboxRuntimeUnavailable unless settings.microvm_runtime registered AND /dev/kvm present on daemon host
        ...
    def _create_kwargs(self, spec: SandboxSpec) -> dict:
        kw = super()._create_kwargs(spec)
        kw["runtime"] = self._settings.microvm_runtime        # "kata-fc"
        kw["labels"] = {**kw["labels"],
            "io.katacontainers.config.hypervisor.default_vcpus": str(spec.vm_vcpus or int(spec.limits.cpus)),
            "io.katacontainers.config.hypervisor.default_memory": str(spec.vm_memory_mb or spec.limits.memory_mb)}
        return kw
    async def create(self, spec: SandboxSpec) -> SandboxSession:
        session = await super().create(spec)
        # capture guest kernel + boot latency into sandbox_instances (proof of a separate kernel)
        ...
        return session
```
> `_create_kwargs` is the single F19 hook this slice relies on existing; if F19's `ContainerSandboxProvider.create` inlines its kwargs, F34's first task is the trivial refactor to extract `_create_kwargs(spec) -> dict` (behavior-preserving, covered by F19's existing `test_container_create_args.py`).

**Settings (`forge_agent.sandbox.settings.SandboxSettings`, env-bound; EDIT of F19):**
| Env | Default | Notes |
|---|---|---|
| `FORGE_SANDBOX_KIND` | `worktree` | workspace **minimum** isolation; now accepts `gvisor`,`microvm` |
| `FORGE_SANDBOX_GVISOR_RUNTIME` | `runsc` | OCI runtime name as registered in daemon.json |
| `FORGE_SANDBOX_GVISOR_PLATFORM` | `systrap` | `systrap`\|`kvm`\|`ptrace` (kvm fastest; needs `/dev/kvm`) |
| `FORGE_SANDBOX_MICROVM_RUNTIME` | `kata-fc` | Kata+Firecracker OCI runtime name |
| `FORGE_SANDBOX_MICROVM_VCPUS` | (unset → from `limits.cpus`) | guest vCPU default |
| `FORGE_SANDBOX_MICROVM_MEMORY_MB` | (unset → from `limits.memory_mb`) | guest RAM default |
| `FORGE_SANDBOX_REQUIRE_KVM` | `true` | when `microvm`, preflight fails without `/dev/kvm` |
| `FORGE_SANDBOX_JAILER_ROOT` | `/var/lib/forge/jailer` | VM-artifact sweep root for the reaper (direct-FC/jailer path) |

(All F19 envs — `FORGE_SANDBOX_DOCKER_HOST`, image pins/allowlist, worktree volume, limits, network, timeouts, reap cadence, uid/gid — remain in force.)

**policy.yaml `sandbox:` block (`PolicySandboxBlock`, owned by F04, extended here):**
```yaml
sandbox:
  isolation: microvm            # worktree | container | gvisor | microvm  (request; >= workspace minimum)
  gvisor_platform: kvm          # optional; only meaningful when isolation: gvisor
  vm_vcpus: 2                    # optional; only meaningful when isolation: microvm
  vm_memory: 4g                 # optional; only meaningful when isolation: microvm
  # F19 fields still apply (image, network, egress_allowlist, cpus, memory, pids_limit,
  # exec_timeout_seconds, setup_commands); image must remain on FORGE_SANDBOX_ALLOWED_IMAGES.
```

**RuntimeClass (Helm, `deploy/helm/forge`) — the K8s analogue of daemon.json runtimes:**
```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata: { name: forge-gvisor }      # values.sandbox.runtimeClasses.gvisor.name
handler: runsc
scheduling:
  nodeSelector: { forge.dev/sandbox-runtime: gvisor }
  tolerations: [{ key: forge.dev/sandbox-runtime, operator: Equal, value: gvisor, effect: NoSchedule }]
---
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata: { name: forge-kata-fc }
handler: kata-fc
scheduling:
  nodeSelector: { forge.dev/sandbox-runtime: kata-fc }
```

## 5. Dependencies — features/slices that must exist first

| Ref | Why F34 needs it | Hard/Soft |
|---|---|---|
| `v2/F19-container-sandboxing` | Owns the `SandboxProvider`/`SandboxSession` Protocols, `ContainerSandboxProvider` (the base F34 subclasses), `SandboxSpec`/`SandboxKind`/error hierarchy, `resolve_sandbox_kind`, `SandboxSettings`, `build_sandbox_provider`, the reaper, the `docker-proxy`+`sandbox-proxy`+networks substrate, `sandbox_instances`, and the F19 test suite F34 parametrizes. F34 is a thin extension of F19. | **Hard** |
| `v1/F06-single-execution-agent` | Owns `RuntimeDeps.sandbox_provider`, `ToolContext.command_runner`, the `run_tests` tool, the worktree, and `agent_runs`. F34 changes none of these; it relies on F19's wiring into them. | **Hard** |
| `v1/F04-repo-policy` | `PolicySandboxBlock` (F34 adds the new `isolation` values + vm fields), `policy.commands` (the only allowlisted commands), `PolicyGuard.check_command`. | **Hard** |
| `v1/F14-docker-compose-selfhost` | The compose substrate, `daemon.json`, `preflight.sh`, the compose-contract test suite F34 extends; the host where the daemon runs. | **Hard** |
| `v1/F00-foundation-substrate` | `agent_runs`/`sandbox_instances` base models, `forge_contracts` package, the Celery app + beat substrate the reaper runs on, MinIO `ObjectStore` for output offload (inherited from F19). | **Hard** |
| `cross-cutting/F39-audit-log` | The canonical frozen `AuditEvent` DTO + `AuditSink` Protocol + `SqlAuditWriter` (`deps.audit`) into the central, immutable, hash-chained `audit_log`. F34 records each kernel-boundary lifecycle transition with `runtime`/`isolation_class`/`guest_kernel_version` through this sink (non-negotiable audit log). | **Hard** |
| `cross-cutting/F37-auth-secrets-byok` | The encrypted per-workspace BYOK vault (the model key F34 must keep out of every sandbox) + the canonical `SecretRedactor` that redacts `AuditEvent.metadata` and the new `runtime`/`isolation_class`/kernel fields before they hit `sandbox_instances`, `agent_steps`, logs, or the audit log. | **Hard** |
| `v2/F24-kubernetes-helm` | The Helm chart F34 adds `RuntimeClass` manifests + `values.sandbox` to (K8s path). | **Soft** (compose path stands alone; Helm additions land when F24 exists) |
| `v1/F10-run-trace-viewer` | Renders the extended `sandbox` block (isolation badge, guest kernel). | **Soft** (contract addition usable later) |

External components (provisioned on the host, not vendored): **gVisor** (`runsc`), **Kata Containers** (`kata-fc` runtime + `containerd-shim-kata-fc-v2`), **Firecracker** (the Kata hypervisor), `virtiofsd`, a **KVM-capable host** (`/dev/kvm`; nested virt on cloud VMs). Python `docker` SDK (already pinned by F19). Docker Engine ≥ 26.1 (F19).

## 6. Acceptance criteria (numbered, testable)

1. **Lattice precedence / no downgrade.** `resolve_sandbox_kind(gvisor, microvm) == microvm`; `resolve_sandbox_kind(microvm, container) == microvm`; `resolve_sandbox_kind(gvisor, worktree) == gvisor`; `resolve_sandbox_kind(container, gvisor) == gvisor`; both-unset → `worktree`. Full 4×4-plus-`None` matrix is exercised. *(unit)*
2. **Factory selects the right provider.** `build_sandbox_provider(settings)` returns `GvisorSandboxProvider` for `kind=gvisor` and `MicroVMSandboxProvider` for `kind=microvm`, each carrying the correct default runtime (`runsc`/`kata-fc`); `worktree`/`container` resolution is unchanged from F19. *(unit)*
3. **gVisor runtime is actually selected (create body).** Against `fake_docker_client`, `GvisorSandboxProvider.create` issues `containers.create(..., runtime="runsc", ...)` and otherwise produces F19's hardening kwargs (user 10001, read-only rootfs, cap_drop ALL, no-new-privileges, single `/workspace` mount, limits, labels). *(unit)*
4. **microVM runtime + sizing (create body).** Against `fake_docker_client`, `MicroVMSandboxProvider.create` issues `runtime="kata-fc"` and Kata vcpu/memory annotations derived from `SandboxResourceLimits` (or the policy `vm_vcpus`/`vm_memory` overrides). *(unit)*
5. **F19 hardening preserved under both runtimes.** The full F19 sandbox suite (`test_container_*`, isolation, timeout, oom, capping, teardown, reaper, uid round-trip, image allowlist, redaction) passes **parametrized over `runtime ∈ {runc, runsc, kata-fc}`** with no behavioral change (unit tier via fake client; integration tier under real runtimes). *(unit + integration)*
6. **gVisor kernel boundary (real runtime).** Under `@pytest.mark.gvisor`, `cat /proc/version` inside the sandbox identifies gVisor (contains `gVisor`), and a host-kernel probe (e.g. `unshare --user` / a syscall gVisor services in userspace) does not execute against the host kernel; `container inspect` shows `HostConfig.Runtime == "runsc"`. *(integration)*
7. **microVM kernel boundary (real runtime).** Under `@pytest.mark.firecracker`, `uname -r` inside the sandbox returns the **guest** kernel version, which differs from the host's `uname -r`; `container inspect` shows `HostConfig.Runtime == "kata-fc"`; `sandbox_instances.guest_kernel_version` is recorded and non-null. *(integration)*
8. **Cross-task isolation still holds.** Under each real runtime, two sessions on `runA`/`runB` mount exactly one `/workspace` each; a file written in run A is not visible in run B (kernel boundary does not weaken the F19 single-mount guarantee). *(integration)*
9. **No network by default.** With `network=none`, an outbound probe (`getent hosts pypi.org` / `curl`) fails inside both gVisor and microVM sandboxes. *(integration)*
10. **Egress allowlist through the proxy.** With `network=egress`, an allow-listed host succeeds via `sandbox-proxy` and a non-allow-listed host is blocked, under both runtimes (gVisor netstack and Kata tap both route only through `sandbox_egress`). *(integration)*
11. **uid round-trip across the boundary.** A file the sandbox writes (`coverage.xml`) is read back by the host worker (uid/gid 10001) so F06/F08 coverage parsing works on output produced inside a gVisor sandbox and inside a microVM (virtio-fs mount). *(integration)*
12. **Preflight gates gVisor.** `provider.preflight()` raises `SandboxRuntimeUnavailable` when `runsc` is absent from `docker info` Runtimes; `preflight.sh` exits non-zero with a message + `install-runtimes.sh` hint when `FORGE_SANDBOX_KIND=gvisor` and the runtime is unregistered. *(unit + shell contract)*
13. **Preflight gates microVM (KVM + runtime).** `MicroVMSandboxProvider.preflight()` raises `SandboxRuntimeUnavailable` when `kata-fc` is unregistered **or** `/dev/kvm` is missing (with `FORGE_SANDBOX_REQUIRE_KVM=true`); `preflight.sh` fails for `microvm` when `/dev/kvm` is absent. *(unit + shell contract)*
14. **No silent downgrade on failure.** When `gvisor`/`microvm` is resolved but the runtime/KVM is unavailable at run time, `create` raises `SandboxRuntimeUnavailable`; the agent run finalizes `failed`/`awaiting_input` with `needs_human_reason` naming the runtime/KVM failure — execution **never** falls back to `runc`, `worktree`, or a host subprocess. `build_sandbox_provider` also fails at worker boot when the workspace-minimum kind's `preflight()` fails. *(unit)*
15. **Docker-proxy needs no new verbs.** Selecting a non-default `HostConfig.Runtime` goes through `POST /containers/create`; the compose contract still shows `docker-proxy` exposing only `CONTAINERS/IMAGES/POST/EXEC/INFO` and the `worker` with no `/var/run/docker.sock` mount. *(compose contract)*
16. **Audit records the trust tier.** Each kernel-boundary run produces a `sandbox_instances` row with `runtime`, `isolation_class` (`userspace_kernel`/`microvm`), and (microvm) `guest_kernel_version`/`vm_vcpus`/`vm_memory_mb`/`boot_ms`; the `agent_steps.output.sandbox` block carries `runtime`+`isolation_class`; **and** a central `audit_log` `AuditEvent` (carrying `runtime`+`isolation_class`+microvm guest kernel in its `metadata`) is emitted via `deps.audit` (`cross-cutting/F39-audit-log`) for every create/exec/teardown/timeout/oom/reap. The BYOK key and git creds never appear in any of them — verified after passing through F37's `SecretRedactor` (assert against serialized records, the `AuditEvent.metadata`, and a captured log buffer). *(unit + integration)*
17. **VM-artifact cleanup.** Under `@pytest.mark.firecracker`, after `teardown()`/`reap_orphans()` there is no orphaned `firecracker`/`virtiofsd` process and no leftover jailer chroot for the removed run id; `sandbox_instances.status=removed`. *(integration)*

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Layout under `packages/agent-runtime/tests/sandbox/` (extends F19's). Three tiers: **unit** (fake Docker client / pure logic, every PR), **gVisor integration** (`@pytest.mark.gvisor`, host with `runsc` registered), **Firecracker integration** (`@pytest.mark.firecracker`, KVM-capable host with `kata-fc`). The virtualization tiers run in a dedicated, virtualization-enabled CI job (bare-metal or nested-virt runner) required at release; unit tier gates every PR. Compose/Helm contract additions live in `deploy/tests/`. Write tests first; drive each AC red→green.

**Key fixtures (`conftest.py`, extending F19):**
- `fake_docker_client` (reuse F19) — extended to record `runtime=` on `containers.create` and to script `docker info` `Runtimes` so `detect_registered_runtimes` and preflight are testable without a daemon.
- `runtime_param` — parametrize fixture over `{runc, runsc, kata-fc}` to run F19's suite across runtimes (AC5).
- `real_gvisor` (`@pytest.mark.gvisor`) — live `docker` client; `skipif` `runsc` not in `docker info` Runtimes; auto-removes `forge.sandbox=true` containers in teardown.
- `real_firecracker` (`@pytest.mark.firecracker`) — live client; `skipif` `kata-fc` unregistered or `/dev/kvm` absent; teardown removes containers and asserts no leftover VM helpers.
- `unregistered_runtime_docker` — `docker info` Runtimes scripted to omit `runsc`/`kata-fc` (drives preflight-failure / no-downgrade unit tests).
- `no_kvm_host` — patches the `/dev/kvm` probe to absent (drives microVM preflight-failure unit test).
- reuse F19's `tmp_worktree_volume`, `policy_with_sandbox`, `fake_object_store`, `redacting_log_capture`.

**Unit tests:**
- `test_selection_lattice.py` — `resolve_sandbox_kind` full precedence matrix incl. the two new kinds; downgrade rejected (AC1).
- `test_factory_kernel_providers.py` — factory returns gVisor/microVM providers with correct default runtimes; F19 kinds unchanged (AC2).
- `test_gvisor_create_args.py` — `runtime="runsc"`, platform env, plus inherited F19 hardening kwargs (AC3,5).
- `test_microvm_create_args.py` — `runtime="kata-fc"`, vcpu/memory annotations from limits and from policy overrides (AC4,5).
- `test_runtime_detection.py` — `detect_registered_runtimes` parses `docker info` Runtimes; `isolation_class_for` maps each kind correctly (AC16).
- `test_preflight_gvisor.py` — `preflight()` raises `SandboxRuntimeUnavailable` when `runsc` absent (AC12).
- `test_preflight_microvm.py` — raises when `kata-fc` absent or `/dev/kvm` missing; passes when both present (AC13).
- `test_no_silent_downgrade.py` — resolved `gvisor`/`microvm` with unavailable runtime → `SandboxRuntimeUnavailable`, mapped to a terminal run with reason, never a runc/worktree fallback; `build_sandbox_provider` raises at boot (AC14).
- `test_audit_fields.py` — persisted `sandbox_instances`/`agent_steps` carry `runtime`+`isolation_class` (+ microvm kernel/vm fields) and never secrets (AC16).
- `test_f19_suite_parametrized.py` — imports/parametrizes F19's create-args/isolation/timeout/oom/capping/reaper unit tests over `runtime_param` (AC5).

**gVisor integration (`@pytest.mark.gvisor`):**
- `test_gvisor_inspect_runtime.py` — `HostConfig.Runtime == "runsc"` (AC6).
- `test_gvisor_kernel_boundary.py` — `/proc/version` shows gVisor; host-kernel probe blocked (AC6).
- `test_gvisor_cross_task_isolation.py` / `test_gvisor_network_none.py` / `test_gvisor_egress.py` / `test_gvisor_uid_roundtrip.py` — F19 isolation/network/uid behaviors under runsc (AC8,9,10,11).
- `test_gvisor_image_compat.py` — python/node/go verification commands (`ruff`, `pytest --cov`, `npm test`, `go test`) succeed under runsc (image-compat gate) (AC5).

**Firecracker integration (`@pytest.mark.firecracker`):**
- `test_microvm_inspect_runtime.py` — `HostConfig.Runtime == "kata-fc"` (AC7).
- `test_microvm_separate_kernel.py` — guest `uname -r` ≠ host `uname -r`; `sandbox_instances.guest_kernel_version` recorded (AC7,16).
- `test_microvm_cross_task_isolation.py` / `test_microvm_network_none.py` / `test_microvm_egress.py` / `test_microvm_uid_roundtrip.py` — virtio-fs mount + tap network behaviors (AC8,9,10,11).
- `test_microvm_timeout_oom.py` — timeout (exit 124) and OOM (`oom_killed`) semantics hold inside the VM; worker survives (AC5).
- `test_microvm_vm_artifact_cleanup.py` — after teardown/reap, no orphan `firecracker`/`virtiofsd`/jailer chroot; status=removed (AC17).
- `test_graph_with_microvm.py` — F06 graph end-to-end with `kind=microvm` + `ScriptedModelClient`: write_code (host) → run_tests (microVM) → finalize succeeded; assert exec ran under `kata-fc` (AC5 at loop level).

**Compose/Helm contract (`deploy/tests/`, extends F14/F19/F24):**
- `test_docker_proxy_unchanged_verbs` (AC15), `test_worker_has_no_docker_socket` (reaffirm AC15).
- `test_preflight_gvisor_runtime_check`, `test_preflight_microvm_kvm_check` — drive `preflight.sh` with stubbed `docker info` / absent `/dev/kvm` (AC12,13).
- `test_install_runtimes_merges_daemon_json` — `install-runtimes.sh --gvisor/--firecracker` produces the expected `runtimes` block (stubbed installer + temp daemon.json).
- `test_helm_runtimeclasses_render` — `helm template` renders `forge-gvisor`/`forge-kata-fc` RuntimeClasses when enabled and omits them when disabled; worker env reflects `values.sandbox` (AC2-analog on K8s).

**Coverage gate:** built under `backend-tdd` — ≥80% line coverage on the new `forge_agent.sandbox` modules, ruff + types green. Unit tier required on every PR; gVisor + Firecracker tiers required at release.

## 8. Security & policy considerations

- **A real kernel boundary is the entire point.** F19's Docker container shares the host kernel; a kernel-level exploit escapes every sandbox on the node. gVisor interposes a userspace application kernel so the guest never issues direct host syscalls (drastically smaller host attack surface); Firecracker (via Kata `kata-fc`) runs the guest in a hardware-virtualized microVM with its own kernel, so an escape must defeat the VMM — a small, hardened surface. This satisfies the spec's "Stronger isolation for multi-tenant" and the Phase 3 "Firecracker / gVisor sandbox isolation" item.
- **Monotonic isolation, no silent downgrade.** The `worktree < container < gvisor < microvm` lattice means a per-repo policy can only *strengthen* isolation, never weaken what an operator mandated (AC1). If the requested runtime/KVM is unavailable, the run **fails loudly** (AC14) — Forge never quietly executes untrusted code under a weaker boundary. `build_sandbox_provider` fails at worker boot if the workspace-minimum runtime is unusable, so misconfiguration is caught before any task runs.
- **All F19 controls still apply inside the stronger boundary.** Single `/workspace` mount (no cross-task FS access, AC8), `cap_drop: ALL`, `no-new-privileges`, non-root uid, read-only rootfs, CPU/memory/PID limits, no network by default with egress only through the allowlisting proxy (AC9,10), curated/allow-listed images, exec-with-timeout, OOM containment, output capping, command-allowlist (`policy.commands` only; no model-authored shell). The kernel boundary is defense-in-depth *on top of*, not instead of, these.
- **Secrets never enter the sandbox.** Unchanged from F19: the BYOK model key (worker-resident, resolved in-memory from F37's vault) and GitHub App creds (host-side git/PR only) are never in `SandboxSpec.env`, exec env, `sandbox_instances`, `agent_steps`, or logs; F37's canonical `SecretRedactor` (`cross-cutting/F37-auth-secrets-byok`) covers the new `runtime`/`isolation_class`/kernel fields and the `AuditEvent.metadata` (AC16).
- **Daemon socket surface unchanged.** Runtime selection is a `POST /containers/create` field, so `docker-proxy` keeps its minimal verb set and the worker still never touches the raw socket (AC15). The added privileged surface is the *host's* KVM device exposed to the daemon — documented in `security.md` with the recommendation to run sandbox runtimes on dedicated, runtime-capable nodes (K8s `RuntimeClass` node pools) isolated from control-plane workloads.
- **KVM / nested-virt trust.** microVM requires `/dev/kvm`; on cloud VMs this means enabling nested virtualization. `security.md` notes the residual trust in the hypervisor and the recommendation to prefer bare-metal sandbox nodes for the highest-untrust tenants; gVisor (no KVM needed) is the recommended default tier where nested virt is unavailable.
- **Auditable trust tier.** Every run records its `isolation_class` and `runtime` (and guest kernel for microVM) in `sandbox_instances`/`agent_steps` **and** as a redacted `AuditEvent` written through F19's `deps.audit` sink into F39's central, immutable, hash-chained `audit_log` (`cross-cutting/F39-audit-log`), giving auditors a single queryable record of exactly which boundary contained each piece of untrusted execution (AC16) — satisfying the Security table's "audit log for every agent action — immutable, queryable" non-negotiable.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** Although the *code* delta is small (two thin provider subclasses + selection lattice + settings + preflight, all reusing F19), the surface that makes it real is wide and operationally heavy: runtime installation/registration scripting, KVM/nested-virt plumbing, the gVisor and Kata+Firecracker integration test tiers (which need a virtualization-capable CI runner), virtio-fs mount/uid correctness, Helm `RuntimeClass` wiring, and the no-silent-downgrade guarantees. Rough split: selection/settings/providers (S), preflight + install-runtimes + compose/daemon.json (M), virtio-fs mount + uid + VM-artifact reaper (M), gVisor+Firecracker integration harness + CI runner (M-L), Helm RuntimeClasses + docs (S).

Key risks:
- **Virtualization-capable CI is hard and slow.** gVisor needs `runsc`; Firecracker needs `/dev/kvm` + nested virt — not available on standard shared CI runners. *Mitigation:* unit tier (fake client, runtime asserted in create body) gates every PR; gVisor + Firecracker tiers run on a dedicated bare-metal/nested-virt runner, required only at release, with strict per-test cleanup.
- **virtio-fs mount + uid semantics under Kata.** F19's `volume-subpath` and uid/gid 10001 round-trip must survive the virtio-fs share so host-side coverage/diff parsing works. *Mitigation:* AC11 guards it per runtime; documented fallback to F19's per-run named volume when `volume-subpath` is unsupported under a runtime; pin `virtiofsd` and Kata config.
- **gVisor syscall compatibility.** gVisor implements a subset of Linux syscalls; a toolchain in a sandbox image could hit an unimplemented call. *Mitigation:* `test_gvisor_image_compat.py` runs the real python/node/go verification commands under runsc; fixes land in the existing F19 Dockerfiles; gVisor platform configurable (`systrap`/`kvm`).
- **microVM boot + dep-install latency.** A VM boot per task adds latency over a runc container. *Mitigation:* one long-lived sandbox per run (many `exec`s), Kata's ~125 ms Firecracker boot, pre-pulled images/rootfs, `setup_commands` once; tiers stay opt-in so latency-sensitive single-tenant deployments keep worktrees/containers.
- **VM artifact leakage.** Crashed runs can leave `firecracker`/`virtiofsd` processes or jailer chroots. *Mitigation:* the Kata shim ties helpers to container lifecycle; F34 adds the `_sweep_vm_artifacts` reaper pass + AC17.
- **Operational complexity / host requirements.** Operators may lack KVM or mis-register runtimes. *Mitigation:* `install-runtimes.sh` + `preflight.sh` hard gates with actionable messages; no silent downgrade; gVisor offered as the no-KVM tier; `security.md`/`kubernetes.md` document node-pool isolation.
- **Direct-Firecracker (non-Kata) demand.** Some operators may want firecracker-containerd without Kata. *Mitigation:* the runtime is configurable via `FORGE_SANDBOX_MICROVM_RUNTIME`; a direct `firecracker-containerd` shim that exposes the same OCI runtime name slots in without provider code changes; a fully custom Firecracker-API + vsock-agent provider is explicitly future (§12).

## 10. Key files / paths (exact)

```
packages/agent-runtime/forge_agent/sandbox/
├── gvisor.py            # GvisorSandboxProvider(ContainerSandboxProvider): runtime=runsc, platform, preflight
├── microvm.py           # MicroVMSandboxProvider(ContainerSandboxProvider): runtime=kata-fc, vcpu/mem, kernel capture, VM cleanup
├── runtime.py           # detect_registered_runtimes(), isolation_class_for()
├── selection.py         # EDIT (F19): SANDBOX_KIND_RANK lattice + resolve_sandbox_kind/settings
├── settings.py          # EDIT (F19): SandboxSettings += gvisor/microvm/KVM/jailer envs
├── factory.py           # EDIT (F19): build_sandbox_provider returns kernel providers + runs preflight at boot
├── container.py         # EDIT (F19): extract _create_kwargs(spec) hook if not already present
└── reaper.py            # EDIT (F19): + _sweep_vm_artifacts() pass (no-op unless microvm)

packages/agent-runtime/tests/sandbox/   # unit + @pytest.mark.gvisor + @pytest.mark.firecracker (see §7)

packages/contracts/forge_contracts/sandbox.py   # EDIT: SandboxKind += gvisor,microvm; SandboxIsolationClass;
                                                 #       SANDBOX_KIND_RANK; SandboxSpec runtime/platform/vm_*;
                                                 #       SandboxRuntimeUnavailable; SandboxProvider.preflight()
packages/contracts/forge_contracts/policy.py     # EDIT (F04/F19): PolicySandboxBlock isolation enum + vm fields (re-exported via forge_policy.schema)

apps/api/forge_api/schemas/agent.py              # EDIT (F06/F19): SandboxInstanceRead += runtime/isolation_class/kernel/vm/boot
apps/web/components/runs/SandboxPanel.tsx        # EDIT (F19): isolation-class badge + runtime + guest kernel

packages/db/forge_db/migrations/versions/xxxx_kernel_sandbox_isolation.py  # enum extension + sandbox_instances cols

# Deploy (extends F14/F19/F24):
deploy/scripts/install-runtimes.sh               # NEW: install+register runsc / kata-fc; verify via docker info
deploy/scripts/preflight.sh                      # EDIT: runtime-registered + /dev/kvm + nested-virt checks
deploy/docker/daemon.json                        # EDIT (F14): runtimes { runsc, kata-fc }
deploy/helm/forge/templates/runtimeclass-gvisor.yaml     # NEW (F24): RuntimeClass handler=runsc
deploy/helm/forge/templates/runtimeclass-kata-fc.yaml    # NEW (F24): RuntimeClass handler=kata-fc
deploy/helm/forge/values.yaml                    # EDIT (F24): sandbox{ kind, runtimeClasses, nodeSelector, tolerations }
deploy/.env.production.example                    # EDIT (F14): + FORGE_SANDBOX_GVISOR_*/_MICROVM_*/_REQUIRE_KVM/_JAILER_ROOT
deploy/tests/test_compose_contract.py            # EDIT: proxy-verbs-unchanged, preflight runtime/kvm gates
deploy/tests/test_helm_chart.py                  # EDIT (F24): RuntimeClass render tests
docs/self-hosting/security.md                    # EDIT: "Kernel-boundary sandboxing" section
docs/self-hosting/kubernetes.md                  # EDIT (F24): gVisor / Kata-Firecracker RuntimeClass section
```

## 11. Research references (relevant links from the spec/research report)

- **Sandbox V2/V3 = Docker containers / Firecracker; "Stronger isolation for multi-tenant":** FORGE_SPEC.md §"Technology Stack" (Sandbox V1/V2 rows).
- **Phase 3 roadmap item — "Firecracker / gVisor sandbox isolation":** FORGE_SPEC.md §"Phase 3 — Scale (V3)".
- **Security requirement — "Sandbox isolation: Git worktrees (V1), Docker containers (V2) — no cross-task filesystem access":** FORGE_SPEC.md §"Security" table.
- **The seam this slice plugs into (the `SandboxProvider`/`SandboxSession` Protocols, `ContainerSandboxProvider`, selection precedence, reaper, docker-proxy substrate, and the explicit "a future `FirecrackerSandboxProvider` plugs into this seam"):** `docs/implementation-slices/v2/F19-container-sandboxing.md` (§3.3, §4, §12).
- **V1 worktree sandbox + the "worktrees are isolation, not a security boundary" gap and the shared `SandboxCommandRunner` seam:** `docs/implementation-slices/v1/F06-single-execution-agent.md` (§3.3, §8, §9).
- **Compose hardening contract + `daemon.json`/`preflight.sh`/socket-proxy patterns F34 extends:** `docs/implementation-slices/v1/F14-docker-compose-selfhost.md`; FORGE_SPEC.md §"Production Docker Compose Requirements"; https://distr.sh/blog/running-docker-in-production/ (research report cite:120).
- **Helm chart + `RuntimeClass`/node-pool path F34 extends:** `docs/implementation-slices/v2/F24-kubernetes-helm.md`.
- **Open SWE — "isolated sandboxes per task" (the pattern this hardens):** https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents · https://github.com/langchain-ai/open-swe (research report cite:35).
- **External canonical refs needed at implementation time (not in spec):** gVisor (`runsc`, OCI runtime, platforms) https://gvisor.dev/docs/ ; Firecracker (microVM VMM, ~125 ms boot, jailer) https://firecracker-microvm.github.io/ ; Kata Containers (Firecracker hypervisor backend, `kata-fc` runtime, virtio-fs) https://katacontainers.io/ and https://github.com/kata-containers/kata-containers ; firecracker-containerd https://github.com/firecracker-microvm/firecracker-containerd ; Kubernetes `RuntimeClass` https://kubernetes.io/docs/concepts/containers/runtime-class/ ; Docker custom runtimes (daemon.json `runtimes`) https://docs.docker.com/reference/cli/dockerd/ ; Docker Python SDK https://docker-py.readthedocs.io/ ; `volume-subpath` (Engine 26+) https://docs.docker.com/engine/storage/volumes/ .

## 12. Out of scope / future

- **Custom Firecracker-API + vsock-agent provider** (direct Firecracker without Kata: jailer chroot, custom kernel+rootfs build, a vsock guest-agent for command exec, vhost-user virtio-fs/blk). The default ships Kata `kata-fc` because it reuses F19's OCI create/exec/mount/teardown wholesale; a from-scratch microVM provider is a later optimization that plugs into the same `SandboxProvider` Protocol. `FORGE_SANDBOX_MICROVM_RUNTIME` already lets operators swap in a `firecracker-containerd` shim without provider code changes.
- **Full Kubernetes per-task sandbox launcher** (a worker that creates per-task `Job`/Pod with `runtimeClassName` + `PodSecurityContext` + `NetworkPolicy` instead of going through the Docker daemon). F34 ships the `RuntimeClass` manifests + values into F24's chart; the K8s *launch path* for sandboxes (replacing the docker-proxy create flow on clusters) is tracked with the Kubernetes sandbox work, not built here.
- **Warm microVM pools / snapshot-restore** across runs (Firecracker snapshotting for sub-100ms task start) — a performance optimization; V1 of F34 uses one disposable sandbox per run.
- **Per-tenant runtime policy beyond the lattice** (e.g. "tenant X always microvm regardless of repo policy") — the workspace-minimum + monotonic lattice covers the operator-mandate case; richer conditional policy is an Advanced-Policy-Engine concern (Phase 3 item, separate slice).
- **Additional guest kernels / image curation per runtime** — additive (new images on the allowlist, Kata kernel config); not part of this slice.
- **The F19/F06/F08 machinery itself** (the agent loop, verification parsing, mounts, egress proxy, reaper core, PR flow) — owned upstream; F34 only adds runtime selection, two provider subclasses, preflight, and deploy wiring.
