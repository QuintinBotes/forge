"""Sandbox contracts — the command-execution isolation seam (F19).

These DTOs and Protocols are the **frozen** interface the agent runtime
(``forge_agent.sandbox``) implements and the worker / API consume. A
:class:`SandboxProvider` builds a :class:`SandboxSession` (a
:class:`SandboxCommandRunner`) bound to a single task's worktree; the session
runs allow-listed ``policy.commands`` strings either as a host subprocess
(``worktree``, V1) or inside a locked-down Docker container (``container``, V2)
with no behavioural difference at the seam.

The seam (``SandboxCommandRunner.run``) is deliberately minimal and identical
across providers: network mode is fixed per-session at ``create()`` time
(:attr:`SandboxSpec.network`), never per call, so the signature stays stable.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Shared base: tolerant of unknown keys, populatable by field name or alias."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# --------------------------------------------------------------------------- #
# Enums                                                                         #
# --------------------------------------------------------------------------- #


class SandboxKind(enum.StrEnum):
    """Which isolation provider runs a task's commands."""

    WORKTREE = "worktree"  # V1: host subprocess (LocalSandbox)
    CONTAINER = "container"  # V2: per-task Docker container (runc, shared host kernel)
    GVISOR = "gvisor"  # V3: Docker container under gVisor runsc (userspace kernel) — F34
    MICROVM = "microvm"  # V3: Docker container under Kata+Firecracker (hardware VM) — F34


class SandboxIsolationClass(enum.StrEnum):
    """The auditable trust tier a sandbox kind provides (F34)."""

    HOST_PROCESS = "host_process"  # worktree
    NAMESPACE = "namespace"  # container (runc)
    USERSPACE_KERNEL = "userspace_kernel"  # gvisor
    MICROVM = "microvm"  # microvm (kata-fc / firecracker)


#: Isolation lattice (selection precedence; higher = stronger; never downgrade).
SANDBOX_KIND_RANK: dict[SandboxKind, int] = {
    SandboxKind.WORKTREE: 0,
    SandboxKind.CONTAINER: 1,
    SandboxKind.GVISOR: 2,
    SandboxKind.MICROVM: 3,
}

#: Kinds whose sandbox is a Docker container (reaped by label; OCI runtime differs).
CONTAINER_BACKED_KINDS: frozenset[SandboxKind] = frozenset(
    {SandboxKind.CONTAINER, SandboxKind.GVISOR, SandboxKind.MICROVM}
)


class SandboxNetwork(enum.StrEnum):
    """Container network posture."""

    NONE = "none"  # no egress (default)
    EGRESS = "egress"  # via the allow-listing forward proxy only


# --------------------------------------------------------------------------- #
# Command execution                                                            #
# --------------------------------------------------------------------------- #


class CommandOutput(_Model):
    """The result of running a single allow-listed command in a sandbox."""

    exit_code: int
    stdout: str = ""  # capped at FORGE_SANDBOX_OUTPUT_CAP_BYTES (default 256 KiB)
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    # --- F19 additions (additive, backward-compatible) ---
    oom_killed: bool = False
    stdout_artifact_ref: str | None = None  # object-store ref when stdout exceeds cap
    stderr_artifact_ref: str | None = None
    sandbox_kind: SandboxKind = SandboxKind.WORKTREE
    container_id: str | None = None


class SandboxResourceLimits(_Model):
    """Resource ceilings applied to a container sandbox."""

    cpus: float = 2.0
    memory_mb: int = 4096
    pids_limit: int = 512
    tmpfs_mb: int = 1024  # size of /tmp tmpfs


