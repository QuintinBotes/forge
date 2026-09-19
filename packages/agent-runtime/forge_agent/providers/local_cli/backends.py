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
    #: True only when the binary itself could not be found/executed (a
    #: synthesized failure). A real process can legitimately exit 127, so
    #: callers must key off this field, never off ``returncode == 127``.
    binary_missing: bool = False


class CliBackend(Protocol):
    needs_schema_file: bool

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]: ...


class ClaudeCliBackend:
    """`claude -p` with a JSON result and every tool disabled.

    Layered, most-restrictive-first tool suppression:

    * ``--allowedTools ""`` — an empty allow-list, fail-closed: nothing is
      permitted unless explicitly named. This is the primary layer, so a
      future Claude Code release that ships a twelfth tool is denied by
      default instead of silently slipping past a hardcoded deny-list
      (which is fail-open by construction).
    * ``--disallowedTools <known names>`` — kept as a second, defence-in-depth
      layer naming today's known tools explicitly.
    * ``--permission-mode plan`` / ``--permission-prompts none`` — guard the
      case where a permission request arises anyway: under ``--print`` there
      is no human and no SDK host to answer one, so without
      ``--permission-prompts none`` the subprocess would hang until the
      timeout instead of failing fast.

    ``--allowedTools``/``--disallowedTools`` are documented by ``claude
    --help`` as variadic (``<tools...>``): each greedily consumes every
    following bare (non-``--``-prefixed) argv token as another tool name,
    including a trailing prompt with nothing to terminate it. Verified live:
    ``claude --print ... --disallowedTools "Bash,Edit" "say ok"`` swallowed
    ``"say ok"`` as a tool name and reported no prompt was given. Every
    variadic flag's value here is therefore immediately followed by another
    ``--`` flag token, never by a bare value — see
    ``test_claude_argv_terminates_variadic_flags_before_bare_values``, which
    pins this ordering invariant so a reordering refactor cannot reintroduce
    the bug silently.
    """

    needs_schema_file = False

    def argv(self, *, system: str, user: str, schema_path: str | None) -> list[str]:
        return [
            "claude",
            "--print",
            "--output-format", "json",
            "--allowedTools", "",
            "--disallowedTools", _CLAUDE_TOOLS,
            "--permission-mode", "plan",
            "--permission-prompts", "none",
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
        return CliResult(
            stdout="", stderr=str(exc), returncode=127, timed_out=False, binary_missing=True
        )
    except Exception as exc:  # noqa: BLE001 - this seam's invariant is "never raises" (mirrors
        # detect.py): ``runner`` is caller-injectable, so any exception it can raise — a decoding
        # error from ``text=True``, a bug in a test double, anything — must fold into a CliResult
        # rather than propagate. 1 is a generic failure marker; it carries no sentinel meaning of
        # its own the way 124/127 do not (see ``timed_out``/``binary_missing``).
        return CliResult(stdout="", stderr=str(exc), returncode=1, timed_out=False)
    return CliResult(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=completed.returncode,
    )
