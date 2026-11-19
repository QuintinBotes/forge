"""#187 criteria 6 and 7: the gates on the diff join ``forge.run_checks``'s check set.

On a local bare mirror and the worktree sandbox (tests only), as #251's tests run
the checks; bandit is a dev dependency. The credential below is fake and split, so
no source line holds one.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from scf_agent.testing import ScriptedModelClient, finish_response
from scf_contracts.orchestration_config import AgentRole, Effort
from scf_db.base import Base
from scf_db.models import AgentRun, Project, RepositoryConnection, Task, WorkflowRun, Workspace
from scf_db.models.enums import RunStatus
from scf_db.models.role_config import AgentRoleConfig
from scf_worker.temporal_services import run_checks_service
from scf_workflow.temporal.payloads import ChecksResult, RunChecksInput

WS = uuid.UUID("00000000-0000-0000-0000-0000000000b7")
REPO = "acme/app"
BRANCH = "scf/run-0123456789cd"
POLICY = 'repo_id: app\ncommands:\n  lint: "true"\n'
FAKE_KEY = "AKIA" + "QZ7X" * 4
#: A shape the stored output's own redaction does not catch: only the gate masks it.
FAKE_GOOGLE_KEY = "AIza" + "Sy" + "B7" * 16 + "x"
SHELL = (
    "import subprocess\n\n\ndef run(command):\n    return subprocess.Popen(command, shell=True)\n"
)


class Reviewer:
    """Records each review it is asked for, and approves."""

    def __init__(self) -> None:
        self.models: list[str] = []

    def __call__(self, model: str) -> ScriptedModelClient:
        self.models.append(model)
        reply = '{"verdict": "approve", "findings": []}'
        return ScriptedModelClient([finish_response(reply, confidence=0.9)])


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
    """``(work clone, bare mirror)``: main declares one check and holds the policy."""
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    work = tmp_path / "work"
    (work / ".forge").mkdir(parents=True)
    (work / ".forge" / "policy.yaml").write_text(POLICY)
    (work / "README.md").write_text("app\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    mirror = root / str(WS) / "acme" / "app"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(mirror))
    return work, mirror


def _commit(
    work: Path, mirror: Path, branch: str, files: dict[str, str], message: str = "change"
) -> str:
    _git(work, "checkout", "-q", branch)
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    _git(work, "push", "-q", str(mirror), branch)
    _git(work, "checkout", "-q", "main")
    return _git(mirror, "rev-parse", branch)


def _agent_commits(work: Path, mirror: Path, files: dict[str, str]) -> str:
    _git(work, "branch", BRANCH, "main")
    return _commit(work, mirror, BRANCH, files)


def _seed(factory: sessionmaker[Session], head: str) -> uuid.UUID:
    """A run whose builder recorded ``head`` and whose workspace names a reviewer."""
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
            key="CORE-7",
            title="gated",
            repo_targets=[{"repo": REPO, "base_branch": "main"}],
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.flush()
        s.add(
            AgentRun(
                workspace_id=WS,
                workflow_run_id=run.id,
                task_id=task.id,
                model="claude-opus-5-5",
                status=RunStatus.SUCCEEDED,
                output={"artifacts": {"branch": BRANCH, "head_commit": head}},
            )
        )
        for role, model in ((AgentRole.REVIEWER, "gpt-6-astra"), (AgentRole.ADVERSARY, "off")):
            s.add(
                AgentRoleConfig(workspace_id=WS, role=role, model_or_tier=model, effort=Effort.HIGH)
            )
        s.commit()
        return run.id


def _checks(factory: sessionmaker[Session], run_id: uuid.UUID, reviewer: Reviewer) -> ChecksResult:
    service = run_checks_service(factory, reviewer=reviewer)
    return service(
        RunChecksInput(workflow_run_id=run_id, workspace_id=WS, attempt=0, idempotency_key="c0")
    )


def _recorded(factory: sessionmaker[Session], run_id: uuid.UUID) -> dict[str, Any]:
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        [attempt] = run.context["checks"]
        return {c["id"]: c for c in attempt["checks"]}


@pytest.mark.parametrize(
    ("files", "failed", "finding"),
    [
        ({"app/settings.py": f'KEY = "{FAKE_KEY}"\n'}, "secret_scan", "app/settings.py:1 [aws"),
        ({"app/run.py": SHELL}, "sast", "app/run.py:5 [B602]"),
    ],
)
def test_a_change_that_fails_a_gate_is_not_offered_for_review(
    factory: sessionmaker[Session],
    repo: tuple[Path, Path],
    files: dict[str, str],
    failed: str,
    finding: str,
) -> None:
    work, mirror = repo
    run_id = _seed(factory, _agent_commits(work, mirror, files))
    reviewer = Reviewer()

    result = _checks(factory, run_id, reviewer)

    gates = {"secret_scan": True, "sast": True}
    assert result.results == {"lint": True, **gates, failed: False}
    assert not result.all_passed and result.pause_reason is None
    assert reviewer.models == [], "a change that fails a gate is never reviewed"
    stored = _recorded(factory, run_id)[failed]
    assert stored["passed"] is False and finding in stored["output_tail"]


def test_the_stored_result_never_holds_the_secret(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    run_id = _seed(factory, _agent_commits(work, mirror, {"app/k.py": f'K = "{FAKE_KEY}"\n'}))

    _checks(factory, run_id, Reviewer())

    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        assert FAKE_KEY not in json.dumps(run.context)


def test_a_worker_without_bandit_pauses_for_a_person(
    factory: sessionmaker[Session], repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, mirror = repo
    run_id = _seed(factory, _agent_commits(work, mirror, {"app/run.py": "VALUE = 1\n"}))
    paths = os.environ["PATH"].split(os.pathsep)
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in paths if not Path(p, "bandit").exists()))

    result = _checks(factory, run_id, Reviewer())

    assert result.results["sast"] is False
    assert result.pause_reason == "sast could not verify: bandit is not installed on this worker"


def test_a_secret_added_then_removed_in_a_later_commit_is_found(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    first = _agent_commits(work, mirror, {"app/k.py": f'K = "{FAKE_KEY}"\n'})
    run_id = _seed(factory, _commit(work, mirror, BRANCH, {"app/k.py": "K = None\n"}))

    result = _checks(factory, run_id, Reviewer())

    assert result.results["secret_scan"] is False
    stored = _recorded(factory, run_id)["secret_scan"]["output_tail"]
    assert f"app/k.py:1 [aws-access-token] in commit {first[:12]}" in stored


def test_a_secret_in_a_commit_message_is_found(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    _git(work, "branch", BRANCH, "main")
    head = _commit(work, mirror, BRANCH, {"a.txt": "a\n"}, message=f"fix\n\nkey {FAKE_KEY}")
    run_id = _seed(factory, head)

    result = _checks(factory, run_id, Reviewer())

    assert result.results["secret_scan"] is False
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None and FAKE_KEY not in json.dumps(run.context)
    assert f"commit message:3 [aws-access-token] in commit {head[:12]}" in json.dumps(run.context)


def test_a_python_file_whose_name_holds_a_space_is_analysed(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    run_id = _seed(factory, _agent_commits(work, mirror, {"deploy tool.py": SHELL}))

    result = _checks(factory, run_id, Reviewer())

    assert result.results["sast"] is False


def test_a_python_path_that_is_a_symlink_cannot_be_verified(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    """``import tool`` runs tool.dat; git's blob for tool.py is only the link's text."""
    work, mirror = repo
    _git(work, "branch", BRANCH, "main")
    (work / "tool.py").symlink_to("tool.dat")
    run_id = _seed(factory, _commit(work, mirror, BRANCH, {"tool.dat": SHELL}))

    result = _checks(factory, run_id, Reviewer())

    assert result.results["sast"] is False
    assert result.pause_reason is None, "the change's to fix, not the worker's"
    stored = _recorded(factory, run_id)["sast"]["output_tail"]
    assert stored.startswith("could_not_verify") and "tool.py: not read as a regular file" in stored


def test_a_credential_in_a_file_path_is_found_and_never_stored(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    head = _agent_commits(work, mirror, {f"keys/{FAKE_GOOGLE_KEY}.py": SHELL})
    run_id = _seed(factory, head)

    result = _checks(factory, run_id, Reviewer())

    assert result.results["secret_scan"] is False and result.results["sast"] is False
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None and FAKE_GOOGLE_KEY not in json.dumps(run.context)
    stored = _recorded(factory, run_id)
    assert (
        f"file path:1 [google-api-key] in commit {head[:12]}"
        in stored["secret_scan"]["output_tail"]
    )
    assert "[B602]" in stored["sast"]["output_tail"]


def test_a_change_that_passes_every_gate_goes_on_to_review(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    run_id = _seed(factory, _agent_commits(work, mirror, {"app/ok.py": "VALUE = 1\n"}))
    reviewer = Reviewer()

    result = _checks(factory, run_id, reviewer)

    assert result.results == {
        "lint": True,
        "secret_scan": True,
        "sast": True,
        "independent_review": True,
    }
    assert reviewer.models == ["gpt-6-astra"]


def test_a_secret_the_default_branch_allows_is_recorded_as_allowed(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    allowing = POLICY + "diff_gates:\n  secret_allowed_files: [tests/fixture.py]\n"
    _commit(work, mirror, "main", {".forge/policy.yaml": allowing})
    run_id = _seed(
        factory, _agent_commits(work, mirror, {"tests/fixture.py": f'K = "{FAKE_KEY}"\n'})
    )

    result = _checks(factory, run_id, Reviewer())

    assert result.results["secret_scan"] is True
    stored = _recorded(factory, run_id)["secret_scan"]["output_tail"]
    assert "allowed by policy: tests/fixture.py:1 [aws-access-token]" in stored


def test_a_change_cannot_allow_its_own_secret(
    factory: sessionmaker[Session], repo: tuple[Path, Path]
) -> None:
    work, mirror = repo
    allowing = POLICY + "diff_gates:\n  secret_allowed_files: [tests/fixture.py]\n"
    files = {".forge/policy.yaml": allowing, "tests/fixture.py": f'K = "{FAKE_KEY}"\n'}
    run_id = _seed(factory, _agent_commits(work, mirror, files))

    result = _checks(factory, run_id, Reviewer())

    assert result.results["secret_scan"] is False
