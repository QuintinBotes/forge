"""Sandbox package — worktree (V1) + container (V2) command-execution isolation.

Back-compatible surface (unchanged from F06's ``sandbox.py``):
:class:`SandboxError`, :class:`WorktreeSandbox`, :func:`load_agents_md`.

F19 additions: the :class:`~forge_contracts.SandboxProvider` /
:class:`~forge_contracts.SandboxSession` seam with a host-subprocess
(:class:`LocalSandboxProvider`) and a locked-down Docker
(:class:`ContainerSandboxProvider`) implementation, image-allowlist enforcement,
kind/settings precedence (never downgrade), the orphan reaper, and a factory.

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
    SandboxSession,
    SandboxSpec,
    SandboxStartupError,
)
from forge_agent.sandbox.container import (
    ContainerSandboxProvider,
    ContainerSandboxSession,
    select_orphans,
)
from forge_agent.sandbox.factory import build_sandbox_provider
from forge_agent.sandbox.images import resolve_image
from forge_agent.sandbox.local import LocalSandboxProvider, LocalSandboxSession
from forge_agent.sandbox.reaper import reap_orphans
from forge_agent.sandbox.selection import (
    parse_memory_mb,
    resolve_sandbox_kind,
    resolve_sandbox_settings,
)
from forge_agent.sandbox.settings import SandboxSettings
from forge_agent.sandbox.worktree import WorktreeSandbox, _git, load_agents_md

__all__ = [
    "ArtifactStore",
    "CommandOutput",
    "ContainerSandboxProvider",
    "ContainerSandboxSession",
    "LocalSandboxProvider",
    "LocalSandboxSession",
    "SandboxCommandRunner",
    "SandboxError",
    "SandboxExecError",
    "SandboxImageNotAllowed",
    "SandboxKind",
    "SandboxNetwork",
    "SandboxProvider",
    "SandboxResourceLimits",
    "SandboxSession",
    "SandboxSettings",
    "SandboxSpec",
    "SandboxStartupError",
    "WorktreeSandbox",
    "_git",
    "build_sandbox_provider",
    "load_agents_md",
    "parse_memory_mb",
    "reap_orphans",
    "resolve_image",
    "resolve_sandbox_kind",
    "resolve_sandbox_settings",
    "select_orphans",
]
