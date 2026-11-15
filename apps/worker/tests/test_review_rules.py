"""PR gates carry the repo's review rules from the default branch (spec K §4.11)."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from forge_approval.models import ApprovalRequest, GateType
from forge_db.base import Base
from forge_db.models import Project, Task, WorkflowRun, Workspace
from forge_worker import review_rules
from forge_worker.approval_activity import make_create_approval_fn
from forge_workflow.temporal.payloads import ApprovalInput

WS = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
REPO = "acme/app"


def _policy(min_approvals: int) -> str:
    return f"repo_id: app\nreview_rules:\n  min_approvals: {min_approvals}\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def mirrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    return root


def _mirror(mirrors: Path, tmp_path: Path, default_policy: str | bytes | None) -> Path:
    """A bare mirror whose default branch carries ``default_policy`` and whose
    run branch (an agent's) carries a laxer one, committed later."""
    work = tmp_path / "work"
    (work / ".forge").mkdir(parents=True)
    (work / "README.md").write_text("app\n")
    if isinstance(default_policy, bytes):
        (work / ".forge" / "policy.yaml").write_bytes(default_policy)
    elif default_policy is not None:
        (work / ".forge" / "policy.yaml").write_text(default_policy)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", f"forge/run-{uuid.uuid4().hex[:12]}")
    (work / ".forge" / "policy.yaml").write_text(_policy(1))
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "agent loosens its gate")
    _git(work, "checkout", "-q", "main")  # the remote's HEAD is its default branch
    mirror = mirrors / str(WS) / "acme" / "app"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(mirror))
    return mirror


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _seed_run(
    factory: sessionmaker[Session], repo_targets: list[dict[str, str]]
) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="TASK-1",
            title="gated change",
            repo_targets=repo_targets,
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id)
        s.add(run)
        s.commit()
        return task.id, run.id


async def _open(
    factory: sessionmaker[Session], gate: str, repo_targets: list[dict[str, str]] | None = None
) -> ApprovalRequest:
    task_id, run_id = _seed_run(factory, [{"repo": REPO}] if repo_targets is None else repo_targets)
    await make_create_approval_fn(factory)(
        ApprovalInput(
            workflow_run_id=run_id,
            workspace_id=WS,
            task_id=task_id,
            gate=gate,
            idempotency_key=f"{run_id}:approval:{gate}:1",
        )
    )
    [request] = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
    return request


async def test_pr_gate_takes_the_default_branch_rules_not_the_run_branch(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path
) -> None:
    _mirror(mirrors, tmp_path, _policy(3))

    request = await _open(factory, "pr")

    assert request.gate_payload["review_rules_source"] == "repo"
    assert request.gate_payload["review_rules"]["min_approvals"] == 3  # main, not the run branch
    assert request.required_approvals == 3  # a stricter repo quorum raises the gate's


async def test_a_repo_cannot_lower_the_default_quorum(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path
) -> None:
    _mirror(mirrors, tmp_path, _policy(0))

    request = await _open(factory, "pr")

    assert request.gate_payload["review_rules"]["min_approvals"] == 0
    assert request.required_approvals == 1


@pytest.mark.parametrize(
    "default_policy",
    [
        None,
        "review_rules: [not, a, mapping\n",
        "repo_id: app\nreview_rules:\n  min_approvals: lots\n",
        b"repo_id: app\nreview_rules:\n  min_approvals: 3\n# \xff\xfe\n",
    ],
    ids=["absent", "bad_yaml", "schema_invalid", "not_utf8"],
)
async def test_unreadable_policy_stamps_the_default_marker(
    factory: sessionmaker[Session],
    mirrors: Path,
    tmp_path: Path,
    default_policy: str | bytes | None,
) -> None:
    _mirror(mirrors, tmp_path, default_policy)

    request = await _open(factory, "pr")

    assert request.gate_payload["review_rules_source"] == "default"
    assert "review_rules" not in request.gate_payload
    assert request.required_approvals == 1


async def test_a_task_without_a_repository_stamps_the_default_marker(
    factory: sessionmaker[Session], mirrors: Path
) -> None:
    request = await _open(factory, "pr", repo_targets=[])

    assert request.gate_payload["review_rules_source"] == "default"


# -- anything but an absent or invalid policy fails closed: the activity retries #


async def test_a_missing_mirror_for_a_real_target_raises(
    factory: sessionmaker[Session], mirrors: Path
) -> None:
    with pytest.raises(FileNotFoundError, match="no mirror of acme/app"):
        await _open(factory, "pr")

    assert await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS) == []


async def test_a_git_timeout_raises(
    factory: sessionmaker[Session],
    mirrors: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mirror(mirrors, tmp_path, _policy(3))

    def _timeout(cmd: list[str], **_: object) -> None:
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(review_rules.subprocess, "run", _timeout)

    with pytest.raises(subprocess.TimeoutExpired):
        await _open(factory, "pr")


def test_a_database_error_raises() -> None:
    def _down() -> Session:
        raise OperationalError("SELECT task", {}, Exception("connection refused"))

    with pytest.raises(OperationalError):
        review_rules.review_rules_stamp(
            _down,  # type: ignore[arg-type]
            workspace_id=WS,
            task_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("gate", ["spec", "plan"])
async def test_only_pr_gates_are_stamped(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path, gate: str
) -> None:
    _mirror(mirrors, tmp_path, _policy(3))

    request = await _open(factory, gate)

    assert request.gate_type is GateType(gate)
    assert "review_rules" not in request.gate_payload
    assert "review_rules_source" not in request.gate_payload
    assert request.required_approvals == 1
