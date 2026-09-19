import subprocess

import pytest

from forge_agent.providers.local_cli.backends import backend_for, run_cli
from forge_agent.providers.local_cli.detect import CliName


def test_claude_argv_disables_tools_and_prints():
    argv = backend_for(CliName.claude).argv(system="S", user="U", schema_path=None)
    assert "--print" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "", "allow-list must be empty (fail-closed)"
    assert "--disallowedTools" in argv
    assert "--permission-mode" in argv
    assert "--permission-prompts" in argv and "none" in argv
    assert "--dangerously-skip-permissions" not in argv


#: claude's --allowedTools/--disallowedTools are documented as variadic
#: (`<tools...>`): each greedily consumes every following bare argv token as
#: another tool name, right up to the next `--`-prefixed flag. Verified live
#: against the real binary: `--disallowedTools "Bash,Edit" "say ok"` with
#: nothing between the tools value and the trailing prompt caused the CLI to
#: swallow "say ok" as a tool name and report no prompt was given at all.
_VARIADIC_TOOL_FLAGS = ("--allowedTools", "--disallowedTools")


def test_claude_argv_terminates_variadic_flags_before_bare_values():
    """Pin the ordering invariant, not just flag membership.

    A refactor that moved a variadic flag (e.g. --disallowedTools) to
    directly precede the trailing user prompt would pass every membership
    assertion above (`"--disallowedTools" in argv` is still true) while
    silently swallowing the user prompt as an extra tool name — the CLI
    would receive no prompt at all. The only way to catch that class of
    regression is to assert on order: the token immediately after each
    variadic flag's value must itself be a `--`-prefixed flag, never a bare
    value (including the trailing user prompt).
    """
    argv = backend_for(CliName.claude).argv(system="S", user="U", schema_path=None)
    assert argv[-1] == "U", "the user prompt must be the final positional"
    for flag in _VARIADIC_TOOL_FLAGS:
        assert flag in argv
        value_index = argv.index(flag) + 1
        terminator_index = value_index + 1
        assert terminator_index < len(argv), (
            f"{flag}'s value has nothing after it to terminate the variadic list"
        )
        terminator = argv[terminator_index]
        assert terminator.startswith("--"), (
            f"{flag}'s value is immediately followed by bare token {terminator!r}; "
            "a variadic flag would swallow it (and could swallow the prompt)"
        )


def test_codex_argv_is_read_only_and_schema_bound():
    argv = backend_for(CliName.codex).argv(system="S", user="U", schema_path="/tmp/s.json")
    assert argv[:2] == ["codex", "exec"]
    assert "--sandbox" in argv and "read-only" in argv
    assert "--output-schema" in argv and "/tmp/s.json" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_declares_it_needs_a_schema_file():
    assert backend_for(CliName.codex).needs_schema_file is True
    assert backend_for(CliName.claude).needs_schema_file is False


def test_run_cli_captures_output():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="out", stderr="err")

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.stdout == "out"
    assert result.returncode == 0
    assert result.timed_out is False


def test_run_cli_marks_timeout_rather_than_raising():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.timed_out is True
    assert result.returncode != 0


def test_run_cli_survives_missing_binary():
    def runner(argv, **kwargs):
        raise FileNotFoundError("no such binary")

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.returncode != 0
    assert result.timed_out is False
    assert result.binary_missing is True


def test_run_cli_real_127_exit_is_not_reported_as_binary_missing():
    """127 is a shell convention, not a marker we own: a real process can
    legitimately exit 127 for its own reasons. Only a synthesized failure
    (the runner itself raising FileNotFoundError/OSError) should set
    binary_missing; callers must not infer it from the numeric code alone.
    """

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr="command not found")

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.returncode == 127
    assert result.binary_missing is False
    assert result.timed_out is False


def test_run_cli_never_raises_on_unexpected_runner_exception():
    """run_cli's invariant is "never raises", not "never raises the two
    exceptions we anticipated". ``runner`` is caller-injectable, so any
    exception it can produce - a ValueError, a UnicodeDecodeError from
    ``text=True`` decoding, anything - must fold into a CliResult.
    """

    def runner(argv, **kwargs):
        raise ValueError("unexpected failure from the runner")

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.returncode != 0
    assert result.timed_out is False
    assert result.binary_missing is False
    assert "unexpected failure" in result.stderr


@pytest.mark.parametrize("name", list(CliName))
def test_every_backend_suppresses_tools(name):
    """Global constraint: no backend may ever omit tool suppression."""
    argv = backend_for(name).argv(system="S", user="U", schema_path="/tmp/s.json")
    joined = " ".join(argv)
    assert ("--disallowedTools" in joined) or ("--sandbox read-only" in joined.replace("  ", " "))
