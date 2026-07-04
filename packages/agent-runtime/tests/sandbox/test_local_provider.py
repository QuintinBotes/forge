"""AC2/AC10 — LocalSandboxProvider host-subprocess parity + output capping."""

from __future__ import annotations

import shutil
import uuid

import pytest

from forge_agent.sandbox import LocalSandboxProvider, SandboxSettings, build_sandbox_provider
from forge_contracts import SandboxKind, SandboxSpec

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="sh not available")


def _spec(path: str) -> SandboxSpec:
    return SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=SandboxKind.WORKTREE,
        host_worktree_path=path,
    )


async def test_run_exit_code_and_stdout(tmp_worktree: str) -> None:
    provider = LocalSandboxProvider()
    session = await provider.create(_spec(tmp_worktree))
    assert session.kind is SandboxKind.WORKTREE
    assert session.workspace_dir == tmp_worktree

    out = await session.run("echo hello", cwd=tmp_worktree, timeout_s=10)
    assert out.exit_code == 0
    assert out.stdout.strip() == "hello"
    assert out.sandbox_kind is SandboxKind.WORKTREE
    assert out.container_id is None


async def test_run_nonzero_exit(tmp_worktree: str) -> None:
    session = await LocalSandboxProvider().create(_spec(tmp_worktree))
    out = await session.run("exit 3", cwd=tmp_worktree, timeout_s=10)
    assert out.exit_code == 3


async def test_run_env_passthrough(tmp_worktree: str) -> None:
    session = await LocalSandboxProvider().create(_spec(tmp_worktree))
    out = await session.run("echo $FORGE_X", cwd=tmp_worktree, timeout_s=10, env={"FORGE_X": "42"})
    assert out.stdout.strip() == "42"


async def test_run_timeout_sets_124(tmp_worktree: str) -> None:
    session = await LocalSandboxProvider().create(_spec(tmp_worktree))
    out = await session.run("sleep 5", cwd=tmp_worktree, timeout_s=1)
    assert out.timed_out is True
    assert out.exit_code == 124
    # Session stays usable for a subsequent run.
    again = await session.run("echo ok", cwd=tmp_worktree, timeout_s=10)
    assert again.exit_code == 0


async def test_output_capping_offloads_to_store(tmp_worktree: str) -> None:
    class _Store:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put(self, key: str, data: bytes, *, content_type: str = "text/plain") -> str:
            self.objects[key] = data
            return f"minio://{key}"

    store = _Store()
    provider = LocalSandboxProvider(artifact_store=store, output_cap_bytes=10)
    session = await provider.create(_spec(tmp_worktree))
    out = await session.run("printf 'aaaaaaaaaaaaaaaaaaaa'", cwd=tmp_worktree, timeout_s=10)
    assert len(out.stdout.encode()) <= 10
    assert out.stdout_artifact_ref is not None
    assert len(store.objects) == 1
    assert next(iter(store.objects.values())) == b"a" * 20


def test_factory_returns_local_for_worktree() -> None:
    provider = build_sandbox_provider(SandboxSettings(kind=SandboxKind.WORKTREE))
    assert isinstance(provider, LocalSandboxProvider)
