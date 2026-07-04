"""Firecracker microVM sandbox provider via Kata Containers ``kata-fc`` (F34).

``MicroVMSandboxProvider`` subclasses F19's :class:`ContainerSandboxProvider`:
the container simply runs inside a Firecracker microVM with its **own guest
kernel** (hardware-virtualized isolation). The provider differs from the base
only in the OCI runtime selected at create (``kata-fc``), Kata vCPU/memory
sizing annotations derived from :class:`SandboxResourceLimits` (or the policy
``vm_vcpus``/``vm_memory`` overrides), a preflight that requires the runtime
registered **and** ``/dev/kvm`` on the daemon host, post-start capture of the
guest kernel version + boot latency (proof of a separate kernel), and a
VM-artifact sweep (orphaned jailer chroots) layered onto teardown/reaping.

No silent downgrade: a missing runtime or missing KVM raises
:class:`SandboxRuntimeUnavailable` — execution never falls back to runc,
gVisor, or a host subprocess.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

from forge_agent.sandbox.base import SandboxRuntimeUnavailable
from forge_agent.sandbox.container import (
    SANDBOX_LABEL,
    ContainerSandboxProvider,
    ContainerSandboxSession,
)
from forge_agent.sandbox.runtime import detect_registered_runtimes
from forge_contracts import SandboxKind, SandboxSpec

DEFAULT_MICROVM_RUNTIME = "kata-fc"
DEFAULT_JAILER_ROOT = "/var/lib/forge/jailer"
DEFAULT_KVM_DEVICE = "/dev/kvm"

#: Kata annotation keys for per-sandbox hypervisor sizing.
KATA_VCPUS_ANNOTATION = "io.katacontainers.config.hypervisor.default_vcpus"
KATA_MEMORY_ANNOTATION = "io.katacontainers.config.hypervisor.default_memory"


def kvm_present(device: str = DEFAULT_KVM_DEVICE) -> bool:
    """Whether the KVM device node exists (patchable in tests)."""
    return os.path.exists(device)


class MicroVMSandboxSession(ContainerSandboxSession):
    """A container session running inside a Firecracker microVM (Kata)."""

    def __init__(self, *args: Any, jailer_root: str = DEFAULT_JAILER_ROOT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._jailer_root = jailer_root

    async def teardown(self, *, reason: str = "completed") -> None:
        await super().teardown(reason=reason)
        # The Kata shim tears the VM + virtiofsd down with the container; this
        # backstops any leftover jailer chroot for THIS run (best-effort).
        sweep_jailer_chroots(self._jailer_root, {str(self._spec.agent_run_id)})


class MicroVMSandboxProvider(ContainerSandboxProvider):
    """Per-task Docker container under Kata Containers + Firecracker."""

    kind = SandboxKind.MICROVM

    def __init__(
        self,
        *,
        microvm_runtime: str = DEFAULT_MICROVM_RUNTIME,
        require_kvm: bool = True,
        kvm_device: str = DEFAULT_KVM_DEVICE,
        jailer_root: str = DEFAULT_JAILER_ROOT,
        default_vcpus: int | None = None,
        default_memory_mb: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._microvm_runtime = microvm_runtime
        self._require_kvm = require_kvm
        self._kvm_device = kvm_device
        self._jailer_root = jailer_root
        self._default_vcpus = default_vcpus
        self._default_memory_mb = default_memory_mb

    @property
    def runtime(self) -> str:
        return self._microvm_runtime

    def preflight_check(self) -> None:
        """Require ``kata-fc`` registered AND ``/dev/kvm`` — never downgrade."""
        registered = detect_registered_runtimes(self._ensure_client())
        if self._microvm_runtime not in registered:
            raise SandboxRuntimeUnavailable(
                f"microvm sandbox requires OCI runtime {self._microvm_runtime!r} "
                f"registered with the Docker daemon (found: {sorted(registered) or 'none'}); "
                "run deploy/scripts/install-runtimes.sh --firecracker on the daemon host"
            )
        if self._require_kvm and not kvm_present(self._kvm_device):
            raise SandboxRuntimeUnavailable(
                f"microvm sandbox requires {self._kvm_device} on the daemon host "
                "(bare metal, or a cloud VM with nested virtualization enabled)"
            )

    def _vm_vcpus(self, spec: SandboxSpec) -> int:
        if spec.vm_vcpus:
            return spec.vm_vcpus
        if self._default_vcpus:
            return self._default_vcpus
        return max(1, int(spec.limits.cpus))

    def _vm_memory_mb(self, spec: SandboxSpec) -> int:
        if spec.vm_memory_mb:
            return spec.vm_memory_mb
        if self._default_memory_mb:
            return self._default_memory_mb
        return spec.limits.memory_mb

    def _create_kwargs(self, spec: SandboxSpec) -> dict[str, Any]:
        kw = super()._create_kwargs(spec)
        kw["runtime"] = spec.runtime or self._microvm_runtime
        kw["labels"] = {
            **kw.get("labels", {}),
            KATA_VCPUS_ANNOTATION: str(self._vm_vcpus(spec)),
            KATA_MEMORY_ANNOTATION: str(self._vm_memory_mb(spec)),
        }
        return kw

    def _build_session(
        self, spec: SandboxSpec, *, client: Any, container: Any
    ) -> MicroVMSandboxSession:
        return MicroVMSandboxSession(
            spec,
            client=client,
            container=container,
            artifact_store=self._artifact_store,
            output_cap_bytes=self._output_cap_bytes,
            jailer_root=self._jailer_root,
        )

    async def create(self, spec: SandboxSpec) -> ContainerSandboxSession:
        self.preflight_check()
        started = time.monotonic()
        session = await super().create(spec)
        session.boot_ms = int((time.monotonic() - started) * 1000)
        session.runtime = spec.runtime or self._microvm_runtime
        # Proof of a separate kernel: `uname -r` INSIDE the microVM returns the
        # guest kernel, not the host's (best-effort; never fails the create).
        with contextlib.suppress(Exception):
            out = await session.run("uname -r", cwd=session.workspace_dir, timeout_s=60)
            if out.exit_code == 0 and out.stdout.strip():
                session.guest_kernel_version = out.stdout.strip().splitlines()[0]
        return session

    async def reap_orphans(self, *, terminal_run_ids: set[str] | None = None) -> int:
        removed = await super().reap_orphans(terminal_run_ids=terminal_run_ids)
        # Post-reap VM-artifact sweep: remove any jailer chroot whose run no
        # longer has a live sandbox container (crashed runs can leak them).
        with contextlib.suppress(Exception):
            live = self._live_run_ids()
            sweep_orphaned_jailer_chroots(self._jailer_root, live_run_ids=live)
        return removed

    def _live_run_ids(self) -> set[str]:
        client = self._ensure_client()
        containers = client.containers.list(all=True, filters={"label": f"{SANDBOX_LABEL}=true"})
        ids: set[str] = set()
        for container in containers:
            labels = (getattr(container, "attrs", {}) or {}).get("Config", {}).get("Labels", {})
            run_id = (labels or {}).get("forge.agent_run_id")
            if run_id:
                ids.add(str(run_id))
        return ids


def sweep_jailer_chroots(jailer_root: str, run_ids: set[str]) -> int:
    """Remove the jailer chroot dirs for the given run ids; return count removed."""
    root = Path(jailer_root)
    if not root.is_dir():
        return 0
    removed = 0
    for run_id in run_ids:
        target = root / run_id
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    return removed


def sweep_orphaned_jailer_chroots(jailer_root: str, *, live_run_ids: set[str]) -> int:
    """Remove every jailer chroot whose run id has no live sandbox container."""
    root = Path(jailer_root)
    if not root.is_dir():
        return 0
    orphans = {entry.name for entry in root.iterdir() if entry.is_dir()} - live_run_ids
    return sweep_jailer_chroots(jailer_root, orphans)


__all__ = [
    "DEFAULT_JAILER_ROOT",
    "DEFAULT_KVM_DEVICE",
    "DEFAULT_MICROVM_RUNTIME",
    "KATA_MEMORY_ANNOTATION",
    "KATA_VCPUS_ANNOTATION",
    "MicroVMSandboxProvider",
    "MicroVMSandboxSession",
    "kvm_present",
    "sweep_jailer_chroots",
    "sweep_orphaned_jailer_chroots",
]
