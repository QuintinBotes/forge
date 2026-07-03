"""F34 AC12/AC13 — provider preflight gates: runtime registration + /dev/kvm."""

from __future__ import annotations

import pytest
from _sandbox_fakes import FakeDockerClient

from forge_agent.sandbox import (
    GvisorSandboxProvider,
    MicroVMSandboxProvider,
    SandboxRuntimeUnavailable,
    SandboxStartupError,
)
from forge_agent.sandbox import microvm as microvm_module


async def test_gvisor_preflight_raises_when_runsc_unregistered() -> None:
    """AC12 — no runsc in docker info Runtimes -> SandboxRuntimeUnavailable."""
    client = FakeDockerClient(registered_runtimes=("runc",))
    provider = GvisorSandboxProvider(client=client)
    with pytest.raises(SandboxRuntimeUnavailable, match="runsc"):
        await provider.preflight()


async def test_gvisor_preflight_passes_when_registered() -> None:
    client = FakeDockerClient(registered_runtimes=("runc", "runsc"))
    await GvisorSandboxProvider(client=client).preflight()


async def test_gvisor_preflight_error_names_the_installer() -> None:
    client = FakeDockerClient(registered_runtimes=("runc",))
    with pytest.raises(SandboxRuntimeUnavailable, match=r"install-runtimes\.sh"):
        await GvisorSandboxProvider(client=client).preflight()


async def test_microvm_preflight_raises_when_kata_unregistered(monkeypatch) -> None:
    """AC13 — kata-fc unregistered -> SandboxRuntimeUnavailable."""
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: True)
    client = FakeDockerClient(registered_runtimes=("runc", "runsc"))
    with pytest.raises(SandboxRuntimeUnavailable, match="kata-fc"):
        await MicroVMSandboxProvider(client=client).preflight()


async def test_microvm_preflight_raises_without_kvm(monkeypatch) -> None:
    """AC13 — /dev/kvm absent (require_kvm=True) -> SandboxRuntimeUnavailable."""
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: False)
    client = FakeDockerClient(registered_runtimes=("runc", "kata-fc"))
    with pytest.raises(SandboxRuntimeUnavailable, match="kvm"):
        await MicroVMSandboxProvider(client=client).preflight()


async def test_microvm_preflight_passes_with_runtime_and_kvm(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: True)
    client = FakeDockerClient(registered_runtimes=("runc", "kata-fc"))
    await MicroVMSandboxProvider(client=client).preflight()


async def test_microvm_preflight_kvm_not_required_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: False)
    client = FakeDockerClient(registered_runtimes=("runc", "kata-fc"))
    await MicroVMSandboxProvider(client=client, require_kvm=False).preflight()


async def test_runtime_unavailable_is_a_startup_error() -> None:
    """The F19 no-silent-downgrade mapping (SandboxStartupError) catches it."""
    assert issubclass(SandboxRuntimeUnavailable, SandboxStartupError)


async def test_f19_providers_preflight_is_noop() -> None:
    from forge_agent.sandbox import ContainerSandboxProvider, LocalSandboxProvider

    await LocalSandboxProvider().preflight()
    await ContainerSandboxProvider(client=FakeDockerClient()).preflight()
