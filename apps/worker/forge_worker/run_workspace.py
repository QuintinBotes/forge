"""Give an agent run a workspace it can act in (spec G §3.3).

For an objective whose ``context["repo_path"]`` names a local clone the worker
can reach, this creates a dedicated git worktree on a run branch, resolves the
sandbox from workspace settings strengthened by the repo's
``.forge/policy.yaml`` (never weakened), opens one sandbox session for the whole
run, and builds the main-path tool belt over it. Declared ``policy.commands``
run inside that session; the model only chooses which one.

The worktree is removed afterwards but the run branch and its commits stay in
the clone, so the finalizer (G2) can push them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from forge_agent.sandbox import (
    SandboxError,
    WorktreeSandbox,
    build_sandbox_provider,
    resolve_sandbox_settings,
)
from forge_agent.sandbox.settings import SandboxSettings
from forge_agent.tools import ToolRegistry, agent_tool_belt
from forge_contracts import AgentObjective, CommandOutput, Policy, SandboxSession
from forge_policy.loader import load_policy


@dataclass(frozen=True)
class RunWorkspace:
    path: Path
    branch: str
    tools: ToolRegistry


def _repo_policy(worktree: Path) -> Policy | None:
    try:
        return load_policy(worktree)
    except FileNotFoundError:
        return None


@contextmanager
def prepared_workspace(
    objective: AgentObjective,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    settings: SandboxSettings | None = None,
) -> Iterator[RunWorkspace | None]:
    """Yield a ready workspace, or ``None`` when the objective names no repository."""
    repo_path = objective.context.get("repo_path")
    if not repo_path:
        yield None
        return
    target = objective.primary_repo_target
    base_branch = target.base_branch if target else "main"
    prefix = (target.branch_prefix if target else None) or "forge"
    worktree_sandbox = WorktreeSandbox(repo_path, base_branch=base_branch, branch_prefix=prefix)
    worktree = worktree_sandbox.create(branch=f"{prefix}/run-{run_id.hex[:12]}")
    loop = asyncio.new_event_loop()
    session: SandboxSession | None = None
    try:
        policy = _repo_policy(worktree)
        resolved_settings = settings or SandboxSettings.from_env()
        spec = resolve_sandbox_settings(
            resolved_settings,
            policy.sandbox if policy else None,
            language=(policy.languages[0] if policy and policy.languages else "python"),
            agent_run_id=run_id,
            workspace_id=workspace_id,
            host_worktree_path=str(worktree),
        )
        provider = build_sandbox_provider(resolved_settings.model_copy(update={"kind": spec.kind}))
        loop.run_until_complete(provider.preflight())
        # The provider union widens the coroutine's result; every provider returns a session.
        live_session = cast(SandboxSession, loop.run_until_complete(provider.create(spec)))
        session = live_session

        def run_command(command: str) -> CommandOutput:
            return loop.run_until_complete(
                live_session.run(
                    command, cwd=live_session.workspace_dir, timeout_s=spec.exec_timeout_seconds
                )
            )

        tools = agent_tool_belt(
            worktree,
            run_id=str(run_id),
            commands=policy.commands if policy else None,
            run_command=run_command,
        )
        assert worktree_sandbox.branch is not None  # set by create()
        yield RunWorkspace(path=worktree, branch=worktree_sandbox.branch, tools=tools)
    finally:
        if session is not None:
            with suppress(SandboxError):
                loop.run_until_complete(session.teardown(reason="run_complete"))
        loop.close()
        worktree_sandbox.cleanup()


__all__ = ["RunWorkspace", "prepared_workspace"]
