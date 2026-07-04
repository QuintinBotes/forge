"""Real-runtime kernel-boundary integration tests (F34 — @pytest.mark.gvisor /
@pytest.mark.firecracker).

These exercise ACs 6-11 against a **live** daemon with the gVisor (``runsc``)
and Kata+Firecracker (``kata-fc``) OCI runtimes registered — a virtualization-
enabled CI tier (bare metal or nested virt). They are opt-in via
``FORGE_SANDBOX_DOCKER_TESTS=1`` + a curated image, and skip (parked, never
faked) when the runtime is unregistered or ``/dev/kvm`` is absent.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest

from forge_agent.sandbox import (
    GvisorSandboxProvider,
    MicroVMSandboxProvider,
    detect_registered_runtimes,
)
from forge_contracts import SandboxKind, SandboxSpec

_OPT_IN = os.environ.get("FORGE_SANDBOX_DOCKER_TESTS") == "1"
_SANDBOX_IMAGE = os.environ.get("FORGE_SANDBOX_IMAGE_PYTHON", "")

skip_unless_docker = pytest.mark.skipif(
    not (_OPT_IN and _SANDBOX_IMAGE),
    reason=(
        "PARKED: real-runtime kernel-boundary tests need FORGE_SANDBOX_DOCKER_TESTS=1 "
        "+ a curated FORGE_SANDBOX_IMAGE_PYTHON on a virtualization-enabled runner "
        "(runsc / kata-fc registered, /dev/kvm for microvm). Run in the "
        "virtualization-gated CI job to close."
    ),
)


def _live_client_or_skip(required_runtime: str):
    docker = pytest.importorskip("docker")
    client = docker.from_env()
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"docker daemon unreachable: {exc}")
    if required_runtime not in detect_registered_runtimes(client):
        pytest.skip(f"OCI runtime {required_runtime!r} not registered with the daemon")
    return client


@pytest.fixture
def real_gvisor():
    client = _live_client_or_skip("runsc")
    created: list[str] = []
    yield client, created
    for name in created:
        with contextlib.suppress(Exception):
            client.containers.get(name).remove(force=True)


@pytest.fixture
def real_firecracker():
    client = _live_client_or_skip("kata-fc")
    if not os.path.exists("/dev/kvm"):
        pytest.skip("/dev/kvm absent — microvm tier needs a KVM-capable host")
    created: list[str] = []
    yield client, created
    for name in created:
        with contextlib.suppress(Exception):
            client.containers.get(name).remove(force=True)


def _spec(kind: SandboxKind, runtime: str, subpath: str = "") -> SandboxSpec:
    return SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=kind,
        host_worktree_path="/workspace",
        worktree_volume="forge_repos",
        worktree_subpath=subpath,
        image=_SANDBOX_IMAGE,
        runtime=runtime,
    )


@pytest.mark.gvisor
@skip_unless_docker
async def test_gvisor_inspect_runtime(real_gvisor) -> None:
    """AC6 — HostConfig.Runtime == runsc on the live container."""
    client, created = real_gvisor
    provider = GvisorSandboxProvider(client=client)
    spec = _spec(SandboxKind.GVISOR, "runsc")
    session = await provider.create(spec)
    created.append(f"forge-sbx-{spec.agent_run_id}")
    info = client.containers.get(session.sandbox_id).attrs
    assert info["HostConfig"]["Runtime"] == "runsc"
    await session.teardown(reason="completed")


@pytest.mark.gvisor
@skip_unless_docker
async def test_gvisor_kernel_boundary(real_gvisor) -> None:
    """AC6 — /proc/version identifies gVisor's userspace kernel."""
    client, created = real_gvisor
    provider = GvisorSandboxProvider(client=client)
    spec = _spec(SandboxKind.GVISOR, "runsc")
    session = await provider.create(spec)
    created.append(f"forge-sbx-{spec.agent_run_id}")
    out = await session.run("cat /proc/version", cwd="/workspace", timeout_s=30)
    assert out.exit_code == 0
    assert "gVisor" in out.stdout
    await session.teardown(reason="completed")


@pytest.mark.firecracker
@skip_unless_docker
async def test_microvm_inspect_runtime(real_firecracker) -> None:
    """AC7 — HostConfig.Runtime == kata-fc on the live container."""
    client, created = real_firecracker
    provider = MicroVMSandboxProvider(client=client)
    spec = _spec(SandboxKind.MICROVM, "kata-fc")
    session = await provider.create(spec)
    created.append(f"forge-sbx-{spec.agent_run_id}")
    info = client.containers.get(session.sandbox_id).attrs
    assert info["HostConfig"]["Runtime"] == "kata-fc"
    await session.teardown(reason="completed")


@pytest.mark.firecracker
@skip_unless_docker
async def test_microvm_separate_kernel(real_firecracker) -> None:
    """AC7 — the guest `uname -r` differs from the host kernel."""
    client, created = real_firecracker
    provider = MicroVMSandboxProvider(client=client)
    spec = _spec(SandboxKind.MICROVM, "kata-fc")
    session = await provider.create(spec)
    created.append(f"forge-sbx-{spec.agent_run_id}")
    assert session.guest_kernel_version, "guest kernel must be captured post-start"
    assert session.guest_kernel_version != os.uname().release
    await session.teardown(reason="completed")
