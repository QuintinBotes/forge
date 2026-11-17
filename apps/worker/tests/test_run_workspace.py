"""Agents act on the main worker path (spec G §3): worktree, tool belt, sandbox."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from scf_agent.providers import (
    CliName,
    LocalCliModelClient,
    ModelClientError,
    ModelClientUnavailable,
)
from scf_agent.sandbox.settings import SandboxSettings
from scf_agent.testing import ScriptedModelClient, finish_response, tool_response
from scf_contracts import (
    AgentObjective,
    ModelClient,
    ModelRequest,
    ModelResponse,
    RunStatus,
    SandboxKind,
    StepKind,
)
from scf_worker import agent_runner as agent_runner_module
from scf_worker.agent_runner import build_agent_runner, run_agent_task, run_objective
from scf_worker.run_workspace import prepared_workspace

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
        lambda **kw: real_build(**{**kw, "model_client": scripted}),
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


class _ProviderClient:
    """Stands in for a provider's client: neither the scripted client nor a local CLI."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._scripted = ScriptedModelClient(responses=responses)

    def complete(self, request: ModelRequest) -> ModelResponse:
        return self._scripted.complete(request)


class _LocalCli(LocalCliModelClient):
    """A local-CLI client that replays responses instead of running the CLI."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(cli_name=CliName.claude)
        self._scripted = ScriptedModelClient(responses=responses)

    def complete(self, request: ModelRequest) -> ModelResponse:
        return self._scripted.complete(request)


def _configure_provider(monkeypatch: pytest.MonkeyPatch, client: ModelClient | None) -> None:
    """A provider configured with ``claude-sonnet-5`` whose client builds as ``client``,
    or fails to build when ``client`` is ``None``."""
    monkeypatch.setenv("FORGE_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.setenv("FORGE_MODEL_NAME", "claude-sonnet-5")

    def build(config: object, **_: object) -> ModelClient:
        if client is None:
            raise ModelClientError("the provider SDK is unusable here")
        return client

    monkeypatch.setattr(agent_runner_module, "build_model_client", build)


def _assisted_by(repo: Path, ref: str) -> str:
    return _git(repo, "log", "-1", "--format=%(trailers:key=Assisted-by,valueonly)", ref)


def _run_in(repo: Path, **objective: object) -> dict[str, object]:
    return run_agent_task(
        {
            "objective": "Create done.txt",
            "context": {"repo_path": str(repo), "agent_run_id": str(uuid.uuid4())},
            **objective,
        }
    )


def test_a_run_commits_with_its_model_and_records_its_base_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#214: the commit names the model the run used; the base lets the attestation tell
    a run that committed from one that did not."""
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    _configure_provider(monkeypatch, _ProviderClient(list(SCRIPT)))
    base = _git(repo, "rev-parse", "main")

    artifacts = _run_in(repo)["artifacts"]

    assert isinstance(artifacts, dict)
    assert (artifacts["base_commit"], artifacts["head_commit"] != base) == (base, True)
    assert _assisted_by(repo, artifacts["branch"]) == "scf-agent:claude-sonnet-5"


def test_a_run_on_the_scripted_client_names_no_model(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured client that fails to build falls back to the scripted one (with the
    development opt-in): no model ran, so the trailer names none."""
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    _configure_provider(monkeypatch, None)
    monkeypatch.setattr(
        agent_runner_module, "_scripted_client", lambda: ScriptedModelClient(list(SCRIPT))
    )

    artifacts = _run_in(repo)["artifacts"]

    assert isinstance(artifacts, dict)
    assert _assisted_by(repo, artifacts["branch"]) == "scf-agent"


def test_a_run_on_a_local_cli_names_the_cli_not_a_pinned_model(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local CLI runs its own model whatever the objective pins."""
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    monkeypatch.setattr(
        agent_runner_module, "_resolve_model_client", lambda _: _LocalCli(list(SCRIPT))
    )

    artifacts = _run_in(repo, model="claude-opus-5")["artifacts"]

    assert isinstance(artifacts, dict)
    assert _assisted_by(repo, artifacts["branch"]) == "scf-agent:local-cli:claude"


def _subagent_commit(repo: Path, client: ModelClient, model: str) -> str:
    """The ``Assisted-by:`` of the commit a supervised subagent makes on ``client``."""
    from scf_worker.multi_agent import _SubAgentRuntime

    objective = AgentObjective(
        objective="Create done.txt",
        model=model,
        allowed_actions=["read_repo", "write_code"],
        context={"role": "implementer"},
    )
    result = _SubAgentRuntime(client, uuid.uuid4(), repo_root=repo).run(objective)
    committed = [
        step.observation or ""
        for step in result.steps
        if step.tool_call and step.tool_call.tool == "git_commit" and step.observation
    ]
    return _assisted_by(repo, committed[-1].split()[-1])


_SUBAGENT_SCRIPT = [
    tool_response("write_file", {"path": "done.txt", "content": "yes"}),
    tool_response("git_commit", {"message": "Add done.txt"}),
    finish_response("done", confidence=0.95),
]


def test_a_subagent_commits_with_the_model_it_actually_runs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#214: the supervised path names the model the same way: the routed model on a
    provider's client, the CLI on a local CLI, nothing on the scripted client."""
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")

    assert (
        _subagent_commit(repo, _ProviderClient(list(_SUBAGENT_SCRIPT)), "claude-haiku-5")
        == "scf-agent:claude-haiku-5"
    )
    assert (
        _subagent_commit(repo, _LocalCli(list(_SUBAGENT_SCRIPT)), "claude-haiku-5")
        == "scf-agent:local-cli:claude"
    )
    assert (
        _subagent_commit(repo, ScriptedModelClient(list(_SUBAGENT_SCRIPT)), "claude-haiku-5")
        == "scf-agent"
    )


def test_remove_run_worktrees_removes_only_that_runs_worktree_once(repo: Path) -> None:
    """``forge.cleanup_worktree`` (spec K §4.11): a worktree a cancelled run
    left behind is removed, its branch kept; a second call is a no-op."""
    from scf_agent.sandbox import WorktreeSandbox
    from scf_worker.run_workspace import remove_run_worktrees, run_branch

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
