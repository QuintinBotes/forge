"""Sandbox provider factory (F19).

``build_sandbox_provider`` returns the provider matching the workspace minimum
isolation: the host-subprocess :class:`LocalSandboxProvider` for ``worktree`` and
the :class:`ContainerSandboxProvider` for ``container``. The container provider
points at ``DOCKER_HOST=tcp://docker-proxy:2375`` (never the raw socket).
"""

from __future__ import annotations

from typing import Any

from forge_agent.sandbox.base import ArtifactStore
from forge_agent.sandbox.container import ContainerSandboxProvider
from forge_agent.sandbox.local import LocalSandboxProvider
from forge_agent.sandbox.settings import SandboxSettings
from forge_contracts import SandboxKind


def build_sandbox_provider(
    settings: SandboxSettings,
    *,
    artifact_store: ArtifactStore | None = None,
    docker_client: Any = None,
) -> LocalSandboxProvider | ContainerSandboxProvider:
    """Construct the provider for the workspace minimum isolation kind."""
    if settings.kind is SandboxKind.CONTAINER:
        return ContainerSandboxProvider(
            docker_host=settings.docker_host,
            client=docker_client,
            egress_network=settings.egress_network,
            artifact_store=artifact_store,
            output_cap_bytes=settings.output_cap_bytes,
            max_ttl_seconds=settings.max_ttl_seconds,
        )
    return LocalSandboxProvider(
        artifact_store=artifact_store,
        output_cap_bytes=settings.output_cap_bytes,
    )


__all__ = ["build_sandbox_provider"]
