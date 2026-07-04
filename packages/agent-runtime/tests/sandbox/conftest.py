"""Fixtures for the F19 sandbox suite (fakes live in ``_sandbox_fakes``)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _sandbox_fakes import FakeDockerClient, FakeObjectStore

from forge_contracts import SandboxKind, SandboxResourceLimits, SandboxSpec


@pytest.fixture
def fake_docker_client() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def fake_object_store() -> FakeObjectStore:
    return FakeObjectStore()


@pytest.fixture
def container_spec() -> SandboxSpec:
    return SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=SandboxKind.CONTAINER,
        host_worktree_path="/srv/worktrees/runA/tree",
        worktree_volume="forge_repos",
        worktree_subpath="runA/tree",
        image="ghcr.io/forge-platform/forge-sandbox-python:0.1.0",
        limits=SandboxResourceLimits(cpus=2.0, memory_mb=4096, pids_limit=512, tmpfs_mb=1024),
    )


@pytest.fixture
def tmp_worktree(tmp_path) -> Iterator[str]:
    work = tmp_path / "tree"
    work.mkdir()
    yield str(work)


def _kernel_spec(kind: SandboxKind, runtime: str, **overrides) -> SandboxSpec:
    base = {
        "agent_run_id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
        "kind": kind,
        "host_worktree_path": "/srv/worktrees/runA/tree",
        "worktree_volume": "forge_repos",
        "worktree_subpath": "runA/tree",
        "image": "ghcr.io/forge-platform/forge-sandbox-python:0.1.0",
        "limits": SandboxResourceLimits(cpus=2.0, memory_mb=4096, pids_limit=512, tmpfs_mb=1024),
        "runtime": runtime,
    }
    base.update(overrides)
    return SandboxSpec(**base)


@pytest.fixture
def gvisor_spec() -> SandboxSpec:
    return _kernel_spec(SandboxKind.GVISOR, "runsc")


@pytest.fixture
def microvm_spec() -> SandboxSpec:
    return _kernel_spec(SandboxKind.MICROVM, "kata-fc")
