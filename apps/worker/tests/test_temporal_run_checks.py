"""``forge.run_checks`` runs the declared checks on the agent's commit (#251).

Through ``build_activities`` with ``FORGE_ALLOW_FAKE_ACTIVITIES`` unset, on a local
bare mirror and the worktree sandbox (tests only). The agent run is seeded as
``forge.run_agent`` records it, pointing at a commit on the run branch.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scf_db.base import Base
from scf_db.models import AgentRun, Project, RepositoryConnection, Task, WorkflowRun, Workspace
from scf_db.models.enums import RunStatus
from scf_worker.temporal_main import build_activities
from scf_workflow.temporal.payloads import ChecksResult, RunChecksInput

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a7")
REPO = "acme/app"
BRANCH = "scf/run-0123456789ab"
POLICY = 'repo_id: app\ncommands:\n  marker: "test -f MARKER"\n  lint: "true"\n'


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
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
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """``(work clone, bare mirror)``: main declares the policy; the run branch adds MARKER."""
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    for name in ("FORGE_ALLOW_FAKE_ACTIVITIES", "FORGE_GITHUB_APP_ID"):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    (work / ".forge").mkdir(parents=True)
    (work / ".forge" / "policy.yaml").write_text(POLICY)
    (work / "README.md").write_text("app\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", BRANCH)
    (work / "MARKER").write_text("made by the agent\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "agent change")
    _git(work, "checkout", "-q", "main")
    mirror = root / str(WS) / "acme" / "app"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(mirror))
    return work, mirror


def _seed(factory: sessionmaker[Session], head: str | None) -> uuid.UUID:
    """A workflow run whose agent run (if ``head``) recorded ``head`` on the run branch."""
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(RepositoryConnection(workspace_id=WS, repo_id=REPO, name=REPO))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-9",
            title="checked",
            repo_targets=[{"repo": REPO, "base_branch": "main"}],
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.flush()
        if head is not None:
            s.add(
                AgentRun(
                    workspace_id=WS,
                    workflow_run_id=run.id,
                    task_id=task.id,
                    status=RunStatus.SUCCEEDED,
                    output={
                        "status": "succeeded",
                        "artifacts": {"branch": BRANCH, "head_commit": head},
                    },
                )
            )
        s.commit()
        return run.id


async def _checks(factory: sessionmaker[Session], run_id: uuid.UUID) -> ChecksResult:
    inp = RunChecksInput(workflow_run_id=run_id, workspace_id=WS, attempt=0, idempotency_key="c0")
    result = await ActivityEnvironment().run(build_activities(factory).run_checks, inp)
    assert isinstance(result, ChecksResult)
    return result


def _worktrees(mirror: Path) -> int:
    return len(_git(mirror, "worktree", "list").splitlines())


async def test_each_declared_command_runs_on_the_agents_commit(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    _, mirror = repo
    run_id = _seed(factory, _git(mirror, "rev-parse", BRANCH))

    result = await _checks(factory, run_id)

    assert result.results == {"marker": True, "lint": True}
    assert _worktrees(mirror) == 1, "the checks' worktree is removed"


async def test_a_failing_command_fails_only_its_own_check(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    _, mirror = repo
    run_id = _seed(factory, _git(mirror, "rev-parse", "main"))  # MARKER is not on main

    result = await _checks(factory, run_id)

    assert result.results == {"marker": False, "lint": True}
    assert not result.all_passed


async def test_a_retry_checks_the_same_commit_after_the_branch_moved(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    head = _git(mirror, "rev-parse", BRANCH)
    run_id = _seed(factory, head)
    first = await _checks(factory, run_id)
    # The run branch moves on (MARKER removed) after the agent recorded its head.
    _git(work, "checkout", "-q", BRANCH)
    _git(work, "rm", "-q", "MARKER")
    _git(work, "commit", "-q", "-m", "later")
    _git(work, "push", "-q", str(mirror), BRANCH)

    again = await _checks(factory, run_id)

    assert first.results == again.results == {"marker": True, "lint": True}
    assert _worktrees(mirror) == 1


async def test_a_repository_that_declares_no_command_fails(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    (work / ".forge" / "policy.yaml").write_text("repo_id: app\n")
    _git(work, "commit", "-q", "-am", "no commands")
    _git(work, "push", "-q", str(mirror), "main")
    run_id = _seed(factory, _git(mirror, "rev-parse", BRANCH))

    result = await _checks(factory, run_id)

    assert result.results == {"declared_checks": False}
    assert not result.all_passed


async def test_the_agent_cannot_change_its_own_checks(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    """The commands come from the default branch, not the run branch the agent wrote."""
    work, mirror = repo
    _git(work, "checkout", "-q", BRANCH)
    (work / ".forge" / "policy.yaml").write_text('repo_id: app\ncommands:\n  marker: "true"\n')
    _git(work, "commit", "-q", "-am", "agent loosens its checks")
    _git(work, "push", "-q", str(mirror), BRANCH)
    run_id = _seed(factory, _git(mirror, "rev-parse", "main"))

    result = await _checks(factory, run_id)

    assert result.results == {"marker": False, "lint": True}


async def test_without_an_agent_run_the_checks_refuse(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    run_id = _seed(factory, None)

    with pytest.raises(ApplicationError) as caught:
        await _checks(factory, run_id)

    assert caught.value.type == "AgentRunNotFound"
    assert caught.value.non_retryable is True


async def test_a_run_outside_the_workspace_is_refused(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    _seed(factory, None)

    with pytest.raises(ApplicationError) as caught:
        await _checks(factory, uuid.uuid4())

    assert caught.value.type == "WorkflowRunNotFound"
