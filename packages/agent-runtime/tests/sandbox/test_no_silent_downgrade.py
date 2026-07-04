"""F34 AC14 — a resolved kernel kind with an unavailable runtime fails LOUDLY.

Execution never falls back to runc, worktree, or a host subprocess: ``create``
raises ``SandboxRuntimeUnavailable`` (a ``SandboxStartupError``, which F19
already maps to a terminal/awaiting-input run), and the factory raises at
worker boot when the workspace minimum's preflight fails.
"""

from __future__ import annotations

import pytest
from _sandbox_fakes import FakeDockerClient

from forge_agent.sandbox import (
    GvisorSandboxProvider,
    MicroVMSandboxProvider,
    SandboxRuntimeUnavailable,
    SandboxSettings,
    build_sandbox_provider,
)
from forge_agent.sandbox import microvm as microvm_module
from forge_contracts import SandboxKind


async def test_gvisor_create_raises_never_falls_back(fake_docker_client, gvisor_spec) -> None:
    fake_docker_client.registered_runtimes = ("runc",)  # runsc missing at run time
    provider = GvisorSandboxProvider(client=fake_docker_client)
    with pytest.raises(SandboxRuntimeUnavailable):
        await provider.create(gvisor_spec)
    # No container was created under a weaker runtime.
    assert fake_docker_client.created == []
    assert fake_docker_client.create_kwargs is None


async def test_microvm_create_raises_without_kvm(
    monkeypatch, fake_docker_client, microvm_spec
) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: False)
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    provider = MicroVMSandboxProvider(client=fake_docker_client)
    with pytest.raises(SandboxRuntimeUnavailable):
        await provider.create(microvm_spec)
    assert fake_docker_client.created == []


async def test_microvm_create_raises_when_runtime_missing(
    monkeypatch, fake_docker_client, microvm_spec
) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: True)
    fake_docker_client.registered_runtimes = ("runc",)
    with pytest.raises(SandboxRuntimeUnavailable):
        await MicroVMSandboxProvider(client=fake_docker_client).create(microvm_spec)
    assert fake_docker_client.created == []


def test_factory_boot_failure_names_the_missing_runtime() -> None:
    client = FakeDockerClient(registered_runtimes=("runc",))
    with pytest.raises(SandboxRuntimeUnavailable, match="runsc"):
        build_sandbox_provider(SandboxSettings(kind=SandboxKind.GVISOR), docker_client=client)


def test_factory_boot_failure_names_kvm(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: False)
    client = FakeDockerClient(registered_runtimes=("runc", "kata-fc"))
    with pytest.raises(SandboxRuntimeUnavailable, match="/dev/kvm"):
        build_sandbox_provider(SandboxSettings(kind=SandboxKind.MICROVM), docker_client=client)
