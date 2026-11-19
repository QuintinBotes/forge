"""Run one held-out scenario against a governed run's change (#286).

The scenario's files are overlaid on a fresh, detached worktree of the head commit
and its command runs there in the sandbox the declared checks get, with no
network and its own time limit. The worktree is removed afterwards, so the overlay
never reaches the run branch. Only the outcome is kept: never the output, which
could quote the scenario back to whoever reads it.

The scenario runs in the change's own checkout, so a change that hooks into the
test run (a ``conftest.py``, a module shadowing the runner) could make it pass
whatever it checks. :func:`changed_test_hooks` names such files; while there are
any, no scenario runs. It is a deny-list, not a boundary.
"""

from __future__ import annotations

import json
import os
import shlex
import time
import tomllib
import uuid
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from scf_agent.sandbox import SandboxError
from scf_api.schemas.held_out import ScenarioFile
from scf_contracts import Policy, SandboxNetwork
from scf_worker.run_workspace import _git_out, _sandbox_commands, detached_worktree, file_at

__all__ = [
    "COULD_NOT_START",
    "DEFAULT_SCENARIO_TIMEOUT_SECONDS",
    "EXITED_NONZERO",
    "HOOKS_CHANGED",
    "TIMED_OUT",
    "ScenarioRun",
    "changed_test_hooks",
    "run_scenario",
    "scenario_timeout",
]

#: Why a scenario failed. A scenario that did not run is never a pass.
TIMED_OUT = "timed_out"
COULD_NOT_START = "could_not_start"
EXITED_NONZERO = "exited_nonzero"
#: The shell's exit statuses for a command it could not run: not executable, not found.
_CANNOT_START = frozenset({126, 127})
#: One scenario's limit: well inside the check stage's (1,800 s by default).
DEFAULT_SCENARIO_TIMEOUT_SECONDS = 300
#: The pause reason when the change touches a file that hooks into the test run.
HOOKS_CHANGED = "held_out_test_hooks_changed"

#: Files that hook into a test run wherever they are, by name or pattern (sec_001).
_HOOK_FILES = (
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "sitecustomize.py",
    "usercustomize.py",
    "*.pth",
    "jest.config.*",
    "vitest.config.*",
    ".mocharc*",
)
#: The shell's operators: the word after one is a program again.
_OPERATOR_CHARS = set("();<>|&")


def scenario_timeout() -> int:
    """``FORGE_HELD_OUT_TIMEOUT_SECONDS`` — the most one scenario's command may run."""
    raw = os.environ.get("FORGE_HELD_OUT_TIMEOUT_SECONDS", "").strip()
    return int(raw) if raw else DEFAULT_SCENARIO_TIMEOUT_SECONDS


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


def _programs(command: str) -> set[str]:
    """The programs ``command`` runs and the modules it runs with ``-m``."""
    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: the shell refuses the command too
        return set()
    names: set[str] = set()
    at_program = True
    for i, token in enumerate(tokens):
        if set(token) <= _OPERATOR_CHARS:
            at_program = True
        elif at_program and "=" not in token:  # ``VAR=value`` sets the environment
            names.add(PurePosixPath(token).name)
            at_program = False
        if token == "-m" and i + 1 < len(tokens):
            names.add(tokens[i + 1].split(".")[0])
    return names


def _test_setup(name: str, text: str) -> dict[str, Any] | None:
    """The part of a ``pyproject.toml`` or ``package.json`` that sets up a test run;
    ``None`` when the file does not parse, which counts as a change."""
    try:
        if name == "pyproject.toml":
            tool = tomllib.loads(text).get("tool", {})
            return {k: v for k, v in tool.items() if k.startswith("pytest")}
        data = json.loads(text) if text.strip() else {}
        return {k: data.get(k) for k in ("scripts", "jest", "mocha")}
    except (ValueError, AttributeError):  # a decode error, or not a table at the top
        return None


def changed_test_hooks(repo: Path, base: str, head: str, commands: list[str]) -> list[str]:
    """The files ``base...head`` adds, changes or removes that hook into a test run.

    Any ``conftest.py``, ``pytest.ini``, ``tox.ini``, ``setup.cfg``,
    ``sitecustomize.py``, ``usercustomize.py``, ``*.pth``, ``jest.config.*``,
    ``vitest.config.*`` or ``.mocharc*``; a ``pyproject.toml`` whose ``[tool.pytest*]``
    tables changed; a ``package.json`` whose ``scripts``, ``jest`` or ``mocha`` changed;
    and a top-level module or package named like a program, or ``-m`` module, of
    ``commands``.
    """
    programs = set().union(*(_programs(c) for c in commands))
    # -z: git would otherwise quote an unusual path, and its name would not match.
    changed = _git_out(repo, "diff", "--name-only", "-z", "--no-renames", f"{base}...{head}")
    found = []
    for path in filter(None, changed.split("\0")):
        parts = PurePosixPath(path).parts
        name = parts[-1].lower()  # a case-insensitive file system loads Conftest.py too
        if name in ("pyproject.toml", "package.json"):
            after = _test_setup(name, file_at(repo, head, path))
            hooks = after is None or after != _test_setup(name, file_at(repo, base, path))
        else:
            top = parts[0].removesuffix(".py") if len(parts) == 1 else parts[0]
            hooks = any(fnmatchcase(name, p) for p in _HOOK_FILES) or top in programs
        if hooks:
            found.append(path)
    return found


def _overlay(worktree: Path, files: list[dict[str, Any]]) -> None:
    """Write the scenario's files into ``worktree``, never through a link the change made.

    The store refused paths that leave the checkout already (a ``ValidationError`` is
    a ``ValueError``). A link in a file's directory path is refused, and a link at the
    file's own path is replaced, so the overlay cannot land outside the checkout or
    overwrite the code under test.
    """
    root = worktree.resolve()
    for raw in files:
        file = ScenarioFile.model_validate(raw)
        target = root / file.path
        if target.parent.resolve() != target.parent:
            raise ValueError(f"{file.path} lies under a link")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
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

    It passes when the command exits 0 within :func:`scenario_timeout` (or the
    sandbox's own limit, if lower). One that times out, cannot start (an overlay
    refused, a sandbox fault, or the shell's 126 or 127) or exits otherwise fails,
    with the reason.
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
                timeout_s=scenario_timeout(),
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
