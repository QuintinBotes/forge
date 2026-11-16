"""Give an agent run a workspace it can act in (spec G §3.3).

For an objective whose ``context["repo_path"]`` names a local clone the worker
can reach, this creates a dedicated git worktree on a run branch, resolves the
sandbox from workspace settings strengthened by the repo's
``.forge/policy.yaml`` (never weakened), opens one sandbox session for the whole
run, and builds the main-path tool belt over it. Declared ``policy.commands``
run inside that session; the model only chooses which one.

The worktree is removed afterwards but the run branch and its commits stay in
the clone, so the finalizer (G2) can push them. :func:`remove_run_worktrees`
removes a worktree a crashed or cancelled run left behind (``forge.cleanup_worktree``).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from scf_agent.sandbox import (
    SandboxError,
    WorktreeSandbox,
    build_sandbox_provider,
    resolve_sandbox_settings,
)
from scf_agent.sandbox.settings import SandboxSettings
from scf_agent.tools import ToolRegistry, agent_tool_belt
from scf_contracts import AgentObjective, CommandOutput, Policy, SandboxNetwork, SandboxSession
from scf_policy.loader import load_policy


@dataclass(frozen=True)
class RunWorkspace:
    path: Path
    branch: str
    tools: ToolRegistry


def run_branch(prefix: str, run_id: uuid.UUID) -> str:
    """The branch :func:`prepared_workspace` creates for ``run_id``."""
    return f"{prefix}/run-{run_id.hex[:12]}"


def remove_run_worktrees(
    repos: Iterable[Path], run_ids: Iterable[uuid.UUID], *, deadline: float | None = None
) -> list[Path]:
    """Remove the worktrees left in ``repos`` on these runs' branches; return what was removed.

    Idempotent: a run with no worktree left is a no-op. The run branches stay, as
    after a normal run. Past ``deadline`` (``time.monotonic()``) it raises
    ``TimeoutError`` instead of starting another git call.
    """

    def git(repo: Path, *args: str) -> str:
        timeout = _GIT_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("worktree cleanup ran out of time")
        return _git_out(repo, *args, timeout=timeout)

    suffixes = tuple(run_branch("", run_id) for run_id in run_ids)
    removed: list[Path] = []
    if not suffixes:
        return removed
    for repo in repos:
        listing = git(repo, "worktree", "list", "--porcelain")
        for entry in listing.split("\n\n"):
            fields = dict(line.partition(" ")[::2] for line in entry.splitlines())
            if fields.get("branch", "").endswith(suffixes):
                path = Path(fields["worktree"])
                git(repo, "worktree", "remove", "--force", str(path))
                if path.parent.name.startswith("forge-wt-"):  # WorktreeSandbox's temp holder
                    shutil.rmtree(path.parent, ignore_errors=True)
                removed.append(path)
        git(repo, "worktree", "prune")
    return removed


_GIT_TIMEOUT = 60.0


def _git_out(repo: Path, *args: str, timeout: float = _GIT_TIMEOUT) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def file_at(repo: Path, ref: str, path: str) -> str:
    """``path`` as committed at ``ref``, or nothing when it is not there."""
    try:
        return _git_out(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return ""


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
    worktree = worktree_sandbox.create(branch=run_branch(prefix, run_id))
    try:
        policy = _repo_policy(worktree)
        with _sandbox_commands(
            worktree, policy, run_id=run_id, workspace_id=workspace_id, settings=settings
        ) as run_command:
            tools = agent_tool_belt(
                worktree,
                run_id=str(run_id),
                commands=policy.commands if policy else None,
                run_command=run_command,
            )
            assert worktree_sandbox.branch is not None  # set by create()
            yield RunWorkspace(path=worktree, branch=worktree_sandbox.branch, tools=tools)
    finally:
        worktree_sandbox.cleanup()


@contextmanager
def _sandbox_commands(
    worktree: Path,
    policy: Policy | None,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    settings: SandboxSettings | None,
    network: SandboxNetwork | None = None,
) -> Iterator[Callable[[str], CommandOutput]]:
    """One sandbox session over ``worktree`` for the whole block, as a command runner.

    The sandbox is the workspace settings strengthened by ``policy`` (never weakened).
    """
    loop = asyncio.new_event_loop()
    session: SandboxSession | None = None
    try:
        resolved_settings = settings or SandboxSettings.from_env()
        spec = resolve_sandbox_settings(
            resolved_settings,
            policy.sandbox if policy else None,
            language=(policy.languages[0] if policy and policy.languages else "python"),
            agent_run_id=run_id,
            workspace_id=workspace_id,
            host_worktree_path=str(worktree),
        )
        if network is not None:  # the caller's posture wins over the policy's (checks: none)
            spec = spec.model_copy(update={"network": network})
        provider = build_sandbox_provider(resolved_settings.model_copy(update={"kind": spec.kind}))
        loop.run_until_complete(provider.preflight())
        # The provider union widens the coroutine's result; every provider returns a session.
        live_session = cast(SandboxSession, loop.run_until_complete(provider.create(spec)))
        session = live_session
        exec_timeout = spec.exec_timeout_seconds

        def run_command(command: str) -> CommandOutput:
            return loop.run_until_complete(
                live_session.run(command, cwd=live_session.workspace_dir, timeout_s=exec_timeout)
            )

        yield run_command
    finally:
        if session is not None:
            with suppress(SandboxError):
                loop.run_until_complete(session.teardown(reason="run_complete"))
        loop.close()


#: The result a repository that declares no command gets: a failure named for its
#: reason (spec 001, verification scenario 3), never an empty pass.
NO_DECLARED_CHECKS = {"no_checks_declared": False}

#: The most output a recorded check keeps: the last lines of each stream (#172).
CHECK_OUTPUT_TAIL_LINES = 400


@dataclass(frozen=True)
class CheckRun:
    """One declared command's run on the checked commit."""

    id: str
    exit_code: int
    timed_out: bool
    started_at: datetime
    duration_ms: int
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@contextmanager
def detached_worktree(repo_path: Path, commit: str) -> Iterator[Path]:
    """A fresh, detached worktree of ``commit``, removed afterwards (#172, #173).

    It holds only the commit: nothing from the agent's session.
    """
    holder = Path(tempfile.mkdtemp(prefix="scf-checkout-"))
    worktree = holder / "tree"
    try:
        _git_out(repo_path, "worktree", "add", "--detach", str(worktree), commit)
        yield worktree
    finally:
        with suppress(subprocess.SubprocessError):
            _git_out(repo_path, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(holder, ignore_errors=True)
        with suppress(subprocess.SubprocessError):
            _git_out(repo_path, "worktree", "prune")


def _tail(*streams: str) -> str:
    """The last lines of each stream, so a noisy stderr cannot push out stdout's verdict."""
    tails = ("\n".join(s.splitlines()[-CHECK_OUTPUT_TAIL_LINES:]) for s in streams if s)
    return "\n".join(tails)


def run_declared_checks(
    repo_path: Path,
    commit: str,
    policy: Policy | None,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    settings: SandboxSettings | None = None,
) -> list[CheckRun]:
    """Run ``policy.commands`` in a sandbox on a fresh, detached worktree of ``commit``.

    One run per command, passing when it exited 0 within its time limit. The sandbox
    is asked for no network, whatever the policy asks for; the container tiers
    enforce that, the worktree tier cannot (it is not a sandbox, #184). The worktree
    holds only the commit (nothing from the agent's session) and is removed
    afterwards, so running again checks the same commit afresh. No declared command
    returns no runs.
    """
    commands = policy.commands if policy else {}
    if not commands:
        return []
    with (
        detached_worktree(repo_path, commit) as worktree,
        _sandbox_commands(
            worktree,
            policy,
            run_id=run_id,
            workspace_id=workspace_id,
            # A check's verdict comes last, and the sandbox keeps only the start of an
            # output over its cap; the whole output is in memory already, so keep it all.
            settings=(settings or SandboxSettings.from_env()).model_copy(
                update={"output_cap_bytes": sys.maxsize}
            ),
            network=SandboxNetwork.NONE,
        ) as run_command,
    ):
        runs: list[CheckRun] = []
        for name, command in commands.items():
            started_at = datetime.now(UTC)
            output = run_command(command)
            runs.append(
                CheckRun(
                    id=name,
                    exit_code=output.exit_code,
                    timed_out=output.timed_out,
                    started_at=started_at,
                    duration_ms=output.duration_ms,
                    output_tail=_tail(output.stdout, output.stderr),
                )
            )
        return runs


__all__ = [
    "CHECK_OUTPUT_TAIL_LINES",
    "NO_DECLARED_CHECKS",
    "CheckRun",
    "RunWorkspace",
    "detached_worktree",
    "file_at",
    "prepared_workspace",
    "remove_run_worktrees",
    "run_branch",
    "run_declared_checks",
]
