"""``forge.run_agent`` and ``forge.pause_and_notify`` for a workflow run (#250 criteria 3, 5, 6).

Through ``build_activities`` with ``FORGE_ALLOW_FAKE_ACTIVITIES`` unset, on a local
bare mirror, the worktree sandbox and the offline scripted model (tests only).
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

import scf_worker.agent_runner as agent_runner_module
from scf_db.base import Base
from scf_db.models import (
    AgentRun,
    AgentRunEvent,
    Project,
    RepositoryConnection,
    Task,
    WorkflowRun,
    Workspace,
)
from scf_db.models.enums import ExecutionMode, RunStatus
from scf_worker.temporal_main import build_activities
from scf_workflow.temporal.payloads import (
    AgentRunResultDTO,
    NotifyInput,
    ResumeAgentInput,
    RunAgentInput,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a6")
REPO = "acme/app"


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    # The run body records the agent run through the worker's global factory.
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: session_factory)
    yield session_factory
    engine.dispose()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    monkeypatch.setenv("FORGE_ALLOW_SCRIPTED_AGENT", "1")
    for name in (
        "FORGE_ALLOW_FAKE_ACTIVITIES",
        "FORGE_MODEL_PROVIDER",
        "FORGE_RECORD_RUNS",
        "FORGE_GITHUB_APP_ID",
        "FORGE_GITHUB_INSTALLATION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("app\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    path = root / str(WS) / "acme" / "app"
    path.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(path))
    return path


def _seed(factory: sessionmaker[Session], *, connected: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        if connected:
            s.add(RepositoryConnection(workspace_id=WS, repo_id=REPO, name=REPO))
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-7",
            title="Add a health endpoint",
            repo_targets=[{"repo": REPO, "base_branch": "main", "branch_prefix": "scf"}],
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.commit()
        return run.id, task.id


def _input(run_id: uuid.UUID, task_id: uuid.UUID, key: str = "k-0") -> RunAgentInput:
    return RunAgentInput(
        workflow_run_id=run_id, task_id=task_id, workspace_id=WS, attempt=0, idempotency_key=key
    )


async def _run(factory: sessionmaker[Session], inp: RunAgentInput) -> AgentRunResultDTO:
    result = await ActivityEnvironment().run(build_activities(factory).run_agent, inp)
    assert isinstance(result, AgentRunResultDTO)
    return result


async def test_the_agent_runs_the_tasks_objective_in_a_worktree_of_the_run_branch(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    run_id, task_id = _seed(factory)

    result = await _run(factory, _input(run_id, task_id))

    assert result.status == "succeeded"
    assert result.branch_name is not None and result.branch_name.startswith("scf/")
    assert result.head_commit_sha == _git(mirror, "rev-parse", f"refs/heads/{result.branch_name}")
    with factory() as s:
        row = s.get(AgentRun, result.agent_run_id)
        assert row is not None
        assert row.workflow_run_id == run_id
        assert row.task_id == task_id
        assert row.status == RunStatus.SUCCEEDED
        assert row.inputs["objective"] == "Add a health endpoint"
        assert row.output is not None and row.output["artifacts"]["branch"] == result.branch_name
    assert len(_git(mirror, "worktree", "list").splitlines()) == 1, "the run's worktree is gone"


async def test_the_agent_runs_the_objective_kept_on_the_run(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    """#171: an issue-triggered run keeps the objective built from the re-fetched
    issue; the agent runs that, not one rebuilt from the task title."""
    run_id, task_id = _seed(factory)
    kept = {
        "objective": "Make widgets accessible",
        "description": "Fix the widget.",
        "task_id": str(task_id),
        "repo_targets": [{"repo": REPO, "base_branch": "main", "branch_prefix": "scf"}],
        "acceptance_criteria": [{"id": "AC-1", "text": "Widget renders"}],
        "context": {},
    }
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        run.context = {**run.context, "trigger": {"source": "issue", "objective": kept}}
        s.commit()

    result = await _run(factory, _input(run_id, task_id))

    with factory() as s:
        row = s.get(AgentRun, result.agent_run_id)
        assert row is not None
        assert row.inputs["objective"] == "Make widgets accessible"
        assert row.inputs["description"] == "Fix the widget."
        assert [c["text"] for c in row.inputs["acceptance_criteria"]] == ["Widget renders"]
        context = row.inputs["context"]
        assert context["agent_run_id"] == str(result.agent_run_id)
        assert context["workflow_run_id"] == str(run_id)
        assert context["repo_full_name"] == REPO


async def test_a_repeated_idempotency_key_finds_the_same_agent_run(
    factory: sessionmaker[Session], mirror: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, task_id = _seed(factory)
    first = await _run(factory, _input(run_id, task_id))
    calls: list[object] = []
    monkeypatch.setattr(agent_runner_module, "run_objective", lambda *a: calls.append(a))

    again = await _run(factory, _input(run_id, task_id))

    assert again == first
    assert calls == [], "the agent did not run a second time"
    with factory() as s:
        assert len(s.scalars(select(AgentRun)).all()) == 1


async def test_a_new_attempt_key_is_a_new_agent_run(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    run_id, task_id = _seed(factory)

    first = await _run(factory, _input(run_id, task_id, key="k-0"))
    second = await _run(factory, _input(run_id, task_id, key="k-1"))

    assert first.agent_run_id != second.agent_run_id


async def test_a_repository_the_workspace_has_not_connected_is_refused(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    run_id, task_id = _seed(factory, connected=False)

    with pytest.raises(ApplicationError) as caught:
        await _run(factory, _input(run_id, task_id))

    assert caught.value.type == "RepoNotConnected"
    assert caught.value.non_retryable is True
    with factory() as s:
        assert s.scalars(select(AgentRun)).all() == []


async def test_a_supervised_task_is_refused_rather_than_run_as_one_agent(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    run_id, task_id = _seed(factory)
    with factory() as s:
        task = s.get(Task, task_id)
        assert task is not None
        task.execution_mode = ExecutionMode.SUPERVISED_MULTI_AGENT
        s.commit()

    with pytest.raises(ApplicationError) as caught:
        await _run(factory, _input(run_id, task_id))

    assert caught.value.type == "ExecutionModeNotSupported"
    assert caught.value.non_retryable is True
    with factory() as s:
        assert s.scalars(select(AgentRun)).all() == []


async def test_a_run_outside_the_workspace_is_refused(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    _, task_id = _seed(factory)

    with pytest.raises(ApplicationError) as caught:
        await _run(factory, _input(uuid.uuid4(), task_id))

    assert caught.value.type == "WorkflowRunNotFound"


async def test_resume_agent_still_refuses(factory: sessionmaker[Session], mirror: Path) -> None:
    """Criterion 6: nothing on the acting path produces ``awaiting_input`` yet."""
    run_id, task_id = _seed(factory)
    inp = ResumeAgentInput(run_id, task_id, WS, None, attempt=0, idempotency_key="k")

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(build_activities(factory).resume_agent, inp)

    assert caught.value.type == "ActivityNotConfigured"


# -- criterion 5: forge.pause_and_notify ------------------------------------ #


def _notify(run_id: uuid.UUID, key: str = "n-0") -> NotifyInput:
    return NotifyInput(
        workflow_run_id=run_id,
        workspace_id=WS,
        reason="retry budget exhausted",
        kind="pause_and_notify",
        idempotency_key=key,
    )


def _state_events(factory: sessionmaker[Session]) -> list[AgentRunEvent]:
    with factory() as s:
        return list(
            s.scalars(select(AgentRunEvent).where(AgentRunEvent.type == "thread.state")).all()
        )


async def test_a_pause_is_recorded_once_on_the_runs_latest_agent_run(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    run_id, task_id = _seed(factory)
    earlier = await _run(factory, _input(run_id, task_id, key="k-0"))
    agent = await _run(factory, _input(run_id, task_id, key="k-1"))
    with factory() as s:  # SQLite's clock is too coarse to order two quick runs
        row = s.get(AgentRun, earlier.agent_run_id)
        assert row is not None
        row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        s.commit()
    notify = build_activities(factory).pause_and_notify

    assert await ActivityEnvironment().run(notify, _notify(run_id)) is True
    assert await ActivityEnvironment().run(notify, _notify(run_id)) is True  # a retry

    (event,) = _state_events(factory)
    assert event.run_id == agent.agent_run_id
    assert event.enforcement == "gated"
    assert event.summary == "retry budget exhausted"
    assert event.payload["state"] == "awaiting_input"
    assert event.payload["kind"] == "pause_and_notify"


async def test_a_pause_before_any_agent_run_reports_no_effect(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    run_id, _ = _seed(factory)

    sent = await ActivityEnvironment().run(
        build_activities(factory).pause_and_notify, _notify(run_id)
    )

    assert sent is False
    assert _state_events(factory) == []


async def test_a_pause_for_a_run_outside_the_workspace_is_refused(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    _seed(factory)

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(
            build_activities(factory).pause_and_notify, _notify(uuid.uuid4())
        )

    assert caught.value.type == "WorkflowRunNotFound"
