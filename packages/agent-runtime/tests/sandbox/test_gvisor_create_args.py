"""F34 AC3/AC5 — gVisor create body: runtime=runsc + inherited F19 hardening."""

from __future__ import annotations

from _sandbox_fakes import FakeDockerClient

from forge_agent.sandbox import GvisorSandboxProvider
from forge_contracts import SandboxKind


def _provider(client: FakeDockerClient) -> GvisorSandboxProvider:
    return GvisorSandboxProvider(client=client)


async def test_create_selects_runsc_runtime(fake_docker_client, gvisor_spec) -> None:
    fake_docker_client.registered_runtimes = ("runc", "runsc")
    await _provider(fake_docker_client).create(gvisor_spec)
    kwargs = fake_docker_client.create_kwargs
    assert kwargs["runtime"] == "runsc"
    assert kwargs["environment"]["GVISOR_PLATFORM"] == "systrap"


async def test_create_honours_spec_platform(fake_docker_client, gvisor_spec) -> None:
    fake_docker_client.registered_runtimes = ("runc", "runsc")
    spec = gvisor_spec.model_copy(update={"gvisor_platform": "kvm"})
    await _provider(fake_docker_client).create(spec)
    assert fake_docker_client.create_kwargs["environment"]["GVISOR_PLATFORM"] == "kvm"


async def test_f19_hardening_matrix_preserved(fake_docker_client, gvisor_spec) -> None:
    """AC5 — the full F19 hardening kwargs are inherited unchanged under runsc."""
    fake_docker_client.registered_runtimes = ("runc", "runsc")
    await _provider(fake_docker_client).create(gvisor_spec)
    kwargs = fake_docker_client.create_kwargs
    assert kwargs["user"] == "10001:10001"
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]
    assert kwargs["mem_limit"] == "4096m"
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["pids_limit"] == 512
    assert kwargs["network_mode"] == "none"
    assert kwargs["labels"]["forge.sandbox"] == "true"
    mounts = kwargs["mounts"]
    assert len(mounts) == 1
    assert mounts[0]["Target"] == "/workspace"
    assert mounts[0]["VolumeOptions"]["Subpath"] == "runA/tree"


async def test_session_reports_gvisor_kind_and_runtime(fake_docker_client, gvisor_spec) -> None:
    fake_docker_client.registered_runtimes = ("runc", "runsc")
    session = await _provider(fake_docker_client).create(gvisor_spec)
    assert session.kind is SandboxKind.GVISOR
    assert session.runtime == "runsc"
    out = await session.run("pytest -q", cwd="/workspace", timeout_s=30)
    assert out.sandbox_kind is SandboxKind.GVISOR
