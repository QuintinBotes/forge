"""Runs reach only their own workspace's mirrors, and their outcome is recorded."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forge_agent.testing import ScriptedModelClient, finish_response
from forge_contracts import AgentObjective, AgentRunResult, RunStatus
from forge_db.base import Base
from forge_db.models import AgentRun, Workspace
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_worker.agent_runner import _record_run_outcome
from forge_worker.dispatch import RepositoryUnavailable, resolve_run_repository
from forge_worker.finalizer import GitHubAppCredentials
from forge_worker.multi_agent import _SubAgentRuntime

WS = uuid.UUID("00000000-0000-0000-0000-00000000aa01")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _objective(**context: object) -> AgentObjective:
    return AgentObjective(objective="x", context={"workspace_id": str(WS), **context})


@pytest.fixture
def mirrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    return root


def test_no_repository_leaves_the_objective_alone(mirrors: Path) -> None:
    objective = _objective()
    assert resolve_run_repository(objective, credentials=None) is objective


def test_existing_mirror_resolves_inside_the_workspace_namespace(mirrors: Path) -> None:
    mirror = mirrors / str(WS) / "acme" / "widgets"
    mirror.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
    resolved = resolve_run_repository(_objective(repo_full_name="acme/widgets"), credentials=None)
    assert resolved.context["repo_path"] == str(mirror)


def test_another_workspaces_mirror_is_never_reached(mirrors: Path) -> None:
    other = mirrors / str(uuid.uuid4()) / "acme" / "widgets"
    other.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    with pytest.raises(RepositoryUnavailable, match="no mirror"):
        resolve_run_repository(_objective(repo_full_name="acme/widgets"), credentials=None)


@pytest.mark.parametrize("name", ["../x", "acme/..", "/etc/passwd", "acme/widgets/extra", "acme"])
def test_path_like_names_are_refused(mirrors: Path, name: str) -> None:
    with pytest.raises(RepositoryUnavailable):
        resolve_run_repository(_objective(repo_full_name=name), credentials=None)


def test_with_credentials_the_mirror_is_fetched_with_a_read_token(
    mirrors: Path, tmp_path: Path
) -> None:
    remote_root = tmp_path / "remote"
    upstream = remote_root / "acme" / "widgets.git"
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "one")
    upstream.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(upstream)], check=True)
    creds = GitHubAppCredentials(
        app_id="1", installation_id="2", private_key_pem="unused", git_base_url=str(remote_root)
    )
    minted: list[str] = []

    def minter(_: GitHubAppCredentials):  # type: ignore[no-untyped-def]
        return lambda name: minted.append(name) or "ghs_token"

    resolved = resolve_run_repository(
        _objective(repo_full_name="acme/widgets"), credentials=creds, minter=minter
    )
    path = Path(resolved.context["repo_path"])
    assert path == mirrors / str(WS) / "acme" / "widgets"
    assert _git(path, "rev-parse", "main") == _git(src, "rev-parse", "main")
    assert minted == ["widgets"]


def test_subagents_never_use_a_repo_target_as_a_path(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    _git(victim, "init", "-q", "-b", "main")
    (victim / "secret.txt").write_text("s\n")
    _git(victim, "add", "-A")
    _git(victim, "commit", "-q", "-m", "s")
    model = ScriptedModelClient(responses=[finish_response("done", confidence=0.9)])
    runtime = _SubAgentRuntime(model, WS, repo_root=None)
    objective = AgentObjective(
        objective="read the secret",
        repo_targets=[{"repo": str(victim)}],
        allowed_actions=["read_repo", "write_code"],
        context={"role": "implementer", "workspace_id": str(WS)},
    )
    runtime.run(objective)
    offered = {t["name"] for request in model.requests for t in request.tools}
    assert offered.isdisjoint({"read_file", "write_file", "list_dir", "search_code", "git_commit"})
    assert _git(victim, "worktree", "list").count("\n") == 0, "no worktree was added to it"


@pytest.fixture
def session_factory():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Workspace(id=WS, name="w", slug="w"))
        s.commit()
    yield lambda: Session(engine)
    engine.dispose()


def test_outcome_is_written_to_the_run_row(session_factory) -> None:  # type: ignore[no-untyped-def]
    run_id = uuid.uuid4()
    with session_factory() as s:
        s.add(AgentRun(id=run_id, workspace_id=WS, status=DbRunStatus.PENDING))
        s.commit()
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED, confidence=0.9, output="ok")
    _record_run_outcome(run_id, WS, result, session_factory=session_factory)
    with session_factory() as s:
        row = s.get(AgentRun, run_id)
        assert row is not None
        assert row.status is DbRunStatus.SUCCEEDED
        assert row.confidence == 0.9 and row.completed_at is not None
        assert row.output["output"] == "ok"


def test_outcome_for_a_foreign_workspace_is_ignored(session_factory) -> None:  # type: ignore[no-untyped-def]
    run_id = uuid.uuid4()
    with session_factory() as s:
        s.add(AgentRun(id=run_id, workspace_id=WS, status=DbRunStatus.PENDING))
        s.commit()
    _record_run_outcome(
        run_id,
        uuid.uuid4(),
        AgentRunResult(status=RunStatus.SUCCEEDED),
        session_factory=session_factory,
    )
    with session_factory() as s:
        row = s.get(AgentRun, run_id)
        assert row is not None and row.status is DbRunStatus.PENDING
