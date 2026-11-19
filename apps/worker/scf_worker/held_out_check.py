"""Run one held-out scenario against a governed run's change (#286).

The scenario's files are overlaid on a fresh, detached worktree of the head commit
and its command runs there in the sandbox the declared checks get, with no
network. The worktree is removed afterwards, so the overlay never reaches the run
branch. Only the outcome is kept: never the output, which could quote the
scenario back to whoever reads it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scf_agent.sandbox import SandboxError
from scf_api.schemas.held_out import ScenarioFile
from scf_contracts import Policy, SandboxNetwork
from scf_worker.run_workspace import _sandbox_commands, detached_worktree

__all__ = ["COULD_NOT_START", "EXITED_NONZERO", "TIMED_OUT", "ScenarioRun", "run_scenario"]

#: Why a scenario failed. A scenario that did not run is never a pass.
TIMED_OUT = "timed_out"
COULD_NOT_START = "could_not_start"
EXITED_NONZERO = "exited_nonzero"
#: The shell's exit statuses for a command it could not run: not executable, not found.
_CANNOT_START = frozenset({126, 127})


@dataclass(frozen=True)
class ScenarioRun:
    """One scenario's outcome on the head commit."""

    exit_code: int | None
    timed_out: bool
    duration_ms: int
    #: ``None`` when it passed; otherwise why it failed.
    reason: str | None

    @property
    def passed(self) -> bool:
        return self.reason is None


def _overlay(worktree: Path, files: list[dict[str, Any]]) -> None:
    """Write the scenario's files into ``worktree``, refusing one that would land outside.

    The store refused such paths already (a ``ValidationError`` is a ``ValueError``);
    the change may still have committed a symlink pointing out of the checkout, so
    each target is resolved first.
    """
    root = worktree.resolve()
    for raw in files:
        file = ScenarioFile.model_validate(raw)
        target = (worktree / file.path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"{file.path} would land outside the checkout")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content)


def run_scenario(
    repo_path: Path,
    commit: str,
    *,
    files: list[dict[str, Any]],
    command: str,
    policy: Policy | None,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> ScenarioRun:
    """Overlay ``files`` on a fresh worktree of ``commit`` and run ``command`` in the sandbox.

    It passes when the command exits 0 within the sandbox's time limit. One that
    times out, cannot start (an overlay refused, a sandbox fault, or the shell's
    126 or 127) or exits otherwise fails, with the reason.
    """
    started = time.monotonic()
    with detached_worktree(repo_path, commit) as worktree:
        try:
            _overlay(worktree, files)
            with _sandbox_commands(
                worktree,
                policy,
                run_id=run_id,
                workspace_id=workspace_id,
                settings=None,
                network=SandboxNetwork.NONE,  # whatever the policy asks for
            ) as run_command:
                output = run_command(command)
        except (SandboxError, OSError, ValueError):
            elapsed = int((time.monotonic() - started) * 1000)
            return ScenarioRun(None, False, elapsed, COULD_NOT_START)
    if output.timed_out:
        reason: str | None = TIMED_OUT
    elif output.exit_code in _CANNOT_START:
        reason = COULD_NOT_START
    elif output.exit_code != 0:
        reason = EXITED_NONZERO
    else:
        reason = None
    return ScenarioRun(output.exit_code, output.timed_out, output.duration_ms, reason)
