"""F34 AC16 — runtime registry detection + isolation-class mapping."""

from __future__ import annotations

from _sandbox_fakes import FakeDockerClient

from forge_agent.sandbox import detect_registered_runtimes, isolation_class_for
from forge_agent.sandbox.runtime import DEFAULT_RUNTIMES
from forge_contracts import SandboxIsolationClass, SandboxKind


def test_detect_parses_docker_info_runtimes() -> None:
    client = FakeDockerClient(registered_runtimes=("runc", "runsc", "kata-fc"))
    assert detect_registered_runtimes(client) == {"runc", "runsc", "kata-fc"}


def test_detect_unreachable_daemon_returns_empty() -> None:
    class _Broken:
        def info(self):
            raise ConnectionError("daemon down")

    assert detect_registered_runtimes(_Broken()) == set()


def test_detect_missing_runtimes_block_returns_empty() -> None:
    class _NoRuntimes:
        def info(self):
            return {"ServerVersion": "26.1.4"}

    assert detect_registered_runtimes(_NoRuntimes()) == set()


def test_isolation_class_for_every_kind() -> None:
    assert isolation_class_for(SandboxKind.WORKTREE) is SandboxIsolationClass.HOST_PROCESS
    assert isolation_class_for(SandboxKind.CONTAINER) is SandboxIsolationClass.NAMESPACE
    assert isolation_class_for(SandboxKind.GVISOR) is SandboxIsolationClass.USERSPACE_KERNEL
    assert isolation_class_for(SandboxKind.MICROVM) is SandboxIsolationClass.MICROVM


def test_default_runtimes_per_kind() -> None:
    assert DEFAULT_RUNTIMES[SandboxKind.WORKTREE] is None
    assert DEFAULT_RUNTIMES[SandboxKind.CONTAINER] == "runc"
    assert DEFAULT_RUNTIMES[SandboxKind.GVISOR] == "runsc"
    assert DEFAULT_RUNTIMES[SandboxKind.MICROVM] == "kata-fc"
