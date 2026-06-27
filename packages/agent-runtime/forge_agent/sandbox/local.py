"""Local (host-subprocess) sandbox provider — the ``worktree`` default (F19).

``LocalSandboxProvider`` runs allow-listed ``policy.commands`` strings as host
subprocesses inside the run's worktree, behind the same
:class:`~forge_contracts.SandboxProvider` / :class:`~forge_contracts.SandboxSession`
seam the container provider implements. This is the V1 behaviour (git worktrees
are isolation, not a security boundary); ``container`` is opt-in for hardening.

Commands run via ``sh -lc``; never model-authored shell (the caller passes only
literal ``policy.commands`` strings, gated upstream by the policy guard).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping

from forge_agent.sandbox.base import ArtifactStore
from forge_agent.sandbox.output import cap_output
from forge_contracts import CommandOutput, SandboxKind, SandboxSpec

_KILL_GRACE_SECONDS = 10


class LocalSandboxSession:
    """A host-subprocess session bound to one worktree (``workspace_dir``)."""

    kind = SandboxKind.WORKTREE

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        artifact_store: ArtifactStore | None = None,
        output_cap_bytes: int = 262144,
    ) -> None:
        self._spec = spec
        self._artifact_store = artifact_store
        self._output_cap_bytes = output_cap_bytes
        # For the worktree provider the sandbox dir IS the host path.
        self.sandbox_id = f"local-{spec.agent_run_id}"
        self.workspace_dir = spec.host_worktree_path
        self.host_worktree_path = spec.host_worktree_path

    async def setup(self) -> None:
        """Run any ``setup_commands`` once (idempotent)."""
        for command in self._spec.setup_commands:
            await self.run(
                command,
                cwd=self.workspace_dir,
                timeout_s=self._spec.exec_timeout_seconds,
            )

    async def teardown(self, *, reason: str = "completed") -> None:
        """No host process to remove for the worktree provider (no-op)."""
        return None

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandOutput:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-lc",
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            proc.kill()
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=_KILL_GRACE_SECONDS
                )
            except TimeoutError:  # pragma: no cover - kill should always reap
                stdout_b, stderr_b = b"", b""
        duration_ms = int((time.monotonic() - started) * 1000)

        exit_code = 124 if timed_out else (proc.returncode or 0)
        stdout, stdout_ref = cap_output(
            stdout_b.decode("utf-8", errors="replace"),
            cap_bytes=self._output_cap_bytes,
            store=self._artifact_store,
            key=self._artifact_key("stdout"),
        )
        stderr, stderr_ref = cap_output(
            stderr_b.decode("utf-8", errors="replace"),
            cap_bytes=self._output_cap_bytes,
            store=self._artifact_store,
            key=self._artifact_key("stderr"),
        )
        return CommandOutput(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            oom_killed=False,
            stdout_artifact_ref=stdout_ref,
            stderr_artifact_ref=stderr_ref,
            sandbox_kind=SandboxKind.WORKTREE,
            container_id=None,
        )

    def _artifact_key(self, stream: str) -> str:
        return f"sandbox/{self._spec.agent_run_id}/{uuid.uuid4().hex}.{stream}.log"


class LocalSandboxProvider:
    """Builds host-subprocess sessions (``worktree`` isolation)."""

    kind = SandboxKind.WORKTREE

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore | None = None,
        output_cap_bytes: int = 262144,
    ) -> None:
        self._artifact_store = artifact_store
        self._output_cap_bytes = output_cap_bytes

    async def create(self, spec: SandboxSpec) -> LocalSandboxSession:
        session = LocalSandboxSession(
            spec,
            artifact_store=self._artifact_store,
            output_cap_bytes=self._output_cap_bytes,
        )
        await session.setup()
        return session

    async def reap_orphans(self) -> int:
        """Host subprocesses are synchronous and never orphan a container."""
        return 0


__all__ = ["LocalSandboxProvider", "LocalSandboxSession"]
