"""AC1 — selection precedence / no downgrade; settings merge."""

from __future__ import annotations

import uuid

import pytest

from forge_agent.sandbox import (
    SandboxImageNotAllowed,
    SandboxSettings,
    parse_memory_mb,
    resolve_sandbox_kind,
    resolve_sandbox_settings,
)
from forge_contracts import PolicySandboxBlock, SandboxKind, SandboxNetwork

C = SandboxKind.CONTAINER
W = SandboxKind.WORKTREE


@pytest.mark.parametrize(
    ("workspace_min", "policy", "expected"),
    [
        (W, None, W),  # default
        (C, None, C),
        (C, W, C),  # policy cannot downgrade below workspace minimum
        (W, C, C),  # policy may strengthen
        (C, C, C),
        (W, W, W),
    ],
)
def test_resolve_kind_precedence(workspace_min, policy, expected) -> None:
    assert resolve_sandbox_kind(workspace_min, policy) is expected


def test_parse_memory_mb() -> None:
    assert parse_memory_mb("4g", 1) == 4096
    assert parse_memory_mb("512m", 1) == 512
    assert parse_memory_mb("2048", 1) == 2048
    assert parse_memory_mb(None, 4096) == 4096
    assert parse_memory_mb("garbage", 4096) == 4096


def test_resolve_settings_merges_policy_over_workspace_defaults() -> None:
    settings = SandboxSettings(kind=SandboxKind.WORKTREE)
    block = PolicySandboxBlock(
        isolation="container",
        network="egress",
        memory="8g",
        cpus=4,
        pids_limit=256,
        egress_allowlist=["pypi.org"],
        setup_commands=["uv sync"],
        exec_timeout_seconds=600,
    )
    spec = resolve_sandbox_settings(
        settings,
        block,
        language="python",
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        host_worktree_path="/wt/tree",
        worktree_subpath="runA/tree",
    )
    assert spec.kind is SandboxKind.CONTAINER  # strengthened
    assert spec.network is SandboxNetwork.EGRESS
    assert spec.limits.memory_mb == 8192
    assert spec.limits.cpus == 4
    assert spec.limits.pids_limit == 256
    assert spec.egress_allowlist == ["pypi.org"]
    assert spec.setup_commands == ["uv sync"]
    assert spec.exec_timeout_seconds == 600
    assert spec.image == settings.image_python


def test_resolve_settings_policy_cannot_downgrade() -> None:
    settings = SandboxSettings(kind=SandboxKind.CONTAINER)
    block = PolicySandboxBlock(isolation="worktree")
    spec = resolve_sandbox_settings(
        settings,
        block,
        language="python",
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        host_worktree_path="/wt/tree",
    )
    assert spec.kind is SandboxKind.CONTAINER


def test_resolve_settings_rejects_non_allowlisted_image() -> None:
    settings = SandboxSettings(kind=SandboxKind.CONTAINER)
    block = PolicySandboxBlock(isolation="container", image="evil/image:latest")
    with pytest.raises(SandboxImageNotAllowed):
        resolve_sandbox_settings(
            settings,
            block,
            language="python",
            agent_run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            host_worktree_path="/wt/tree",
        )
