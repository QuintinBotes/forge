"""F34 AC1 — 4-level lattice precedence / no downgrade; kernel settings merge."""

from __future__ import annotations

import uuid

import pytest

from forge_agent.sandbox import (
    SandboxSettings,
    resolve_sandbox_kind,
    resolve_sandbox_settings,
)
from forge_contracts import (
    SANDBOX_KIND_RANK,
    PolicySandboxBlock,
    SandboxKind,
)

W = SandboxKind.WORKTREE
C = SandboxKind.CONTAINER
G = SandboxKind.GVISOR
M = SandboxKind.MICROVM

ALL_KINDS = [W, C, G, M]


def test_rank_is_the_documented_lattice() -> None:
    assert SANDBOX_KIND_RANK == {W: 0, C: 1, G: 2, M: 3}


@pytest.mark.parametrize("workspace_min", ALL_KINDS)
@pytest.mark.parametrize("policy", [None, *ALL_KINDS])
def test_full_matrix_never_downgrades(workspace_min, policy) -> None:
    """AC1 — the full 4x4-plus-None matrix: stronger wins, never below the min."""
    resolved = resolve_sandbox_kind(workspace_min, policy)
    assert SANDBOX_KIND_RANK[resolved] >= SANDBOX_KIND_RANK[workspace_min]
    if policy is not None:
        assert SANDBOX_KIND_RANK[resolved] >= SANDBOX_KIND_RANK[policy]
        assert resolved in {workspace_min, policy}
    else:
        assert resolved is workspace_min


@pytest.mark.parametrize(
    ("workspace_min", "policy", "expected"),
    [
        (G, M, M),  # policy may strengthen above a kernel minimum
        (M, C, M),  # never weaken below microvm
        (G, W, G),  # never weaken below gvisor
        (C, G, G),  # policy strengthens container -> gvisor
        (W, M, M),  # policy may jump straight to microvm
        (W, None, W),  # both unset -> worktree
    ],
)
def test_spec_examples(workspace_min, policy, expected) -> None:
    assert resolve_sandbox_kind(workspace_min, policy) is expected


def test_resolve_settings_gvisor_carries_runtime_platform_and_image() -> None:
    settings = SandboxSettings(kind=SandboxKind.GVISOR)
    spec = resolve_sandbox_settings(
        settings,
        None,
        language="python",
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        host_worktree_path="/wt/tree",
        worktree_subpath="runA/tree",
    )
    assert spec.kind is SandboxKind.GVISOR
    assert spec.runtime == "runsc"
    assert spec.gvisor_platform == "systrap"
    assert spec.image == settings.image_python  # allowlisted image, like container
    assert spec.vm_vcpus is None and spec.vm_memory_mb is None


def test_resolve_settings_microvm_policy_sizing_overrides() -> None:
    settings = SandboxSettings(kind=SandboxKind.GVISOR)
    block = PolicySandboxBlock(isolation="microvm", vm_vcpus=2, vm_memory="4g")
    spec = resolve_sandbox_settings(
        settings,
        block,
        language="python",
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        host_worktree_path="/wt/tree",
    )
    assert spec.kind is SandboxKind.MICROVM  # strengthened over workspace gvisor
    assert spec.runtime == "kata-fc"
    assert spec.vm_vcpus == 2
    assert spec.vm_memory_mb == 4096
    assert spec.gvisor_platform is None


def test_resolve_settings_policy_gvisor_platform_wins() -> None:
    settings = SandboxSettings(kind=SandboxKind.GVISOR, gvisor_platform="systrap")
    block = PolicySandboxBlock(isolation="gvisor", gvisor_platform="kvm")
    spec = resolve_sandbox_settings(
        settings,
        block,
        language="python",
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        host_worktree_path="/wt/tree",
    )
    assert spec.gvisor_platform == "kvm"


def test_resolve_settings_container_downgrade_request_stays_gvisor() -> None:
    """Journey 3 — repo requests container while workspace minimum is gvisor."""
    settings = SandboxSettings(kind=SandboxKind.GVISOR)
    block = PolicySandboxBlock(isolation="container")
    spec = resolve_sandbox_settings(
        settings,
        block,
        language="python",
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        host_worktree_path="/wt/tree",
    )
    assert spec.kind is SandboxKind.GVISOR
    assert spec.runtime == "runsc"


def test_settings_from_env_kernel_fields(monkeypatch) -> None:
    env = {
        "FORGE_SANDBOX_KIND": "microvm",
        "FORGE_SANDBOX_GVISOR_RUNTIME": "my-runsc",
        "FORGE_SANDBOX_GVISOR_PLATFORM": "kvm",
        "FORGE_SANDBOX_MICROVM_RUNTIME": "kata-fc2",
        "FORGE_SANDBOX_MICROVM_VCPUS": "4",
        "FORGE_SANDBOX_MICROVM_MEMORY_MB": "8192",
        "FORGE_SANDBOX_REQUIRE_KVM": "false",
        "FORGE_SANDBOX_JAILER_ROOT": "/srv/jailer",
    }
    settings = SandboxSettings.from_env(env)
    assert settings.kind is SandboxKind.MICROVM
    assert settings.gvisor_runtime == "my-runsc"
    assert settings.gvisor_platform == "kvm"
    assert settings.microvm_runtime == "kata-fc2"
    assert settings.microvm_vcpus == 4
    assert settings.microvm_memory_mb == 8192
    assert settings.require_kvm is False
    assert settings.jailer_root == "/srv/jailer"


def test_settings_from_env_defaults() -> None:
    settings = SandboxSettings.from_env({})
    assert settings.gvisor_runtime == "runsc"
    assert settings.gvisor_platform == "systrap"
    assert settings.microvm_runtime == "kata-fc"
    assert settings.microvm_vcpus is None
    assert settings.microvm_memory_mb is None
    assert settings.require_kvm is True
    assert settings.jailer_root == "/var/lib/forge/jailer"
