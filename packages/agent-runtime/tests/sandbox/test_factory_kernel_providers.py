"""F34 AC2/AC14 — factory returns the kernel providers; preflight runs at boot."""

from __future__ import annotations

import pytest
from _sandbox_fakes import FakeDockerClient

from forge_agent.sandbox import (
    ContainerSandboxProvider,
    GvisorSandboxProvider,
    LocalSandboxProvider,
    MicroVMSandboxProvider,
    SandboxRuntimeUnavailable,
    SandboxSettings,
    build_sandbox_provider,
)
from forge_agent.sandbox import microvm as microvm_module
from forge_contracts import SandboxKind


def test_factory_returns_gvisor_provider() -> None:
    client = FakeDockerClient(registered_runtimes=("runc", "runsc"))
    provider = build_sandbox_provider(
        SandboxSettings(kind=SandboxKind.GVISOR), docker_client=client
    )
    assert isinstance(provider, GvisorSandboxProvider)
    assert provider.kind is SandboxKind.GVISOR
    assert provider.runtime == "runsc"


def test_factory_returns_microvm_provider(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: True)
    client = FakeDockerClient(registered_runtimes=("runc", "kata-fc"))
    provider = build_sandbox_provider(
        SandboxSettings(kind=SandboxKind.MICROVM), docker_client=client
    )
    assert isinstance(provider, MicroVMSandboxProvider)
    assert provider.kind is SandboxKind.MICROVM
    assert provider.runtime == "kata-fc"


def test_factory_f19_kinds_unchanged() -> None:
    assert isinstance(
        build_sandbox_provider(SandboxSettings(kind=SandboxKind.WORKTREE)),
        LocalSandboxProvider,
    )
    container = build_sandbox_provider(
        SandboxSettings(kind=SandboxKind.CONTAINER), docker_client=FakeDockerClient()
    )
    assert type(container) is ContainerSandboxProvider


def test_factory_honours_runtime_name_overrides(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: True)
    client = FakeDockerClient(registered_runtimes=("runsc-custom", "kata-qemu"))
    gvisor = build_sandbox_provider(
        SandboxSettings(kind=SandboxKind.GVISOR, gvisor_runtime="runsc-custom"),
        docker_client=client,
    )
    assert gvisor.runtime == "runsc-custom"
    microvm = build_sandbox_provider(
        SandboxSettings(kind=SandboxKind.MICROVM, microvm_runtime="kata-qemu"),
        docker_client=client,
    )
    assert microvm.runtime == "kata-qemu"


def test_factory_preflight_fails_at_boot_when_runtime_missing() -> None:
    """AC14 — misconfiguration fails at worker boot, not mid-run."""
    client = FakeDockerClient(registered_runtimes=("runc",))  # no runsc
    with pytest.raises(SandboxRuntimeUnavailable):
        build_sandbox_provider(SandboxSettings(kind=SandboxKind.GVISOR), docker_client=client)


def test_factory_preflight_fails_at_boot_without_kvm(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: False)
    client = FakeDockerClient(registered_runtimes=("runc", "kata-fc"))
    with pytest.raises(SandboxRuntimeUnavailable):
        build_sandbox_provider(SandboxSettings(kind=SandboxKind.MICROVM), docker_client=client)


def test_factory_preflight_not_run_for_f19_kinds() -> None:
    # No client injected and no daemon reachable: container/worktree construction
    # must still succeed (F19 behaviour — preflight only gates kernel kinds).
    build_sandbox_provider(SandboxSettings(kind=SandboxKind.CONTAINER))
    build_sandbox_provider(SandboxSettings(kind=SandboxKind.WORKTREE))
