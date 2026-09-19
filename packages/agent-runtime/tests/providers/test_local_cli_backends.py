import subprocess

import pytest

from forge_agent.providers.local_cli.backends import backend_for, run_cli
from forge_agent.providers.local_cli.detect import CliName


def test_claude_argv_disables_tools_and_prints():
    argv = backend_for(CliName.claude).argv(system="S", user="U", schema_path=None)
    assert "--print" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--disallowedTools" in argv
    assert "--permission-mode" in argv
    assert "--permission-prompts" in argv and "none" in argv
    assert "--dangerously-skip-permissions" not in argv


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


@pytest.mark.parametrize("name", list(CliName))
def test_every_backend_suppresses_tools(name):
    """Global constraint: no backend may ever omit tool suppression."""
    argv = backend_for(name).argv(system="S", user="U", schema_path="/tmp/s.json")
    joined = " ".join(argv)
    assert ("--disallowedTools" in joined) or ("--sandbox read-only" in joined.replace("  ", " "))
