"""OCI runtime registry helpers (F34).

``detect_registered_runtimes`` reads the runtimes the Docker daemon has
registered (``docker info`` → ``Runtimes``), so the kernel-boundary providers
can preflight-gate on ``runsc`` / ``kata-fc`` being installed;
``isolation_class_for`` maps each :class:`SandboxKind` to its auditable trust
tier. Both are pure/dependency-light so the unit tier runs without a daemon.
"""

from __future__ import annotations

from typing import Any

from forge_contracts import SandboxIsolationClass, SandboxKind

#: Default OCI runtime per sandbox kind (``worktree`` has none).
DEFAULT_RUNTIMES: dict[SandboxKind, str | None] = {
    SandboxKind.WORKTREE: None,
    SandboxKind.CONTAINER: "runc",
    SandboxKind.GVISOR: "runsc",
    SandboxKind.MICROVM: "kata-fc",
}

_ISOLATION_CLASS: dict[SandboxKind, SandboxIsolationClass] = {
    SandboxKind.WORKTREE: SandboxIsolationClass.HOST_PROCESS,
    SandboxKind.CONTAINER: SandboxIsolationClass.NAMESPACE,
    SandboxKind.GVISOR: SandboxIsolationClass.USERSPACE_KERNEL,
    SandboxKind.MICROVM: SandboxIsolationClass.MICROVM,
}


def isolation_class_for(kind: SandboxKind) -> SandboxIsolationClass:
    """The auditable trust tier for a sandbox kind."""
    return _ISOLATION_CLASS[kind]


def detect_registered_runtimes(docker_client: Any) -> set[str]:
    """The OCI runtime names registered with the daemon (``docker info`` Runtimes).

    Returns an empty set when the daemon is unreachable or the payload has no
    ``Runtimes`` block — callers treat "unknown" as "not registered" (fail loud,
    never assume a kernel boundary that cannot be verified).
    """
    try:
        info = docker_client.info()
    except Exception:
        return set()
    runtimes = (info or {}).get("Runtimes") or {}
    if isinstance(runtimes, dict):
        return {str(name) for name in runtimes}
    return {str(name) for name in runtimes or []}


__all__ = ["DEFAULT_RUNTIMES", "detect_registered_runtimes", "isolation_class_for"]
