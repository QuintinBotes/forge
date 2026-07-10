"""Sandbox package — worktree (V1) + container (V2) command-execution isolation.

Back-compatible surface (unchanged from F06's ``sandbox.py``):
:class:`SandboxError`, :class:`WorktreeSandbox`, :func:`load_agents_md`.

F19 additions: the :class:`~forge_contracts.SandboxProvider` /
:class:`~forge_contracts.SandboxSession` seam with a host-subprocess
(:class:`LocalSandboxProvider`) and a locked-down Docker
(:class:`ContainerSandboxProvider`) implementation, image-allowlist enforcement,
kind/settings precedence (never downgrade), the orphan reaper, and a factory.

F34 additions: kernel-boundary isolation behind the same seam — gVisor
(:class:`GvisorSandboxProvider`, ``runsc``) and Firecracker microVMs via Kata
(:class:`MicroVMSandboxProvider`, ``kata-fc``), the extended 4-level lattice
(``worktree < container < gvisor < microvm``), OCI runtime detection/preflight
(:class:`SandboxRuntimeUnavailable`, never a silent downgrade), and the
VM-artifact sweep.

``subprocess`` is re-exported so the legacy worktree test can monkeypatch
``forge_agent.sandbox.subprocess.run``.
"""

from __future__ import annotations

import subprocess

from forge_agent.sandbox.base import (
    ArtifactStore,
    CommandOutput,
    SandboxCommandRunner,
    SandboxError,
    SandboxExecError,
    SandboxImageNotAllowed,
    SandboxKind,
    SandboxNetwork,
    SandboxProvider,
    SandboxResourceLimits,
    SandboxRuntimeUnavailable,
    SandboxSession,
    SandboxSpec,
    SandboxStartupError,
)
from forge_agent.sandbox.container import (
    ContainerSandboxProvider,
    ContainerSandboxSession,
    select_orphans,
)
from forge_agent.sandbox.factory import KERNEL_BOUNDARY_KINDS, build_sandbox_provider
from forge_agent.sandbox.gvisor import GvisorSandboxProvider
from forge_agent.sandbox.images import resolve_image
from forge_agent.sandbox.local import LocalSandboxProvider, LocalSandboxSession
from forge_agent.sandbox.microvm import (
    MicroVMSandboxProvider,
    MicroVMSandboxSession,
    sweep_jailer_chroots,
    sweep_orphaned_jailer_chroots,
)
from forge_agent.sandbox.reaper import reap_orphans
from forge_agent.sandbox.runtime import detect_registered_runtimes, isolation_class_for
from forge_agent.sandbox.selection import (
    parse_memory_mb,
    resolve_sandbox_kind,
    resolve_sandbox_settings,
)
from forge_agent.sandbox.settings import SandboxSettings
from forge_agent.sandbox.worktree import (
    WorktreeSandbox,
    _git,
    discover_agents_md,
    load_agents_md,
)

__all__ = [
    "KERNEL_BOUNDARY_KINDS",
    "ArtifactStore",
    "CommandOutput",
    "ContainerSandboxProvider",
    "ContainerSandboxSession",
    "GvisorSandboxProvider",
    "LocalSandboxProvider",
    "LocalSandboxSession",
    "MicroVMSandboxProvider",
    "MicroVMSandboxSession",
    "SandboxCommandRunner",
    "SandboxError",
    "SandboxExecError",
    "SandboxImageNotAllowed",
    "SandboxKind",
    "SandboxNetwork",
    "SandboxProvider",
    "SandboxResourceLimits",
    "SandboxRuntimeUnavailable",
    "SandboxSession",
    "SandboxSettings",
    "SandboxSpec",
    "SandboxStartupError",
    "WorktreeSandbox",
    "_git",
    "build_sandbox_provider",
    "detect_registered_runtimes",
    "discover_agents_md",
    "isolation_class_for",
    "load_agents_md",
    "parse_memory_mb",
    "reap_orphans",
    "resolve_image",
    "resolve_sandbox_kind",
    "resolve_sandbox_settings",
    "select_orphans",
    "sweep_jailer_chroots",
    "sweep_orphaned_jailer_chroots",
]