class SandboxSpec(_Model):
    """Everything needed to build one task's sandbox session."""

    agent_run_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: SandboxKind
    host_worktree_path: str  # absolute host path of the worktree
    worktree_volume: str = ""  # named volume backing worktrees (container only)
    worktree_subpath: str = ""  # subpath within the volume == this run's worktree dir
    image: str | None = None  # digest-pinned; required for kind=container, allow-listed
    network: SandboxNetwork = SandboxNetwork.NONE
    egress_allowlist: list[str] = Field(default_factory=list)
    limits: SandboxResourceLimits = Field(default_factory=SandboxResourceLimits)
    env: dict[str, str] = Field(default_factory=dict)  # non-secret env only
    setup_commands: list[str] = Field(default_factory=list)
    exec_timeout_seconds: int = 1800
    run_as_uid: int = 10001
    run_as_gid: int = 10001
    # --- F34 additions (additive; None => derive from kind + settings) ---
    runtime: str | None = None  # resolved OCI runtime: runc | runsc | kata-fc
    gvisor_platform: str | None = None  # systrap | kvm | ptrace (gvisor only)
    vm_vcpus: int | None = None  # microvm guest vCPUs (defaults from limits.cpus)
    vm_memory_mb: int | None = None  # microvm guest RAM (defaults from limits.memory_mb)


# --------------------------------------------------------------------------- #
# API read DTO (surfaced on AgentRunRead.sandbox / run-trace viewer)           #
# --------------------------------------------------------------------------- #


class SandboxInstanceRead(_Model):
    """Read model for a sandbox instance's audit/lifecycle record."""

    kind: SandboxKind
    image: str | None = None  # digest-pinned image (null for worktree)
    network: SandboxNetwork = SandboxNetwork.NONE
    status: str = "creating"  # creating | running | exited | removed | failed
    exit_reason: str | None = None
    limits: SandboxResourceLimits = Field(default_factory=SandboxResourceLimits)
    container_id: str | None = None
    created_at: datetime | None = None
    removed_at: datetime | None = None
    # --- F34 additions (additive; runtime/VM provenance for the audit trail) ---
    runtime: str | None = None  # OCI runtime actually used (null for worktree)
    isolation_class: SandboxIsolationClass = SandboxIsolationClass.HOST_PROCESS
    gvisor_platform: str | None = None  # systrap|kvm|ptrace (gvisor only)
    guest_kernel_version: str | None = None  # microvm: proof of a separate kernel
    vm_vcpus: int | None = None
    vm_memory_mb: int | None = None
    boot_ms: int | None = None  # sandbox/VM start latency


# --------------------------------------------------------------------------- #
# Protocols (the frozen seam)                                                  #
# --------------------------------------------------------------------------- #


@runtime_checkable
class SandboxCommandRunner(Protocol):
    """Runs an exact, allow-listed ``policy.commands`` string. Never model-authored.

    ``command`` is executed via ``sh -lc``; network is fixed at session create
    time (not per call) so the signature is stable across providers.
    """

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandOutput: ...


@runtime_checkable
class SandboxSession(Protocol):
    """A live sandbox bound to one worktree; runs many commands then tears down."""

    sandbox_id: str  # container id, or local pseudo-id
    kind: SandboxKind
    workspace_dir: str  # worktree path *inside* the sandbox
    host_worktree_path: str

    async def setup(self) -> None: ...
    async def teardown(self, *, reason: str = "completed") -> None: ...
    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandOutput: ...


@runtime_checkable
class SandboxProvider(Protocol):
    """Builds sandbox sessions and reaps orphaned ones.

    ``preflight`` (F34) raises when the provider's runtime substrate (OCI
    runtime registration, ``/dev/kvm``) is unusable — a no-op for the F19
    worktree/container providers. Kernel-boundary kinds are **never** silently
    downgraded: an unusable runtime fails loudly instead.
    """

    kind: SandboxKind

    async def preflight(self) -> None: ...
    async def create(self, spec: SandboxSpec) -> SandboxSession: ...
    async def reap_orphans(self) -> int: ...


__all__ = [
    "CONTAINER_BACKED_KINDS",
    "SANDBOX_KIND_RANK",
    "CommandOutput",
    "SandboxCommandRunner",
    "SandboxInstanceRead",
    "SandboxIsolationClass",
    "SandboxKind",
    "SandboxNetwork",
    "SandboxProvider",
    "SandboxResourceLimits",
    "SandboxSession",
    "SandboxSpec",
]
