"""The main-path agent tool belt (spec G §3.2): confined, declared, local-only."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge_agent.tools import agent_tool_belt
from forge_contracts import CommandOutput


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def handler():\n    return 'TODO: implement'\n")
    (root / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\ntouch HOOK_RAN\nexit 1\n")
    (root / ".git" / "hooks" / "pre-commit").chmod(0o755)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "init",
    )
    return root


def test_search_code_finds_literal_matches_with_locations(repo: Path) -> None:
    belt = agent_tool_belt(repo, run_id="r1")
    result = belt.dispatch("search_code", {"query": "TODO"})
    assert result.ok
    assert result.output == "src/app.py:2: return 'TODO: implement'"


def test_search_code_treats_query_as_literal_not_regex(repo: Path) -> None:
    belt = agent_tool_belt(repo, run_id="r1")
    assert belt.dispatch("search_code", {"query": "(a+)+$"}).output == "no matches"


def test_search_code_cannot_escape_the_worktree(repo: Path) -> None:
    belt = agent_tool_belt(repo, run_id="r1")
    result = belt.dispatch("search_code", {"query": "x", "path": "../"})
    assert not result.ok
    assert "escapes sandbox root" in (result.error or "")


def test_command_tools_exist_only_for_declared_commands_with_a_sandbox(repo: Path) -> None:
    ran: list[str] = []

    def runner(command: str) -> CommandOutput:
        ran.append(command)
        return CommandOutput(exit_code=1, stdout="1 failed\n", stderr="")

    no_sandbox = agent_tool_belt(repo, run_id="r1", commands={"test": "pytest -q"})
    assert not no_sandbox.has("run_tests")

    belt = agent_tool_belt(repo, run_id="r1", commands={"test": "pytest -q"}, run_command=runner)
    assert belt.has("run_tests") and not belt.has("run_lint")
    result = belt.dispatch("run_tests", {"command": "rm -rf /"})
    assert ran == ["pytest -q"], "the model can never choose the command text"
    assert result.ok and result.data == {"exit_code": 1, "timed_out": False}
    assert result.output.startswith("exit 1")


def test_command_tools_report_timeouts(repo: Path) -> None:
    belt = agent_tool_belt(
        repo,
        run_id="r1",
        commands={"lint": "ruff check ."},
        run_command=lambda _: CommandOutput(exit_code=124, timed_out=True),
    )
    assert belt.dispatch("run_lint", {}).output.startswith("timed out")


def test_policy_actions_map_to_role_tool_names(repo: Path) -> None:
    belt = agent_tool_belt(
        repo, run_id="r1", commands={"test": "t"}, run_command=lambda _: CommandOutput(exit_code=0)
    )
    assert belt.action_for("search_code") == "read_repo"
    assert belt.action_for("run_tests") == "run_tests"
    assert belt.action_for("git_commit") == "write_code"


def test_git_commit_commits_locally_with_trailer_and_skips_repo_hooks(repo: Path) -> None:
    belt = agent_tool_belt(repo, run_id="run-42")
    belt.dispatch("write_file", {"path": "src/app.py", "content": "def handler():\n    return 1\n"})
    result = belt.dispatch("git_commit", {"message": "Implement handler"})
    assert result.ok, result.error
    assert result.data["sha"] == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "log", "-1", "--format=%B").endswith("Forge-Run: run-42")
    assert not (repo / "HOOK_RAN").exists(), "repository hooks must never run on the host"


def test_git_commit_with_nothing_staged_is_an_error(repo: Path) -> None:
    result = agent_tool_belt(repo, run_id="r1").dispatch("git_commit", {"message": "noop"})
    assert not result.ok
    assert result.error == "nothing to commit"


def test_there_is_no_push_or_pr_tool(repo: Path) -> None:
    names = set(agent_tool_belt(repo, run_id="r1").names())
    assert names.isdisjoint({"open_pr", "git_push", "push", "merge"})
