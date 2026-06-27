"""Sandbox errors, the artifact-store seam, and contract re-exports (F19).

The sandbox providers (``local``, ``container``) implement the frozen
:class:`~forge_contracts.SandboxProvider` / :class:`~forge_contracts.SandboxSession`
Protocols. Errors form a single hierarchy rooted at :class:`SandboxError` — the
pre-existing worktree error — so the V1 ``forge_agent.sandbox.SandboxError`` public
name is preserved (back-compat) while the new failure modes are catchable as one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge_contracts import (
    CommandOutput,
    SandboxCommandRunner,
    SandboxKind,
    SandboxNetwork,
    SandboxProvider,
    SandboxResourceLimits,
    SandboxSession,
    SandboxSpec,
)


class SandboxError(RuntimeError):
    """Base class for every sandbox failure (worktree + container)."""


class SandboxStartupError(SandboxError):
    """Daemon/proxy unreachable, or container create failed.

    Raised when ``kind=container`` is requested but the sandbox cannot be brought
    up. The runtime maps this to a terminal/awaiting-input run — it must **never**
    silently fall back to host execution (no silent isolation downgrade).
    """


class SandboxImageNotAllowed(SandboxError):
    """The requested image is not on ``FORGE_SANDBOX_ALLOWED_IMAGES``."""


class SandboxExecError(SandboxError):
    """A command could not be executed inside the sandbox."""


@runtime_checkable
class ArtifactStore(Protocol):
    """Minimal object-store seam for offloading over-cap command output.

    Implemented in production by the MinIO ``object_store``; faked in tests.
    """

    def put(self, key: str, data: bytes, *, content_type: str = "text/plain") -> str: ...


__all__ = [
    "ArtifactStore",
    "CommandOutput",
    "SandboxCommandRunner",
    "SandboxError",
    "SandboxExecError",
    "SandboxImageNotAllowed",
    "SandboxKind",
    "SandboxNetwork",
    "SandboxProvider",
    "SandboxResourceLimits",
    "SandboxSession",
    "SandboxSpec",
    "SandboxStartupError",
]
