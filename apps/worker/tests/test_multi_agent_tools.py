"""Role-scoped subagents act only in their own worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_contracts import ROLE_TOOLS, AgentObjective, RepoTarget, SubAgentRole
from forge_worker.multi_agent import _agent_factory


def _objective(root: Path, role: SubAgentRole, actions: list[str]) -> AgentObjective:
    return AgentObjective(
        objective="Do the assigned work",
        allowed_actions=actions,
        context={"role": role.value, "worktree_path": str(root)},
    )


@pytest.mark.parametrize("role", list(SubAgentRole))
def test_subagent_belt_intersects_role_with_objective(tmp_path: Path, role: SubAgentRole) -> None:
    model = ScriptedModelClient([finish_response("done")])
    _agent_factory(model)().run(_objective(tmp_path, role, ["read_repo", "write_code"]))
    names = {schema["name"] for schema in model.requests[0].tools}
    assert {"read_file", "search_code", "list_dir"} <= names
    assert ("write_file" in names) == ("write_code" in ROLE_TOOLS[role])
    assert ("git_commit" in names) == ("write_code" in ROLE_TOOLS[role])
    assert not {"open_pr", "push", "merge_pr"} & names


@pytest.mark.parametrize("actions", [[], ["read_repo"]])
def test_implementer_cannot_widen_objective_actions(tmp_path: Path, actions: list[str]) -> None:
    model = ScriptedModelClient([finish_response("done")])
    _agent_factory(model)().run(_objective(tmp_path, SubAgentRole.IMPLEMENTER, actions))
    names = {schema["name"] for schema in model.requests[0].tools}
    assert not {"write_file", "git_commit"} & names
    if not actions:
        assert not names


def test_subagent_writes_only_its_own_worktree(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    model = ScriptedModelClient(
        [
            tool_response("write_file", {"path": "change.txt", "content": "first"}),
            finish_response("done"),
        ]
    )
    _agent_factory(model)().run(_objective(first, SubAgentRole.IMPLEMENTER, ["write_code"]))
    assert (first / "change.txt").read_text() == "first"
    assert not (second / "change.txt").exists()
    escape = ScriptedModelClient(
        [
            tool_response("write_file", {"path": "../second/escape.txt", "content": "escape"}),
            finish_response("done"),
        ]
    )
    result = _agent_factory(escape)().run(
        _objective(first, SubAgentRole.IMPLEMENTER, ["write_code"])
    )
    assert not (second / "escape.txt").exists()
    assert any("escapes sandbox root" in (step.observation or "") for step in result.steps)


def test_policy_denial_still_blocks_a_registered_tool(tmp_path: Path) -> None:
    model = ScriptedModelClient(
        [
            tool_response("write_file", {"path": "denied.txt", "content": "bad"}),
        ]
    )
    objective = _objective(tmp_path, SubAgentRole.IMPLEMENTER, ["write_code"])
    objective.restricted_actions = ["write_code"]
    result = _agent_factory(model)().run(objective)
    assert not (tmp_path / "denied.txt").exists()
    assert result.needs_human


def test_declared_test_command_runs_in_child_sandbox(tmp_path: Path) -> None:
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "policy.yaml").write_text("repo_id: test\ncommands:\n  test: pwd\n")
    model = ScriptedModelClient([tool_response("run_tests"), finish_response("done")])
    result = _agent_factory(model)().run(
        _objective(tmp_path, SubAgentRole.TESTER, ["run_tests", "write_code"])
    )
    names = {schema["name"] for schema in model.requests[0].tools}
    assert "run_tests" in names
    assert not {"write_file", "git_commit"} & names
    assert any(str(tmp_path) in (step.observation or "") for step in result.steps)


def test_read_only_role_gets_private_worktree_and_role_model(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    default = ScriptedModelClient([])
    role_model = ScriptedModelClient(
        [tool_response("read_file", {"path": ".git"}), finish_response("done")]
    )
    objective = AgentObjective(
        objective="Review the repo",
        allowed_actions=["read_repo", "write_code"],
        repo_targets=[RepoTarget(repo="acme/widgets", base_branch="main", worktree=False)],
        context={"role": "reviewer"},
    )
    # The repository comes from the run's resolved mirror (the factory's repo_root),
    # never from repo_targets[].repo, which is a name.
    result = _agent_factory(default, repo_root=repo)(role_model).run(objective)
    assert not default.requests
    names = {schema["name"] for schema in role_model.requests[0].tools}
    assert "read_file" in names
    assert "write_file" not in names
    # The original repo has a .git directory; only the private worktree has this file.
    assert any("gitdir:" in (step.observation or "") for step in result.steps)
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count("worktree ") == 1
