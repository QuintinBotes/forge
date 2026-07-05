"""Sandbox kind/settings precedence — strengthen-only, never downgrade (F19/F34).

Workspace ``FORGE_SANDBOX_KIND`` is the *minimum* isolation. A repo policy may
request **stronger** isolation but never weaker: a compromised/careless repo
policy cannot opt out of containment the operator mandated. F34 extends the
lattice to four levels: ``worktree < container < gvisor < microvm``.
"""

from __future__ import annotations

import re
import uuid

from forge_agent.sandbox.images import resolve_image
from forge_agent.sandbox.runtime import DEFAULT_RUNTIMES
from forge_agent.sandbox.settings import SandboxSettings
from forge_contracts import (
    CONTAINER_BACKED_KINDS,
    SANDBOX_KIND_RANK,
    PolicySandboxBlock,
    SandboxKind,
    SandboxNetwork,
    SandboxResourceLimits,
    SandboxSpec,
)

_MEMORY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)b?\s*$", re.IGNORECASE)


def resolve_sandbox_kind(
    workspace_min: SandboxKind, policy_request: SandboxKind | None
) -> SandboxKind:
    """Return the STRONGER of the workspace minimum and the policy request.

    ``worktree < container < gvisor < microvm`` (``SANDBOX_KIND_RANK``). A policy
    may strengthen (workspace ``gvisor`` + policy ``microvm`` → ``microvm``) but
    never weaken below the workspace minimum (workspace ``gvisor`` + policy
    ``container`` → ``gvisor``). Default (both ``worktree``) → ``worktree``.
    """
    if policy_request is None:
        return workspace_min
    return max(workspace_min, policy_request, key=lambda k: SANDBOX_KIND_RANK[k])


def parse_memory_mb(value: str | None, default_mb: int) -> int:
    """Parse a docker-style memory string (``4g``/``512m``/``2048``) to MiB."""
    if value is None:
        return default_mb
    match = _MEMORY_RE.match(str(value))
    if not match:
        return default_mb
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "g":
        return int(amount * 1024)
    if unit == "k":
        return max(1, int(amount / 1024))
    # "m" or bare number → already MiB.
    return int(amount)


def _policy_kind(policy_block: PolicySandboxBlock | None) -> SandboxKind | None:
    if policy_block is None or policy_block.isolation is None:
        return None
    return SandboxKind(policy_block.isolation)


def _policy_network(
    policy_block: PolicySandboxBlock | None, default: SandboxNetwork
) -> SandboxNetwork:
    if policy_block is None or policy_block.network is None:
        return default
    return SandboxNetwork(policy_block.network)


def resolve_sandbox_settings(
    settings: SandboxSettings,
    policy_block: PolicySandboxBlock | None,
    *,
    language: str,
    agent_run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    host_worktree_path: str,
    worktree_subpath: str = "",
) -> SandboxSpec:
    """Merge workspace settings with a repo policy block into a ``SandboxSpec``.

    Policy values *strengthen* the workspace defaults; ``resolve_sandbox_kind``
    forbids downgrading the isolation kind. The resolved image is allowlist-checked
    (raises ``SandboxImageNotAllowed`` for a non-allow-listed ``policy.image``).
    """
    kind = resolve_sandbox_kind(settings.kind, _policy_kind(policy_block))
    network = _policy_network(policy_block, settings.network)

    limits = SandboxResourceLimits(
        cpus=(policy_block.cpus if policy_block and policy_block.cpus else settings.cpus),
        memory_mb=parse_memory_mb(
            policy_block.memory if policy_block else None, settings.memory_mb
        ),
        pids_limit=(
            policy_block.pids_limit
            if policy_block and policy_block.pids_limit
            else settings.pids_limit
        ),
        tmpfs_mb=(
            policy_block.tmpfs_mb if policy_block and policy_block.tmpfs_mb else settings.tmpfs_mb
        ),
    )

    egress_allowlist = list(
        (policy_block.egress_allowlist if policy_block and policy_block.egress_allowlist else [])
        or settings.egress_allowlist
    )
    setup_commands = list(policy_block.setup_commands) if policy_block else []
    exec_timeout = (
        policy_block.exec_timeout_seconds
        if policy_block and policy_block.exec_timeout_seconds
        else settings.exec_timeout_seconds
    )

    # gvisor/microvm sandboxes are containers under a different OCI runtime, so
    # they resolve (and allowlist-check) an image exactly like ``container``.
    image = (
        resolve_image(language, policy_block, settings) if kind in CONTAINER_BACKED_KINDS else None
    )

    runtime = _runtime_for(kind, settings)
    gvisor_platform = (
        (policy_block.gvisor_platform if policy_block else None) or settings.gvisor_platform
        if kind is SandboxKind.GVISOR
        else None
    )
    vm_vcpus, vm_memory_mb = (None, None)
    if kind is SandboxKind.MICROVM:
        vm_vcpus = (
            policy_block.vm_vcpus if policy_block and policy_block.vm_vcpus else None
        ) or settings.microvm_vcpus
        policy_vm_memory = policy_block.vm_memory if policy_block else None
        vm_memory_mb = (
            parse_memory_mb(policy_vm_memory, 0) if policy_vm_memory else None
        ) or settings.microvm_memory_mb

    return SandboxSpec(
        agent_run_id=agent_run_id,
        workspace_id=workspace_id,
        kind=kind,
        host_worktree_path=host_worktree_path,
        worktree_volume=settings.worktree_volume,
        worktree_subpath=worktree_subpath,
        image=image,
        network=network,
        egress_allowlist=egress_allowlist,
        limits=limits,
        setup_commands=setup_commands,
        exec_timeout_seconds=exec_timeout,
        run_as_uid=settings.run_uid,
        run_as_gid=settings.run_gid,
        runtime=runtime,
        gvisor_platform=gvisor_platform,
        vm_vcpus=vm_vcpus,
        vm_memory_mb=vm_memory_mb,
    )


def _runtime_for(kind: SandboxKind, settings: SandboxSettings) -> str | None:
    """The OCI runtime realising ``kind`` (settings override the defaults)."""
    if kind is SandboxKind.GVISOR:
        return settings.gvisor_runtime
    if kind is SandboxKind.MICROVM:
        return settings.microvm_runtime
    return DEFAULT_RUNTIMES[kind]


__all__ = ["parse_memory_mb", "resolve_sandbox_kind", "resolve_sandbox_settings"]
