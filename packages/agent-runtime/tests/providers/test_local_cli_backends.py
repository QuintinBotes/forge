import errno
import subprocess

import pytest

from forge_agent.providers.local_cli.backends import backend_for, run_cli
from forge_agent.providers.local_cli.detect import CliName

#: Fix round 1, CRITICAL regression fixture: a prompt that is itself a real,
#: known claude flag. Before the fix this was emitted as argv[-1] with no
#: `--` terminator ahead of it, so claude's own parser matched it as this
#: flag instead of treating it as prompt text.
_DANGEROUS_PROMPT = "--dangerously-skip-permissions"


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
    directly precede another bare value would pass every membership assertion
    above (`"--disallowedTools" in argv` is still true) while silently
    swallowing that value as an extra tool name. The only way to catch that
    class of regression is to assert on order: the token immediately after
    each variadic flag's value must itself be a `--`-prefixed flag, never a
    bare value.
    """
    argv = backend_for(CliName.claude).argv(system="S", user="U", schema_path=None)
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
            "a variadic flag would swallow it"
        )


def test_claude_prompt_never_appears_in_argv():
    """Fix round 1, CRITICAL: the prompt must never be an argv token at all.

    A prompt that is itself a real claude flag (--dangerously-skip-permissions)
    must not appear anywhere in argv — including as the value of
    --append-system-prompt, which would indicate the fix leaked the prompt
    back onto argv through a different flag. It must be delivered only via
    stdin_payload.
    """
    argv = backend_for(CliName.claude).argv(system="S", user=_DANGEROUS_PROMPT, schema_path=None)
    assert _DANGEROUS_PROMPT not in argv
    assert backend_for(CliName.claude).stdin_payload(system="S", user=_DANGEROUS_PROMPT) == (
        _DANGEROUS_PROMPT
    )


def test_claude_stdin_payload_is_the_user_prompt():
    payload = backend_for(CliName.claude).stdin_payload(system="S", user="U")
    assert payload == "U"


def test_codex_argv_is_read_only_and_schema_bound():
    argv = backend_for(CliName.codex).argv(system="S", user="U", schema_path="/tmp/s.json")
    assert argv[:2] == ["codex", "exec"]
    assert "--sandbox" in argv and "read-only" in argv
    assert "--output-schema" in argv and "/tmp/s.json" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_argv_never_carries_a_prompt_positional():
    """Fix round 1, CRITICAL: codex now reads its prompt from stdin too (per
    `codex exec --help`: "If not provided as an argument ... instructions are
    read from stdin"), so argv must never contain a trailing prompt token —
    dangerous or otherwise.
    """
    argv = backend_for(CliName.codex).argv(system="S", user=_DANGEROUS_PROMPT, schema_path=None)
    assert _DANGEROUS_PROMPT not in argv
    assert argv == ["codex", "exec", "--json", "--sandbox", "read-only"]


def test_codex_stdin_payload_combines_system_and_user():
    payload = backend_for(CliName.codex).stdin_payload(system="S", user="U")
    assert payload == "S\n\nU"


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


def test_run_cli_forwards_input_to_the_runner_as_stdin():
    """Fix round 1, CRITICAL: the prompt reaches the CLI via stdin, not argv —
    so run_cli must actually forward it to the runner as `input=`.
    """
    seen: dict[str, object] = {}

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="out", stderr="")

    run_cli(["x"], timeout_s=1.0, runner=runner, input="the prompt")
    assert seen["input"] == "the prompt"


def test_run_cli_distinguishes_oversized_input_from_missing_binary():
    """Minor: E2BIG means the binary is present but the call (argv/envp) was
    too large for execve — reporting that as binary_missing would wrongly
    tell the caller the CLI is not installed.
    """

    def runner(argv, **kwargs):
        raise OSError(errno.E2BIG, "Argument list too long")

    result = run_cli(["x"], timeout_s=1.0, runner=runner)
    assert result.input_too_large is True
    assert result.binary_missing is False
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
    argv = backend_for(name).argv(
        system="S", user="U", schema_path="/tmp/s.json", prompt_path="/tmp/d/prompt.txt"
    )
    joined = " ".join(argv)
    assert (
        ("--disallowedTools" in joined)
        or ("--sandbox read-only" in joined.replace("  ", " "))
        or {"--disable-shell", "--disable-write", "--disable-web-tools"} <= set(argv)
    )


def test_muse_prompt_is_a_file_never_an_argv_token():
    argv = backend_for(CliName.muse).argv(
        system="S",
        user=_DANGEROUS_PROMPT,
        schema_path="/tmp/s.json",
        model="muse-wonder-full-1.0",
        prompt_path="/tmp/d/prompt.txt",
    )
    assert _DANGEROUS_PROMPT not in argv
    assert argv[-2:] == ["--prompt-file", "/tmp/d/prompt.txt"]
    assert argv[argv.index("--workspace") + 1] == "/tmp/d"
    assert argv[argv.index("--preset") + 1] == "empty"
    assert argv[argv.index("--model") + 1] == "muse-wonder-full-1.0"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert backend_for(CliName.muse).stdin_payload(system="S", user="U") is None
    assert backend_for(CliName.muse).needs_prompt_file is True


def test_muse_final_text_is_the_terminal_event_text():
    stdout = "\n".join(
        [
            "not json",
            '{"payload_type":"run.output.delta","payload":{"text":"partial"}}',
            '{"payload_type":"run.terminal.completed","payload":{"text":"final"}}',
        ]
    )
    assert backend_for(CliName.muse).final_text(stdout) == "final"
    assert backend_for(CliName.muse).final_text('{"payload_type":"run.usage"}') == ""


def test_model_flag_reaches_claude_and_codex_argv():
    claude = backend_for(CliName.claude).argv(system="S", user="U", schema_path=None, model="m1")
    codex = backend_for(CliName.codex).argv(system="S", user="U", schema_path=None, model="m2")
    assert claude[claude.index("--model") + 1] == "m1"
    assert codex[codex.index("-m") + 1] == "m2"
    assert "--model" not in backend_for(CliName.claude).argv(system="S", user="U", schema_path=None)
