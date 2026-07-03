"""Sandbox provider factory (F19, extended by F34).

``build_sandbox_provider`` returns the provider matching the workspace minimum
isolation: the host-subprocess :class:`LocalSandboxProvider` for ``worktree``,
the :class:`ContainerSandboxProvider` for ``container``, and the F34
kernel-boundary providers for ``gvisor`` (:class:`GvisorSandboxProvider`,
``runsc``) and ``microvm`` (:class:`MicroVMSandboxProvider`, ``kata-fc``). The
container-backed providers point at ``DOCKER_HOST=tcp://docker-proxy:2375``
(never the raw socket).

When the workspace minimum is a kernel-boundary kind, the provider's
``preflight`` runs **at construction** so a missing runtime / missing KVM fails
at worker boot with :class:`SandboxRuntimeUnavailable` — not mid-run, and never
via a silent downgrade.
"""

from __future__ import annotations

from typing import Any

from forge_agent.sandbox.base import ArtifactStore
from forge_agent.sandbox.container import ContainerSandboxProvider
from forge_agent.sandbox.gvisor import GvisorSandboxProvider
from forge_agent.sandbox.local import LocalSandboxProvider
from forge_agent.sandbox.microvm import MicroVMSandboxProvider
from forge_agent.sandbox.settings import SandboxSettings
from forge_contracts import SandboxKind

#: Kinds whose provider preflight runs at worker boot (fail fast, never mid-run).
KERNEL_BOUNDARY_KINDS = frozenset({SandboxKind.GVISOR, SandboxKind.MICROVM})


def build_sandbox_provider(
    settings: SandboxSettings,
    *,
    artifact_store: ArtifactStore | None = None,
    docker_client: Any = None,
    run_preflight: bool = True,
) -> LocalSandboxProvider | ContainerSandboxProvider:
    """Construct the provider for the workspace minimum isolation kind.

    For ``gvisor``/``microvm`` the provider preflight runs immediately (unless
    ``run_preflight=False``), raising ``SandboxRuntimeUnavailable`` when the OCI
    runtime is unregistered or (microvm) ``/dev/kvm`` is absent.
    """
    common: dict[str, Any] = {
        "docker_host": settings.docker_host,
        "client": docker_client,
        "egress_network": settings.egress_network,
        "artifact_store": artifact_store,
        "output_cap_bytes": settings.output_cap_bytes,
        "max_ttl_seconds": settings.max_ttl_seconds,
    }
    provider: LocalSandboxProvider | ContainerSandboxProvider
    if settings.kind is SandboxKind.GVISOR:
        provider = GvisorSandboxProvider(
            gvisor_runtime=settings.gvisor_runtime,
            gvisor_platform=settings.gvisor_platform,
            **common,
        )
    elif settings.kind is SandboxKind.MICROVM:
        provider = MicroVMSandboxProvider(
            microvm_runtime=settings.microvm_runtime,
            require_kvm=settings.require_kvm,
            jailer_root=settings.jailer_root,
            default_vcpus=settings.microvm_vcpus,
            default_memory_mb=settings.microvm_memory_mb,
            **common,
        )
    elif settings.kind is SandboxKind.CONTAINER:
        provider = ContainerSandboxProvider(**common)
    else:
        return LocalSandboxProvider(
            artifact_store=artifact_store,
            output_cap_bytes=settings.output_cap_bytes,
        )
    if run_preflight and settings.kind in KERNEL_BOUNDARY_KINDS:
        provider.preflight_check()
    return provider


__all__ = ["KERNEL_BOUNDARY_KINDS", "build_sandbox_provider"]
