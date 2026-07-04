"""Env-bound sandbox configuration (F19 ``SandboxSettings``).

A plain (dependency-free) settings model with a :meth:`from_env` constructor so
the worker can build it from ``os.environ`` without adding ``pydantic-settings``.
Defaults mirror the slice's ``FORGE_SANDBOX_*`` env table. ``FORGE_SANDBOX_KIND``
is the workspace **minimum** isolation; a repo policy may strengthen but never
weaken it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from forge_contracts import SandboxKind, SandboxNetwork

# Default per-language image pins. Production overrides these with @sha256 digest
# pins via env (digest resolution needs registry access, unavailable offline).
DEFAULT_IMAGE_PYTHON = "ghcr.io/forge-platform/forge-sandbox-python:0.1.0"
DEFAULT_IMAGE_NODE = "ghcr.io/forge-platform/forge-sandbox-node:0.1.0"
DEFAULT_IMAGE_GO = "ghcr.io/forge-platform/forge-sandbox-go:0.1.0"

DEFAULT_EGRESS_ALLOWLIST = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class SandboxSettings(BaseModel):
    """Resolved ``FORGE_SANDBOX_*`` configuration (workspace defaults)."""

    model_config = ConfigDict(frozen=True)

    kind: SandboxKind = SandboxKind.WORKTREE
    docker_host: str = "tcp://docker-proxy:2375"
    image_python: str = DEFAULT_IMAGE_PYTHON
    image_node: str = DEFAULT_IMAGE_NODE
    image_go: str = DEFAULT_IMAGE_GO
    allowed_images: tuple[str, ...] = ()
    worktree_volume: str = "forge_repos"
    cpus: float = 2.0
    memory_mb: int = 4096
    pids_limit: int = 512
    tmpfs_mb: int = 1024
    network: SandboxNetwork = SandboxNetwork.NONE
    egress_network: str = "forge_sandbox_egress"
    egress_allowlist: tuple[str, ...] = DEFAULT_EGRESS_ALLOWLIST
    exec_timeout_seconds: int = 1800
    output_cap_bytes: int = 262144
    run_uid: int = 10001
    run_gid: int = 10001
    reap_interval_seconds: int = 300
    max_ttl_seconds: int = 21600
    # --- F34 kernel-boundary runtimes ---
    gvisor_runtime: str = "runsc"
    gvisor_platform: str = "systrap"  # systrap | kvm | ptrace
    microvm_runtime: str = "kata-fc"
    microvm_vcpus: int | None = None  # None => derive from limits.cpus
    microvm_memory_mb: int | None = None  # None => derive from limits.memory_mb
    require_kvm: bool = True
    jailer_root: str = "/var/lib/forge/jailer"

    def resolved_allowed_images(self) -> tuple[str, ...]:
        """The allowlist, defaulting to the three per-language images."""
        if self.allowed_images:
            return self.allowed_images
        return (self.image_python, self.image_node, self.image_go)

    def image_for(self, language: str | None) -> str:
        """The default sandbox image for ``language`` (python fallback)."""
        lang = (language or "python").strip().lower()
        if lang in {"node", "nodejs", "javascript", "typescript", "ts", "js"}:
            return self.image_node
        if lang in {"go", "golang"}:
            return self.image_go
        return self.image_python

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SandboxSettings:
        """Build settings from ``FORGE_SANDBOX_*`` env (defaults when unset)."""
        e = os.environ if env is None else env
        defaults = cls()
        return cls(
            kind=SandboxKind(e.get("FORGE_SANDBOX_KIND", defaults.kind.value)),
            docker_host=e.get("FORGE_SANDBOX_DOCKER_HOST", defaults.docker_host),
            image_python=e.get("FORGE_SANDBOX_IMAGE_PYTHON", defaults.image_python),
            image_node=e.get("FORGE_SANDBOX_IMAGE_NODE", defaults.image_node),
            image_go=e.get("FORGE_SANDBOX_IMAGE_GO", defaults.image_go),
            allowed_images=tuple(_split_csv(e.get("FORGE_SANDBOX_ALLOWED_IMAGES"))),
            worktree_volume=e.get("FORGE_SANDBOX_WORKTREE_VOLUME", defaults.worktree_volume),
            cpus=float(e.get("FORGE_SANDBOX_CPUS", defaults.cpus)),
            memory_mb=int(e.get("FORGE_SANDBOX_MEMORY_MB", defaults.memory_mb)),
            pids_limit=int(e.get("FORGE_SANDBOX_PIDS_LIMIT", defaults.pids_limit)),
            tmpfs_mb=int(e.get("FORGE_SANDBOX_TMPFS_MB", defaults.tmpfs_mb)),
            network=SandboxNetwork(e.get("FORGE_SANDBOX_NETWORK", defaults.network.value)),
            egress_network=e.get("FORGE_SANDBOX_EGRESS_NETWORK", defaults.egress_network),
            egress_allowlist=tuple(
                _split_csv(e.get("FORGE_SANDBOX_EGRESS_ALLOWLIST"))
                or DEFAULT_EGRESS_ALLOWLIST
            ),
            exec_timeout_seconds=int(
                e.get("FORGE_SANDBOX_EXEC_TIMEOUT_SECONDS", defaults.exec_timeout_seconds)
            ),
            output_cap_bytes=int(
                e.get("FORGE_SANDBOX_OUTPUT_CAP_BYTES", defaults.output_cap_bytes)
            ),
            run_uid=int(e.get("FORGE_SANDBOX_RUN_UID", defaults.run_uid)),
            run_gid=int(e.get("FORGE_SANDBOX_RUN_GID", defaults.run_gid)),
            reap_interval_seconds=int(
                e.get("FORGE_SANDBOX_REAP_INTERVAL_SECONDS", defaults.reap_interval_seconds)
            ),
            max_ttl_seconds=int(e.get("FORGE_SANDBOX_MAX_TTL_SECONDS", defaults.max_ttl_seconds)),
            gvisor_runtime=e.get("FORGE_SANDBOX_GVISOR_RUNTIME", defaults.gvisor_runtime),
            gvisor_platform=e.get("FORGE_SANDBOX_GVISOR_PLATFORM", defaults.gvisor_platform),
            microvm_runtime=e.get("FORGE_SANDBOX_MICROVM_RUNTIME", defaults.microvm_runtime),
            microvm_vcpus=(
                int(v) if (v := e.get("FORGE_SANDBOX_MICROVM_VCPUS")) else defaults.microvm_vcpus
            ),
            microvm_memory_mb=(
                int(v)
                if (v := e.get("FORGE_SANDBOX_MICROVM_MEMORY_MB"))
                else defaults.microvm_memory_mb
            ),
            require_kvm=_env_bool(e.get("FORGE_SANDBOX_REQUIRE_KVM"), defaults.require_kvm),
            jailer_root=e.get("FORGE_SANDBOX_JAILER_ROOT", defaults.jailer_root),
        )

    def to_limits(self) -> dict[str, float | int]:
        """Resource-limit snapshot for ``SandboxResourceLimits``/audit."""
        return {
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "pids_limit": self.pids_limit,
            "tmpfs_mb": self.tmpfs_mb,
        }


__all__ = ["SandboxSettings"]
