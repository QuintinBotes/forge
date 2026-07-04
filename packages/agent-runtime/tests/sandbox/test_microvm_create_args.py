"""F34 AC4/AC5 — microVM create body: runtime=kata-fc + sizing annotations."""

from __future__ import annotations

import pytest
from _sandbox_fakes import FakeDockerClient, FakeExecResult

from forge_agent.sandbox import MicroVMSandboxProvider
from forge_agent.sandbox import microvm as microvm_module
from forge_agent.sandbox.microvm import (
    KATA_MEMORY_ANNOTATION,
    KATA_VCPUS_ANNOTATION,
)
from forge_contracts import SandboxKind


@pytest.fixture(autouse=True)
def _kvm(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: True)


def _provider(client: FakeDockerClient, **kwargs) -> MicroVMSandboxProvider:
    return MicroVMSandboxProvider(client=client, **kwargs)


async def test_create_selects_kata_fc_runtime(fake_docker_client, microvm_spec) -> None:
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    await _provider(fake_docker_client).create(microvm_spec)
    assert fake_docker_client.create_kwargs["runtime"] == "kata-fc"


async def test_sizing_annotations_derived_from_limits(fake_docker_client, microvm_spec) -> None:
    """AC4 — vcpu/memory annotations default from SandboxResourceLimits."""
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    await _provider(fake_docker_client).create(microvm_spec)
    labels = fake_docker_client.create_kwargs["labels"]
    assert labels[KATA_VCPUS_ANNOTATION] == "2"  # limits.cpus=2.0
    assert labels[KATA_MEMORY_ANNOTATION] == "4096"  # limits.memory_mb


async def test_sizing_annotations_policy_overrides(fake_docker_client, microvm_spec) -> None:
    """AC4 — policy vm_vcpus/vm_memory overrides win over the limits defaults."""
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    spec = microvm_spec.model_copy(update={"vm_vcpus": 4, "vm_memory_mb": 8192})
    await _provider(fake_docker_client).create(spec)
    labels = fake_docker_client.create_kwargs["labels"]
    assert labels[KATA_VCPUS_ANNOTATION] == "4"
    assert labels[KATA_MEMORY_ANNOTATION] == "8192"


async def test_f19_hardening_matrix_preserved(fake_docker_client, microvm_spec) -> None:
    """AC5 — the full F19 hardening kwargs are inherited unchanged under kata-fc."""
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    await _provider(fake_docker_client).create(microvm_spec)
    kwargs = fake_docker_client.create_kwargs
    assert kwargs["user"] == "10001:10001"
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]
    assert kwargs["network_mode"] == "none"
    assert kwargs["labels"]["forge.sandbox"] == "true"
    mounts = kwargs["mounts"]
    assert len(mounts) == 1
    assert mounts[0]["Target"] == "/workspace"


async def test_guest_kernel_and_boot_ms_captured(fake_docker_client, microvm_spec) -> None:
    """AC7/AC16 (unit) — `uname -r` output is recorded as the guest kernel."""
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    fake_docker_client.exec_results = [
        FakeExecResult(exit_code=0, stdout=b"6.1.62-kata\n", stderr=b"")
    ]
    session = await _provider(fake_docker_client).create(microvm_spec)
    assert session.kind is SandboxKind.MICROVM
    assert session.runtime == "kata-fc"
    assert session.guest_kernel_version == "6.1.62-kata"
    assert session.boot_ms is not None and session.boot_ms >= 0


async def test_teardown_sweeps_this_runs_jailer_chroot(
    fake_docker_client, microvm_spec, tmp_path
) -> None:
    """AC17 (unit) — teardown removes the run's leftover jailer chroot."""
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    jailer_root = tmp_path / "jailer"
    leftover = jailer_root / str(microvm_spec.agent_run_id)
    leftover.mkdir(parents=True)
    (leftover / "rootfs.img").write_bytes(b"x")
    provider = _provider(fake_docker_client, jailer_root=str(jailer_root))
    session = await provider.create(microvm_spec)
    await session.teardown(reason="completed")
    assert fake_docker_client.created[0].removed is True
    assert not leftover.exists()


async def test_reap_sweeps_orphaned_jailer_chroots(fake_docker_client, tmp_path) -> None:
    """AC17 (unit) — reap removes chroots whose run has no live container."""
    fake_docker_client.registered_runtimes = ("runc", "kata-fc")
    jailer_root = tmp_path / "jailer"
    orphan = jailer_root / "11111111-1111-1111-1111-111111111111"
    orphan.mkdir(parents=True)
    provider = _provider(fake_docker_client, jailer_root=str(jailer_root))
    removed = await provider.reap_orphans(terminal_run_ids=set())
    assert removed == 0  # no containers to reap
    assert not orphan.exists()  # but the orphaned VM artifact is swept
