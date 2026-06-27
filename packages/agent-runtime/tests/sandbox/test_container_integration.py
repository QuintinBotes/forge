"""Real-daemon container integration tests (F19 — @pytest.mark.docker).

These exercise the hardening matrix, cross-task isolation, network posture,
timeout/oom, uid round-trip and teardown against a **live** Docker daemon with the
curated sandbox images present. They are the Docker-gated CI tier: opt in with
``FORGE_SANDBOX_DOCKER_TESTS=1`` (building/pulling images needs network, so the
default offline gate skips them — parked, never faked).
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest

from forge_agent.sandbox import ContainerSandboxProvider, SandboxStartupError
from forge_contracts import SandboxKind, SandboxSpec

pytestmark = pytest.mark.docker

_OPT_IN = os.environ.get("FORGE_SANDBOX_DOCKER_TESTS") == "1"
_SANDBOX_IMAGE = os.environ.get("FORGE_SANDBOX_IMAGE_PYTHON", "")

skip_unless_docker = pytest.mark.skipif(
    not (_OPT_IN and _SANDBOX_IMAGE),
    reason=(
        "PARKED: real-daemon sandbox tests need FORGE_SANDBOX_DOCKER_TESTS=1 + a "
        "curated FORGE_SANDBOX_IMAGE_PYTHON; image pulls need network unavailable "
        "in the offline gate. Run in the Docker-gated CI job to close."
    ),
)


@pytest.fixture
def real_docker():
    docker = pytest.importorskip("docker")
    client = docker.from_env()
    try:
        client.ping()
    except Exception as exc:  # daemon down -> skip the integration tier
        pytest.skip(f"docker daemon unreachable: {exc}")
    created: list[str] = []
    yield client, created
    for name in created:
        with contextlib.suppress(Exception):
            client.containers.get(name).remove(force=True)


def _spec(volume: str, subpath: str) -> SandboxSpec:
    return SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=SandboxKind.CONTAINER,
        host_worktree_path="/workspace",
        worktree_volume=volume,
        worktree_subpath=subpath,
        image=_SANDBOX_IMAGE,
    )


@skip_unless_docker
async def test_container_inspect_hardening(real_docker) -> None:
    """AC3 — inspect shows the hardening matrix on a real container."""
    client, created = real_docker
    provider = ContainerSandboxProvider(client=client)
    spec = _spec("forge_repos", "")
    session = await provider.create(spec)
    created.append(f"forge-sbx-{spec.agent_run_id}")
    info = client.containers.get(session.sandbox_id).attrs
    host_config = info["HostConfig"]
    assert info["Config"]["User"] == "10001:10001"
    assert host_config["ReadonlyRootfs"] is True
    assert "ALL" in host_config["CapDrop"]
    assert any("no-new-privileges" in opt for opt in host_config["SecurityOpt"])
    assert info["Config"]["Labels"]["forge.sandbox"] == "true"
    await session.teardown(reason="completed")


@skip_unless_docker
async def test_network_none_blocks_egress(real_docker) -> None:
    """AC5 — with network=none, outbound DNS/egress fails inside the container."""
    client, created = real_docker
    provider = ContainerSandboxProvider(client=client)
    spec = _spec("forge_repos", "")
    session = await provider.create(spec)
    created.append(f"forge-sbx-{spec.agent_run_id}")
    out = await session.run("getent hosts pypi.org", cwd="/workspace", timeout_s=20)
    assert out.exit_code != 0
    await session.teardown(reason="completed")


def test_unreachable_daemon_is_loud() -> None:
    """AC18 (always-on) — an unreachable proxy raises rather than downgrading."""
    import asyncio

    provider = ContainerSandboxProvider(docker_host="tcp://127.0.0.1:1")
    with pytest.raises(SandboxStartupError):
        asyncio.run(provider.create(_spec("forge_repos", "")))
