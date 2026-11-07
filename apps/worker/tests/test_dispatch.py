"""Dispatcher (spec G §4a): mirrors stay fresh; tasks map onto objectives."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from forge_contracts import ExecutionMode
from forge_db.models import Task
from forge_worker import dispatch as dispatch_module
from forge_worker.dispatch import ensure_mirror, objective_for_task


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "one")
    return src


def test_mirror_is_cloned_then_fetched(upstream: Path, tmp_path: Path) -> None:
    root = tmp_path / "mirrors"
    path = ensure_mirror("acme/widgets", root=root, remote_url=str(upstream))
    assert path == root / "acme" / "widgets"
    first = _git(path, "rev-parse", "main")

    (upstream / "a.txt").write_text("2\n")
    _git(upstream, "commit", "-q", "-am", "two")
    ensure_mirror("acme/widgets", root=root, remote_url=str(upstream))
    assert _git(path, "rev-parse", "main") == _git(upstream, "rev-parse", "main") != first


def test_fetch_keeps_unpushed_run_branches(upstream: Path, tmp_path: Path) -> None:
    root = tmp_path / "mirrors"
    path = ensure_mirror("acme/widgets", root=root, remote_url=str(upstream))
    _git(path, "branch", "forge/run-unpushed", "main")
    ensure_mirror("acme/widgets", root=root, remote_url=str(upstream))
    assert _git(path, "branch", "--list", "forge/run-unpushed")


@pytest.mark.parametrize("name", ["widgets", "acme/", "/widgets", "../x", "acme/.."])
def test_mirror_rejects_bad_repository_names(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ensure_mirror(name, root=tmp_path, remote_url="unused")


def test_mirror_token_reaches_git_through_env_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        dispatch_module.subprocess, "run", lambda argv, **kw: calls.append((argv, kw["env"]))
    )
    ensure_mirror(
        "acme/widgets", root=tmp_path, remote_url="https://x/acme/widgets.git", token="ghs_SECRET"
    )
    ((argv, env),) = calls
    assert "ghs_SECRET" not in " ".join(argv)
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"


def test_task_maps_onto_an_objective(tmp_path: Path) -> None:
    workspace_id = uuid.uuid4()
    task = Task(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        project_id=uuid.uuid4(),
        key="TASK-12",
        title="Add rate limiting",
        description="Limit login attempts",
        execution_mode=ExecutionMode.SINGLE_AGENT,
        repo_targets=[{"repo": "acme/widgets", "base_branch": "main"}],
        acceptance_criteria=[{"id": "A1", "text": "5 attempts per minute"}],
        allowed_actions=["read_repo", "write_code", "run_tests"],
        restricted_actions=["open_pr"],
    )
    run_id = uuid.uuid4()
    objective = objective_for_task(
        task, repo_path=tmp_path, agent_run_id=run_id, repo_full_name="acme/widgets"
    )
    assert objective.key == "TASK-12"
    assert objective.objective == "Add rate limiting"
    assert [c.id for c in objective.acceptance_criteria] == ["A1"]
    assert objective.allowed_actions == ["read_repo", "write_code", "run_tests"]
    assert objective.primary_repo_target is not None
    assert objective.primary_repo_target.repo == "acme/widgets"
    assert objective.context == {
        "repo_path": str(tmp_path),
        "repo_full_name": "acme/widgets",
        "workspace_id": str(workspace_id),
        "agent_run_id": str(run_id),
    }


def test_agent_runs_from_a_bare_mirror(upstream: Path, tmp_path: Path) -> None:
    from forge_agent.sandbox.settings import SandboxSettings
    from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
    from forge_contracts import RunStatus, SandboxKind
    from forge_worker.agent_runner import build_agent_runner, run_objective
    from forge_worker.run_workspace import prepared_workspace

    mirror = ensure_mirror("acme/widgets", root=tmp_path / "m", remote_url=str(upstream))
    task = Task(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        key="TASK-1",
        title="Write b.txt",
        execution_mode=ExecutionMode.SINGLE_AGENT,
        repo_targets=[{"repo": "acme/widgets"}],
        acceptance_criteria=[],
        allowed_actions=[],
        restricted_actions=[],
    )
    run_id = uuid.uuid4()
    objective = objective_for_task(
        task, repo_path=mirror, agent_run_id=run_id, repo_full_name="acme/widgets"
    )
    script = [
        tool_response("write_file", {"path": "b.txt", "content": "b\n"}),
        tool_response("git_commit", {"message": "Add b"}),
        finish_response("done", confidence=0.95),
    ]
    with prepared_workspace(
        objective,
        run_id=run_id,
        workspace_id=task.workspace_id,
        settings=SandboxSettings(kind=SandboxKind.WORKTREE),
    ) as workspace:
        assert workspace is not None
        runner = build_agent_runner(
            model_client=ScriptedModelClient(responses=script),
            tools=workspace.tools,
            repo_root=workspace.path,
        )
        result = run_objective(runner, objective)
        branch = workspace.branch
    assert result.status == RunStatus.SUCCEEDED
    assert _git(mirror, "show", f"{branch}:b.txt") == "b"
