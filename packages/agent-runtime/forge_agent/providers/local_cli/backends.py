"""Argv construction and subprocess execution for each supported agent CLI.

Every backend disables the CLI's own tool use. The CLIs are agents; left
unconstrained they would plan and edit files on the host while Forge's own loop
also acted — two agent loops, one working tree, no sandbox. Here they are text
oracles only; all tool execution stays inside Forge's audited sandbox.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from forge_agent.providers.local_cli.detect import CliName

__all__ = ["CliBackend", "CliResult", "ClaudeCliBackend", "CodexCliBackend", "backend_for", "run_cli"]

#: Claude Code tool names suppressed on every invocation.
_CLAUDE_TOOLS = (
    "Bash,Edit,Write,Read,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
)


@dataclass(frozen=True)
class CliResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


class CliBackend(Protocol):
    needs_schema_file: bool

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]: ...


class ClaudeCliBackend:
    """`claude -p` with a JSON result and every tool disabled.

    Two layers of tool suppression: ``--disallowedTools`` should prevent a
    permission request from ever arising, and ``--permission-prompts none``
    guards the case where one arises anyway — under ``--print`` there is no
    human and no SDK host to answer, so without this the subprocess would hang
    until the timeout instead of failing fast.
    """

    needs_schema_file = False

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]:
        return [
            "claude",
            "--print",
            "--output-format", "json",
            "--permission-mode", "plan",
            "--permission-prompts", "none",
            "--disallowedTools", _CLAUDE_TOOLS,
            "--append-system-prompt", system,
            user,
        ]


class CodexCliBackend:
    """`codex exec` with a read-only sandbox and a schema-enforced result."""

    needs_schema_file = True

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]:
        argv = ["codex", "exec", "--json", "--sandbox", "read-only"]
        if schema_path:
            argv += ["--output-schema", schema_path]
        argv.append(f"{system}\n\n{user}")
        return argv


_BACKENDS: dict[CliName, CliBackend] = {
    CliName.claude: ClaudeCliBackend(),
    CliName.codex: CodexCliBackend(),
}


def backend_for(name: CliName) -> CliBackend:
    return _BACKENDS[name]


def run_cli(
    argv: list[str],
    *,
    timeout_s: float,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CliResult:
    """Execute ``argv``. Never raises; failures come back as a ``CliResult``."""
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return CliResult(stdout="", stderr="timed out", returncode=124, timed_out=True)
    except (FileNotFoundError, OSError) as exc:
        return CliResult(stdout="", stderr=str(exc), returncode=127, timed_out=False)
    return CliResult(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=completed.returncode,
    )
