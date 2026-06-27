"""Container provider unit tests (fake Docker client) — AC3,4,7,8,9,16,18."""

from __future__ import annotations

import shlex
import uuid

import pytest
from _sandbox_fakes import FakeDockerClient, FakeExecResult

from forge_agent.sandbox import (
    ContainerSandboxProvider,
    SandboxSettings,
    SandboxStartupError,
    build_sandbox_provider,
)
from forge_contracts import SandboxKind, SandboxNetwork, SandboxSpec


async def test_create_args_hardening(fake_docker_client: FakeDockerClient, container_spec) -> None:
    """AC3/AC4 — create kwargs carry the full hardening matrix + single mount."""
    provider = ContainerSandboxProvider(client=fake_docker_client)
    await provider.create(container_spec)

    kwargs = fake_docker_client.create_kwargs
    assert kwargs is not None
    assert kwargs["user"] == "10001:10001"
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]
    assert kwargs["mem_limit"] == "4096m"
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["pids_limit"] == 512
    assert kwargs["network_mode"] == "none"
    assert kwargs["labels"]["forge.sandbox"] == "true"
    assert kwargs["labels"]["forge.agent_run_id"] == str(container_spec.agent_run_id)
    assert kwargs["name"] == f"forge-sbx-{container_spec.agent_run_id}"

    # Exactly one mount, targeting /workspace at the run's subpath.
    mounts = kwargs["mounts"]
    assert len(mounts) == 1
    assert mounts[0]["Target"] == "/workspace"
    assert mounts[0]["Source"] == "forge_repos"
    assert mounts[0]["VolumeOptions"]["Subpath"] == "runA/tree"

    # The container was actually started.
    assert fake_docker_client.created[0].started is True


async def test_run_command_shape_and_timeout(
    fake_docker_client: FakeDockerClient, container_spec
) -> None:
    """AC7/AC8 — only the literal command is interpolated; exit 124 => timed_out."""
    fake_docker_client.exec_results = [FakeExecResult(exit_code=124, stdout=b"", stderr=b"")]
    provider = ContainerSandboxProvider(client=fake_docker_client)
    session = await provider.create(container_spec)

    out = await session.run("pytest -q", cwd="/workspace", timeout_s=30)
    call = fake_docker_client.exec_create_calls[0]
    quoted = shlex.quote("pytest -q")
    assert call["cmd"] == [
        "/bin/sh",
        "-lc",
        f"timeout --kill-after=10s 30s sh -lc {quoted}",
    ]
    assert call["kwargs"]["user"] == "10001:10001"
    assert call["kwargs"]["workdir"] == "/workspace"
    assert out.timed_out is True
    assert out.exit_code == 124
    assert out.sandbox_kind is SandboxKind.CONTAINER
    assert out.container_id == session.sandbox_id


async def test_run_demux_stdout_stderr(
    fake_docker_client: FakeDockerClient, container_spec
) -> None:
    fake_docker_client.exec_results = [
        FakeExecResult(exit_code=0, stdout=b"out-data", stderr=b"err-data")
    ]
    session = await ContainerSandboxProvider(client=fake_docker_client).create(container_spec)
    out = await session.run("echo hi", cwd="/workspace", timeout_s=30)
    assert out.stdout == "out-data"
    assert out.stderr == "err-data"
    assert out.exit_code == 0


async def test_oom_killed_reported(fake_docker_client: FakeDockerClient, container_spec) -> None:
    """AC9 — OOMKilled inspect surfaces oom_killed=True without crashing."""
    fake_docker_client.exec_results = [FakeExecResult(exit_code=137, stdout=b"", stderr=b"")]
    session = await ContainerSandboxProvider(client=fake_docker_client).create(container_spec)
    fake_docker_client.created[0].attrs = {"State": {"OOMKilled": True, "Status": "running"}}
    out = await session.run("python -c 'x=[0]*10**12'", cwd="/workspace", timeout_s=30)
    assert out.oom_killed is True
    assert out.exit_code == 137


async def test_teardown_removes_container(
    fake_docker_client: FakeDockerClient, container_spec
) -> None:
    """AC11 (unit) — teardown removes the container."""
    session = await ContainerSandboxProvider(client=fake_docker_client).create(container_spec)
    await session.teardown(reason="completed")
    assert fake_docker_client.created[0].removed is True


async def test_env_carries_no_secrets(fake_docker_client: FakeDockerClient) -> None:
    """AC16 (unit) — only non-secret SandboxSpec.env reaches the container."""
    spec = SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=SandboxKind.CONTAINER,
        host_worktree_path="/wt/tree",
        worktree_volume="forge_repos",
        worktree_subpath="runA/tree",
        image="ghcr.io/forge-platform/forge-sandbox-python:0.1.0",
        env={"CI": "true", "LANG": "C.UTF-8"},
    )
    session = await ContainerSandboxProvider(client=fake_docker_client).create(spec)
    environment = fake_docker_client.create_kwargs["environment"]
    assert environment == {"CI": "true", "LANG": "C.UTF-8"}
    # exec env is exactly what the caller passes (no implicit secret injection).
    await session.run("env", cwd="/workspace", timeout_s=10, env={"PIP_NO_INPUT": "1"})
    exec_env = fake_docker_client.exec_create_calls[0]["kwargs"]["environment"]
    assert exec_env == {"PIP_NO_INPUT": "1"}
    blob = " ".join(str(v) for v in (*environment.values(), *exec_env.values()))
    for secret_marker in ("sk-ant", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "ghp_"):
        assert secret_marker not in blob


async def test_no_silent_downgrade_on_unreachable_daemon(container_spec) -> None:
    """AC18 — an unreachable daemon raises SandboxStartupError (no host fallback)."""
    # No injected client + a connection-refused host: _ensure_client must raise.
    provider = ContainerSandboxProvider(docker_host="tcp://127.0.0.1:1")
    with pytest.raises(SandboxStartupError):
        await provider.create(container_spec)


async def test_missing_image_raises_startup_error(
    fake_docker_client: FakeDockerClient,
) -> None:
    """A container spec without an image fails loudly (never a host fallback)."""
    spec = SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=SandboxKind.CONTAINER,
        host_worktree_path="/wt/tree",
        image=None,
    )
    with pytest.raises(SandboxStartupError):
        await ContainerSandboxProvider(client=fake_docker_client).create(spec)


async def test_egress_attaches_network(fake_docker_client: FakeDockerClient) -> None:
    """AC6 (unit) — egress mode connects the sandbox to the egress network."""
    spec = SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=SandboxKind.CONTAINER,
        host_worktree_path="/wt/tree",
        worktree_volume="forge_repos",
        worktree_subpath="runA/tree",
        image="ghcr.io/forge-platform/forge-sandbox-python:0.1.0",
        network=SandboxNetwork.EGRESS,
    )
    provider = ContainerSandboxProvider(client=fake_docker_client, egress_network="forge_egress")
    await provider.create(spec)
    assert fake_docker_client.create_kwargs["network_mode"] is None
    assert fake_docker_client.networks_got[0].name == "forge_egress"
    assert fake_docker_client.networks_got[0].connected


def test_factory_returns_container_for_container_kind() -> None:
    provider = build_sandbox_provider(
        SandboxSettings(kind=SandboxKind.CONTAINER), docker_client=FakeDockerClient()
    )
    assert isinstance(provider, ContainerSandboxProvider)
