"""F34 AC5 — F19 behavioural suite parametrized over runc/runsc/kata-fc.

The kernel-boundary providers inherit F19's exec/timeout/OOM/capping/teardown
machinery; this asserts there is **no behavioural change** at the seam when the
OCI runtime is swapped (unit tier via the fake client; the real-runtime tier
lives in ``test_kernel_integration.py``).
"""

from __future__ import annotations

import shlex
import uuid

import pytest
from _sandbox_fakes import FakeDockerClient, FakeExecResult

from forge_agent.sandbox import (
    ContainerSandboxProvider,
    GvisorSandboxProvider,
    MicroVMSandboxProvider,
    reap_orphans,
)
from forge_agent.sandbox import microvm as microvm_module
from forge_contracts import SandboxKind, SandboxResourceLimits, SandboxSpec

RUNTIME_MATRIX = [
    ("runc", SandboxKind.CONTAINER),
    ("runsc", SandboxKind.GVISOR),
    ("kata-fc", SandboxKind.MICROVM),
]


@pytest.fixture(autouse=True)
def _kvm(monkeypatch) -> None:
    monkeypatch.setattr(microvm_module, "kvm_present", lambda device: True)


def _spec(kind: SandboxKind, runtime: str) -> SandboxSpec:
    return SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=kind,
        host_worktree_path="/srv/worktrees/runA/tree",
        worktree_volume="forge_repos",
        worktree_subpath="runA/tree",
        image="ghcr.io/forge-platform/forge-sandbox-python:0.1.0",
        limits=SandboxResourceLimits(cpus=2.0, memory_mb=4096, pids_limit=512, tmpfs_mb=1024),
        runtime=None if kind is SandboxKind.CONTAINER else runtime,
    )


def _provider(kind: SandboxKind, client: FakeDockerClient):
    client.registered_runtimes = ("runc", "runsc", "kata-fc")
    if kind is SandboxKind.GVISOR:
        return GvisorSandboxProvider(client=client)
    if kind is SandboxKind.MICROVM:
        return MicroVMSandboxProvider(client=client)
    return ContainerSandboxProvider(client=client)


@pytest.mark.parametrize(("runtime", "kind"), RUNTIME_MATRIX)
async def test_exec_shape_identical_across_runtimes(runtime, kind) -> None:
    """The literal-command exec wrapper is byte-identical across runtimes."""
    client = FakeDockerClient()
    session = await _provider(kind, client).create(_spec(kind, runtime))
    client.exec_create_calls.clear()  # ignore any provider post-start probes
    await session.run("pytest -q", cwd="/workspace", timeout_s=30)
    call = client.exec_create_calls[0]
    quoted = shlex.quote("pytest -q")
    assert call["cmd"] == ["/bin/sh", "-lc", f"timeout --kill-after=10s 30s sh -lc {quoted}"]
    assert call["kwargs"]["user"] == "10001:10001"
    assert call["kwargs"]["workdir"] == "/workspace"


@pytest.mark.parametrize(("runtime", "kind"), RUNTIME_MATRIX)
async def test_timeout_semantics_identical(runtime, kind) -> None:
    """exit 124 => timed_out under every runtime."""
    client = FakeDockerClient()
    session = await _provider(kind, client).create(_spec(kind, runtime))
    client.exec_results = [FakeExecResult(exit_code=124)]
    client._exec_idx = 0
    out = await session.run("sleep 999", cwd="/workspace", timeout_s=1)
    assert out.timed_out is True
    assert out.exit_code == 124


@pytest.mark.parametrize(("runtime", "kind"), RUNTIME_MATRIX)
async def test_oom_semantics_identical(runtime, kind) -> None:
    client = FakeDockerClient()
    session = await _provider(kind, client).create(_spec(kind, runtime))
    client.created[0].attrs = {"State": {"OOMKilled": True, "Status": "running"}}
    client.exec_results = [FakeExecResult(exit_code=137)]
    client._exec_idx = 0
    out = await session.run("python eat_ram.py", cwd="/workspace", timeout_s=30)
    assert out.oom_killed is True
    assert out.exit_code == 137


@pytest.mark.parametrize(("runtime", "kind"), RUNTIME_MATRIX)
async def test_teardown_removes_container_across_runtimes(runtime, kind) -> None:
    client = FakeDockerClient()
    session = await _provider(kind, client).create(_spec(kind, runtime))
    await session.teardown(reason="completed")
    assert client.created[0].removed is True


@pytest.mark.parametrize(("runtime", "kind"), RUNTIME_MATRIX)
async def test_env_carries_no_secrets_across_runtimes(runtime, kind) -> None:
    client = FakeDockerClient()
    spec = _spec(kind, runtime).model_copy(update={"env": {"CI": "true"}})
    await _provider(kind, client).create(spec)
    environment = client.create_kwargs["environment"]
    blob = " ".join(f"{k}={v}" for k, v in environment.items())
    for secret_marker in ("sk-ant", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "ghp_"):
        assert secret_marker not in blob


@pytest.mark.parametrize(("runtime", "kind"), RUNTIME_MATRIX)
async def test_reaper_reaps_forge_sandbox_containers(runtime, kind, tmp_path) -> None:
    """The label-scoped reaper works identically for every container-backed kind."""
    from _sandbox_fakes import FakeContainer

    client = FakeDockerClient()
    client.existing = [
        FakeContainer(
            id="dead-1",
            attrs={
                "State": {"Status": "exited"},
                "Config": {"Labels": {"forge.agent_run_id": "run-1"}},
            },
        )
    ]
    provider = _provider(kind, client)
    if kind is SandboxKind.MICROVM:
        provider._jailer_root = str(tmp_path / "jailer")  # no VM artifacts to sweep
    removed = await reap_orphans(provider, terminal_run_ids=["run-1"])
    assert removed == 1
    assert client.existing[0].removed is True
