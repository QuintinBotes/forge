"""Argv construction and subprocess execution for each supported agent CLI.

Every backend disables the CLI's own tool use. The CLIs are agents; left
unconstrained they would plan and edit files on the host while Forge's own loop
also acted — two agent loops, one working tree, no sandbox. Here they are text
oracles only; all tool execution stays inside Forge's audited sandbox.

TODO(local_cli behavioural verification): the suppression flags below have
only been checked for *acceptance* by the real CLIs (they don't error), never
for *enforcement* — no test yet sends a prompt engineered to provoke a tool
call and asserts the CLI performs no tool use end to end. That test needs a
real, authenticated CLI invocation (real subscription quota), so it belongs
on an opt-in live lane (e.g. a ``live_local_cli`` pytest marker, following the
``live_model``/``live_github``/etc. convention in ``pyproject.toml``), not the
default hermetic suite. Not yet written; see
``ClaudeCliBackend``'s docstring for the same caveat spelled out.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from forge_agent.providers.local_cli.detect import CliName

__all__ = [
    "ClaudeCliBackend",
    "CliBackend",
    "CliResult",
    "CodexCliBackend",
    "MuseCliBackend",
    "backend_for",
    "run_cli",
]

#: Claude Code tool names suppressed on every invocation.
_CLAUDE_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"


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
    #: True only when ``execve`` itself failed with ``E2BIG`` (the combined
    #: argv/envp exceeded the OS limit, e.g. a very large ``--append-system-prompt``
    #: value). Distinct from ``binary_missing``: the binary is present, the
    #: input was simply too large for this call to ever have been executed.
    input_too_large: bool = False


class CliBackend(Protocol):
    needs_schema_file: bool
    #: The CLI cannot read its prompt from stdin, so the client writes it to a
    #: private temp file and passes ``prompt_path`` (the prompt stays out of argv).
    needs_prompt_file: bool

    def argv(
        self,
        *,
        system: str,
        user: str,
        schema_path: str | None,
        model: str | None = None,
        prompt_path: str | None = None,
    ) -> list[str]: ...

    def final_text(self, stdout: str) -> str:
        """The CLI's final answer extracted from its raw stdout."""
        ...

    def stdin_payload(self, *, system: str, user: str) -> str | None:
        """The text to send on stdin (then EOF), or ``None`` for none.

        The caller-supplied prompt must never be an argv token: a value that
        looks like a CLI flag (e.g. ``--dangerously-skip-permissions``) would
        otherwise be parsed as one rather than as text, since only a ``--``
        terminator (which nothing here emits before the old trailing
        positional) disables option parsing for later tokens. Sending it on
        stdin removes it from argv entirely, closing that class of bug
        structurally rather than by blacklisting leading ``-`` characters.
        """
        ...


class ClaudeCliBackend:
    """`claude -p` with a JSON result and every tool disabled.

    Layered, most-restrictive-first tool suppression, as *configured*:

    * ``--allowedTools ""`` — an empty allow-list. By construction, an empty
      allow-list should deny anything not explicitly named — the primary
      layer, intended so a future Claude Code release that ships a twelfth
      tool is denied by default instead of silently slipping past a
      hardcoded deny-list (which is fail-open by construction).
    * ``--disallowedTools <known names>`` — kept as a second, defence-in-depth
      layer naming today's known tools explicitly.
    * ``--permission-mode plan`` / ``--permission-prompts none`` — intended to
      guard the case where a permission request arises anyway: under
      ``--print`` there is no human and no SDK host to answer one, so without
      ``--permission-prompts none`` the subprocess would hang until the
      timeout instead of failing fast.

    **What is proven vs. what is configured:** as of this commit, only flag
    *acceptance* has been verified against the real CLI — a live call with
    ``--allowedTools ""`` completed and returned ``permission_denials: []``
    for a prompt ("say ok") that never attempted a tool call. That result is
    consistent both with "nothing was ever attempted" and with "something
    was attempted and blocked" — acceptance is not enforcement. No test yet
    sends a prompt engineered to provoke a tool call and observes the CLI
    perform no tool use end to end; see the module-level TODO below for the
    missing behavioural test.

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

    **The caller-supplied prompt is never an argv token (Fix round 1,
    CRITICAL).** It used to be ``argv[-1]``, with no ``--`` terminator ahead
    of it. ``claude``'s own parser scans every argv token for option syntax
    regardless of position, so a prompt that happens to look like a real flag
    (e.g. ``--dangerously-skip-permissions``, a genuine ``claude`` option) was
    parsed as that flag rather than as text — bypassing the entire tool-
    suppression invariant this class exists to enforce. A single-token form
    like ``--settings={"x":1}`` needs no second argv slot to reach the CLI's
    own configuration surface. Rejecting/escaping leading ``-`` characters
    would be a blacklist over an open-ended parser surface; instead the
    prompt is sent on stdin (via :meth:`stdin_payload`) and never appears in
    argv at all — verified live: ``echo "say ok" | claude --print
    --output-format json --allowedTools "" --disallowedTools "<tools>"
    --permission-mode plan --permission-prompts none --append-system-prompt
    "<system>"`` (no trailing positional) returned a normal
    ``{"subtype":"success", ..., "result":"ok"}`` envelope with
    ``permission_denials: []``, confirming ``--print`` reads the prompt from
    stdin when no positional is given. See
    ``test_claude_prompt_never_appears_in_argv``.
    """

    needs_schema_file = False
    needs_prompt_file = False

    def argv(
        self,
        *,
        system: str,
        user: str,
        schema_path: str | None,
        model: str | None = None,
        prompt_path: str | None = None,
    ) -> list[str]:
        return [
            "claude",
            "--print",
            *(["--model", model] if model else []),
            "--output-format",
            "json",
            "--allowedTools",
            "",
            "--disallowedTools",
            _CLAUDE_TOOLS,
            "--permission-mode",
            "plan",
            "--permission-prompts",
            "none",
            "--append-system-prompt",
            system,
        ]

    def stdin_payload(self, *, system: str, user: str) -> str | None:
        return user

    def final_text(self, stdout: str) -> str:
        return stdout


