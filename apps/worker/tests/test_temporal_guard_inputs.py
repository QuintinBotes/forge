"""``forge.load_guard_inputs`` reads the execute preconditions from stored data (#250).

Owner decisions (2026-11-15): a repository target on the task is always required;
policy counts as loaded when the default branch's policy file parses, or when there
is none and the default policy applies; a skill profile and a knowledge sync are
required only when the workspace has configured them, and the run records which of
the two were not in use. Every test goes through ``build_activities`` with
``FORGE_ALLOW_FAKE_ACTIVITIES`` unset.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scf_db.base import Base
from scf_db.models import (
    KnowledgeSource,
    Project,
    SkillProfile,
    Task,
    WorkflowRun,
    Workspace,
)
from scf_db.models.enums import KnowledgeSourceKind
from scf_worker.temporal_main import build_activities
from scf_workflow.temporal.payloads import GuardInputs, GuardInputsRequest

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a5")
REPO = "acme/app"
TARGETS = [{"repo": REPO, "base_branch": "main"}]
VALID_POLICY = "repo_id: app\nreview_rules:\n  min_approvals: 1\n"
ALL_MET = {
    "repo_target_set": True,
    "policy_loaded": True,
    "skill_profile_set": True,
    "knowledge_synced": True,
}


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


@pytest.fixture
def mirrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    for name in ("FORGE_GITHUB_APP_ID", "FORGE_GITHUB_INSTALLATION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FORGE_ALLOW_FAKE_ACTIVITIES", raising=False)
    return root


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _mirror(mirrors: Path, tmp_path: Path, policy: str | None) -> Path:
    """A bare mirror of ``REPO`` whose default branch carries ``policy``, if any."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("app\n")
    if policy is not None:
        (work / ".forge").mkdir()
        (work / ".forge" / "policy.yaml").write_text(policy)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    mirror = mirrors / str(WS) / "acme" / "app"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(mirror))
    return mirror


def _seed(
    factory: sessionmaker[Session],
    *,
    repo_targets: list[dict[str, str]] | None = None,
    skill_profile: str | None = None,
) -> uuid.UUID:
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-1",
            title="guarded",
            repo_targets=TARGETS if repo_targets is None else repo_targets,
            skill_profile=skill_profile,
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.commit()
        return run.id


async def _load(
    factory: sessionmaker[Session], run_id: uuid.UUID, phase: str = "execute"
) -> GuardInputs:
    activities = build_activities(factory)
    request = GuardInputsRequest(workflow_run_id=run_id, workspace_id=WS, phase=phase)
    result = await ActivityEnvironment().run(activities.load_guard_inputs, request)
    assert isinstance(result, GuardInputs)
    return result


def _not_in_use(factory: sessionmaker[Session], run_id: uuid.UUID) -> object:
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        return run.context.get("preconditions_not_in_use")


async def test_a_parsing_policy_and_nothing_optional_configured_meets_every_precondition(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path
) -> None:
    _mirror(mirrors, tmp_path, VALID_POLICY)
    run_id = _seed(factory)

    guard = await _load(factory, run_id)

    assert guard.preconditions == ALL_MET
    assert _not_in_use(factory, run_id) == ["skill_profile_set", "knowledge_synced"]


async def test_without_a_policy_file_the_default_policy_counts_as_loaded(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path
) -> None:
    _mirror(mirrors, tmp_path, None)
    run_id = _seed(factory)

    assert (await _load(factory, run_id)).preconditions["policy_loaded"] is True


async def test_a_policy_file_that_does_not_parse_is_not_loaded(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path
) -> None:
    _mirror(mirrors, tmp_path, "review_rules: [unclosed\n")
    run_id = _seed(factory)

    guard = await _load(factory, run_id)

    assert guard.preconditions == ALL_MET | {"policy_loaded": False}


async def test_a_task_without_a_repository_target_meets_neither_repo_precondition(
    factory: sessionmaker[Session], mirrors: Path
) -> None:
    run_id = _seed(factory, repo_targets=[])

    guard = await _load(factory, run_id)

    assert guard.preconditions == ALL_MET | {"repo_target_set": False, "policy_loaded": False}


async def test_a_repository_with_no_mirror_and_no_credentials_has_no_readable_policy(
    factory: sessionmaker[Session], mirrors: Path
) -> None:
    run_id = _seed(factory)

    guard = await _load(factory, run_id)

    assert guard.preconditions == ALL_MET | {"policy_loaded": False}


async def test_a_configured_skill_profile_must_be_set_on_the_task(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path
) -> None:
    _mirror(mirrors, tmp_path, VALID_POLICY)
    run_id = _seed(factory)
    with factory() as s:
        s.add(SkillProfile(workspace_id=WS, name="backend-tdd"))
        s.commit()

    assert (await _load(factory, run_id)).preconditions["skill_profile_set"] is False
    assert _not_in_use(factory, run_id) == ["knowledge_synced"]

    with factory() as s:
        task = s.get(Task, s.get(WorkflowRun, run_id).task_id)  # type: ignore[union-attr]
        assert task is not None
        task.skill_profile = "backend-tdd"
        s.commit()

    assert (await _load(factory, run_id)).preconditions["skill_profile_set"] is True


async def test_configured_knowledge_must_have_synced(
    factory: sessionmaker[Session], mirrors: Path, tmp_path: Path
) -> None:
    _mirror(mirrors, tmp_path, VALID_POLICY)
    run_id = _seed(factory)
    with factory() as s:
        source = KnowledgeSource(
            workspace_id=WS, kind=KnowledgeSourceKind.REPO, name="app", uri=REPO
        )
        s.add(source)
        s.commit()
        source_id = source.id

    assert (await _load(factory, run_id)).preconditions["knowledge_synced"] is False
    assert _not_in_use(factory, run_id) == ["skill_profile_set"]

    with factory() as s:
        row = s.get(KnowledgeSource, source_id)
        assert row is not None
        row.last_synced_at = datetime.now(UTC)
        s.commit()

    assert (await _load(factory, run_id)).preconditions["knowledge_synced"] is True


async def test_the_plan_phase_requires_the_plan_gate(
    factory: sessionmaker[Session], mirrors: Path
) -> None:
    run_id = _seed(factory)

    assert (await _load(factory, run_id, phase="plan")).plan_required is True


async def test_a_run_outside_the_workspace_is_refused(
    factory: sessionmaker[Session], mirrors: Path
) -> None:
    _seed(factory)

    with pytest.raises(ApplicationError) as caught:
        await _load(factory, uuid.uuid4())

    assert caught.value.type == "WorkflowRunNotFound"
    assert caught.value.non_retryable is True
