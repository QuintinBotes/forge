"""Agents act on the main worker path (spec G §3): worktree, tool belt, sandbox."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from forge_agent.providers import ModelClientUnavailable
from forge_agent.sandbox.settings import SandboxSettings
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_contracts import AgentObjective, RunStatus, SandboxKind, StepKind
from forge_worker import agent_runner as agent_runner_module
from forge_worker.agent_runner import build_agent_runner, run_agent_task, run_objective
from forge_worker.run_workspace import prepared_workspace

POLICY = """\
repo_id: sample
languages: [python]
commands:
  test: python -c "import pathlib, sys; sys.exit(0 if pathlib.Path('done.txt').exists() else 3)"
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    (root / ".forge").mkdir(parents=True)
    (root / ".forge" / "policy.yaml").write_text(POLICY)
    (root / "README.md").write_text("sample\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _objective(repo: Path, **extra: object) -> AgentObjective:
    return AgentObjective(
        objective="Create done.txt so the tests pass, then commit",
        context={"repo_path": str(repo)},
        **extra,  # type: ignore[arg-type]
    )


SCRIPT = [
    tool_response("run_tests"),
    tool_response("write_file", {"path": "done.txt", "content": "yes\n"}),
    tool_response("run_tests"),
    tool_response("git_commit", {"message": "Add done.txt"}),
    finish_response("done", confidence=0.95),
]


def test_worker_refuses_without_a_model_unless_scripted_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("FORGE_ALLOW_SCRIPTED_AGENT", raising=False)
    with pytest.raises(ModelClientUnavailable, match="FORGE_ALLOW_SCRIPTED_AGENT"):
        build_agent_runner()


def test_objective_without_a_repository_gets_no_workspace() -> None:
    with prepared_workspace(
        AgentObjective(objective="think"), run_id=uuid.uuid4(), workspace_id=uuid.uuid4()
    ) as workspace:
        assert workspace is None


def test_agent_edits_tests_and_commits_on_its_own_branch(repo: Path) -> None:
    run_id = uuid.uuid4()
    with prepared_workspace(
        _objective(repo),
        run_id=run_id,
        workspace_id=uuid.uuid4(),
        settings=SandboxSettings(kind=SandboxKind.WORKTREE),
    ) as workspace:
        assert workspace is not None
        assert workspace.branch == f"forge/run-{run_id.hex[:12]}"
        runner = build_agent_runner(
            model_client=ScriptedModelClient(responses=list(SCRIPT)),
            tools=workspace.tools,
            repo_root=workspace.path,
        )
        result = run_objective(runner, _objective(repo))
        worktree = workspace.path

    assert result.status == RunStatus.SUCCEEDED, result.error
    observed = [
        step.observation or ""
        for step in result.steps
        if step.kind == StepKind.OBSERVATION
        and step.tool_call
        and step.tool_call.tool == "run_tests"
    ]
    assert [o.split("\n")[0] for o in observed] == ["exit 3", "exit 0"]
    # The commit lives on the run branch in the clone; the worktree is gone; main is untouched.
    assert not worktree.exists()
    assert _git(repo, "show", f"{workspace.branch}:done.txt") == "yes"
    assert f"Forge-Run: {run_id}" in _git(repo, "log", "-1", "--format=%B", workspace.branch)
    assert not (repo / "done.txt").exists()


def test_policy_gate_blocks_tools_outside_allowed_actions(repo: Path) -> None:
    with prepared_workspace(
        _objective(repo),
        run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        settings=SandboxSettings(kind=SandboxKind.WORKTREE),
    ) as workspace:
        assert workspace is not None
        runner = build_agent_runner(
            model_client=ScriptedModelClient(
                responses=[
                    tool_response("write_file", {"path": "x.txt", "content": "x"}),
                    finish_response("done", confidence=0.95),
                ]
            ),
            tools=workspace.tools,
            repo_root=workspace.path,
        )
        result = run_objective(runner, _objective(repo, allowed_actions=["read_repo"]))
        assert not (workspace.path / "x.txt").exists()
    assert result.status == RunStatus.ESCALATED


def test_celery_task_runs_in_a_workspace_and_reports_the_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    scripted = ScriptedModelClient(responses=list(SCRIPT))
    real_build = agent_runner_module.build_agent_runner
    monkeypatch.setattr(
        agent_runner_module,
        "build_agent_runner",
        lambda **kw: real_build(model_client=scripted, **kw),
    )
    run_id = uuid.uuid4()
    result = run_agent_task(
        {
            "objective": "Create done.txt",
            "context": {"repo_path": str(repo), "agent_run_id": str(run_id)},
        }
    )
    assert result["status"] == RunStatus.SUCCEEDED.value
    assert result["artifacts"]["branch"] == f"forge/run-{run_id.hex[:12]}"
    assert _git(repo, "show", f"{result['artifacts']['branch']}:done.txt") == "yes"


def test_remove_run_worktrees_removes_only_that_runs_worktree_once(repo: Path) -> None:
    """``forge.cleanup_worktree`` (spec K §4.11): a worktree a cancelled run
    left behind is removed, its branch kept; a second call is a no-op."""
    from forge_agent.sandbox import WorktreeSandbox
    from forge_worker.run_workspace import remove_run_worktrees, run_branch

    gone, kept = uuid.uuid4(), uuid.uuid4()
    leftover = WorktreeSandbox(repo).create(branch=run_branch("forge", gone))
    other = WorktreeSandbox(repo).create(branch=run_branch("forge", kept))

    removed = remove_run_worktrees([repo], [gone])
    assert [p.resolve() for p in removed] == [leftover.resolve()]  # git reports real paths
    assert not leftover.exists()
    assert other.exists()
    assert run_branch("forge", gone) in _git(repo, "branch", "--list", "forge/*")

    assert remove_run_worktrees([repo], [gone]) == []
    assert remove_run_worktrees([repo], []) == []
    remove_run_worktrees([repo], [kept])
    assert not other.parent.exists()  # the temp holder goes with the worktree