class CodexCliBackend:
    """`codex exec` with a read-only sandbox and a schema-enforced result.

    Like the claude backend, the prompt is sent on stdin rather than as an
    argv positional (Fix round 1, CRITICAL — see ``ClaudeCliBackend``'s
    docstring for the underlying argv-injection risk this closes). Confirmed
    via ``codex exec --help`` (local help text only; no live model call was
    made — the operator's codex quota was exhausted at the time of this fix):
    the ``[PROMPT]`` argument's help text states verbatim "If not provided as
    an argument (or if `-` is used), instructions are read from stdin. If
    stdin is piped and a prompt is also provided, stdin is appended as a
    `<stdin>` block" — so omitting the positional entirely (as done here) and
    piping the prompt on stdin is a documented, first-class codex input mode,
    not an inferred workaround. This is INFERRED-FROM-DOCS, not live-verified
    against the real binary; see the Task 7 fix-round-1 report for the exact
    ``--help`` transcript.
    """

    needs_schema_file = True
    needs_prompt_file = False

    def argv(
        self,
        *,
        system: str,
        user: str,
        schema_path: str | None,
        model: str | None = None,
        prompt_path: str | None = None,
    ) -> list[str]:
        argv = ["codex", "exec", "--json", "--sandbox", "read-only"]
        if model:
            argv += ["-m", model]
        if schema_path:
            argv += ["--output-schema", schema_path]
        return argv

    def stdin_payload(self, *, system: str, user: str) -> str | None:
        return f"{system}\n\n{user}"

    def final_text(self, stdout: str) -> str:
        return stdout


class MuseCliBackend:
    """`muse exec` as a text oracle: no tools, no shell, no writes, no web.

    ``muse exec`` does not read its prompt from stdin (a ``-`` positional is
    taken literally), so the prompt goes in a private temp file passed with
    ``--prompt-file``; like the other backends, the prompt is never an argv
    token. ``--workspace`` is that file's own fresh directory, so the run is
    rooted nowhere near the host's repositories. Verified offline against the
    ``echo`` provider (muse 2026-11): this argv is accepted and the final answer
    arrives as the ``text`` of the ``run.terminal.completed`` JSONL event.
    Enforcement of the tool flags against the live ``meta`` provider is
    unverified (see the module-level TODO).
    """

    needs_schema_file = True
    needs_prompt_file = True

    def argv(
        self,
        *,
        system: str,
        user: str,
        schema_path: str | None,
        model: str | None = None,
        prompt_path: str | None = None,
    ) -> list[str]:
        if not prompt_path:
            raise ValueError("muse needs a prompt file")
        argv = [
            "muse",
            "exec",
            "--json",
            "--no-session-log",
            "--preset",
            "empty",
            "--workspace",
            os.path.dirname(prompt_path),
            "--disable-write",
            "--disable-shell",
            "--disable-web-tools",
            "--disable-request-user-input",
            "--disable-reminders",
            "--approval-mode",
            "untrusted",
        ]
        if model:
            argv += ["--model", model]
        if schema_path:
            argv += ["--output-schema", schema_path]
        return [*argv, "--prompt-file", prompt_path]

    def stdin_payload(self, *, system: str, user: str) -> str | None:
        return None

    def final_text(self, stdout: str) -> str:
        """The ``text`` of the last ``run.terminal.completed`` event, or ``""``."""
        final = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("payload_type") == "run.terminal.completed":
                text = (event.get("payload") or {}).get("text")
                final = text if isinstance(text, str) else ""
        return final


_BACKENDS: dict[CliName, CliBackend] = {
    CliName.claude: ClaudeCliBackend(),
    CliName.codex: CodexCliBackend(),
    CliName.muse: MuseCliBackend(),
}


def backend_for(name: CliName) -> CliBackend:
    return _BACKENDS[name]


def run_cli(
    argv: list[str],
    *,
    timeout_s: float,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    input: str | None = None,
) -> CliResult:
    """Execute ``argv``. Never raises; failures come back as a ``CliResult``.

    ``input``, when given, is forwarded to ``runner`` as its ``input=``
    keyword (``subprocess.run``-compatible: written to the child's stdin,
    then EOF). This is how a backend's ``stdin_payload`` reaches the CLI
    instead of argv — see ``CliBackend.stdin_payload``. It also closes a
    related hang: previously, if the trailing argv prompt was ever swallowed
    by a preceding flag, the child inherited the parent's real stdin (never
    redirected) and blocked on it for the full timeout; a backend that always
    supplies ``input`` never leaves stdin dangling like that.
    """
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False, input=input
        )
    except subprocess.TimeoutExpired:
        return CliResult(stdout="", stderr="timed out", returncode=124, timed_out=True)
    except (FileNotFoundError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.E2BIG:
            # The binary IS present; execve itself refused because the
            # combined argv/envp was too large (e.g. a very large
            # --append-system-prompt value). Reporting this as
            # binary_missing would misleadingly tell the caller the CLI is
            # not installed when the input was simply oversized.
            return CliResult(
                stdout="", stderr=str(exc), returncode=1, timed_out=False, input_too_large=True
            )
        return CliResult(
            stdout="", stderr=str(exc), returncode=127, timed_out=False, binary_missing=True
        )
    except Exception as exc:
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
