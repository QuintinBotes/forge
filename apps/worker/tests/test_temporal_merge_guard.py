"""``forge.load_guard_inputs`` for the ``merge`` phase reads CI and the spec itself (#280).

SCF, not a caller, decides whether the pull request's CI passed (spec FR-011): the
check runs on the agent's head commit, read with a token for the one repository.
``spec_validated`` is the spec engine's rollup for the task's spec, or true for a
run with no spec; the run records which, and why CI was or was not green.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.testing import ActivityEnvironment

import scf_worker.temporal_services as services
from scf_db.base import Base
from scf_db.models import AgentRun, Project, Task, WorkflowRun, Workspace
from scf_db.models.enums import RunStatus, SpecStatus
from scf_db.models.planning import SpecDocument
from scf_db.models.traceability import TraceabilitySpecRollup
from scf_worker.temporal_main import build_activities
from scf_workflow.temporal.payloads import GuardInputs, GuardInputsRequest

WS = uuid.UUID("00000000-0000-0000-0000-000000000280")
REPO = "acme/app"
HEAD = "abc1230000000000000000000000000000000000"


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    monkeypatch.delenv("FORGE_ALLOW_FAKE_ACTIVITIES", raising=False)
    for name in ("FORGE_GITHUB_APP_ID", "FORGE_GITHUB_INSTALLATION_ID"):
        monkeypatch.delenv(name, raising=False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _seed(factory: sessionmaker[Session], *, spec: str | None = None) -> uuid.UUID:
    """A run whose agent committed ``HEAD``; ``spec`` is None, "no_rollup" or the
    spec rollup's ``validation_status`` (scf_spec ValidationStatus)."""
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-280",
            title="Widgets",
            repo_targets=[{"repo": REPO, "base_branch": "main"}],
        )
        s.add(task)
        s.flush()
        if spec is not None:
            document = SpecDocument(
                workspace_id=WS,
                project_id=project.id,
                spec_key="SPEC-1",
                name="Widgets",
                status=SpecStatus.APPROVED,
            )
            s.add(document)
            s.flush()
            task.spec_id = document.id
            if spec != "no_rollup":
                s.add(
                    TraceabilitySpecRollup(
                        workspace_id=WS,
                        spec_id=document.id,
                        project_id=project.id,
                        spec_key="SPEC-1",
                        spec_name="Widgets",
                        spec_status="approved",
                        validation_status=spec,
                    )
                )
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.flush()
        s.add(
            AgentRun(
                workspace_id=WS,
                workflow_run_id=run.id,
                task_id=task.id,
                role="primary",
                inputs={},
                status=RunStatus.SUCCEEDED,
                output={"artifacts": {"branch": "scf/run", "head_commit": HEAD}},
            )
        )
        s.commit()
        return run.id


def _reads(monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]) -> list[Any]:
    calls: list[Any] = []

    def check_runs(repo: str, ref: str, **_: Any) -> list[dict[str, Any]]:
        calls.append((repo, ref))
        return runs

    monkeypatch.setattr(services, "github_check_runs", check_runs)
    return calls


async def _merge(factory: sessionmaker[Session], run_id: uuid.UUID) -> GuardInputs:
    req = GuardInputsRequest(workflow_run_id=run_id, workspace_id=WS, phase="merge")
    result = await ActivityEnvironment().run(build_activities(factory).load_guard_inputs, req)
    assert isinstance(result, GuardInputs)
    return result


def _recorded(factory: sessionmaker[Session], run_id: uuid.UUID) -> dict[str, Any]:
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        return dict(run.context["merge_guard"])


def _done(name: str, conclusion: str) -> dict[str, Any]:
    return {"name": name, "status": "completed", "conclusion": conclusion}


@pytest.mark.parametrize(
    ("runs", "green", "reason"),
    [
        ([], False, "ci_no_check_runs"),
        ([{"name": "tests", "status": "in_progress"}], False, "ci_pending: tests"),
        ([_done("tests", "success"), _done("lint", "failure")], False, "ci_failed: lint"),
        ([_done("tests", "success"), _done("lint", "success")], True, "ci_green"),
        ([_done("tests", "success"), _done("lint", "cancelled")], False, "ci_failed: lint"),
        ([_done("tests", "timed_out")], False, "ci_failed: tests"),
        ([_done("tests", "action_required")], False, "ci_failed: tests"),
        ([{"name": "tests", "status": "completed"}], False, "ci_failed: tests"),
        ([_done("tests", "neutral"), _done("docs", "skipped")], True, "ci_green"),
    ],
    ids=[
        "none",
        "pending",
        "failed",
        "green",
        "cancelled",
        "timed-out",
        "action-required",
        "no-conclusion",
        "neutral-and-skipped",
    ],
)
async def test_ci_is_green_only_when_every_check_run_completed_and_passed(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    runs: list[dict[str, Any]],
    green: bool,
    reason: str,
) -> None:
    """Criterion 1: zero runs, a pending run or a failed run is not green."""
    run_id = _seed(factory)
    calls = _reads(monkeypatch, runs)

    result = await _merge(factory, run_id)

    assert result.ci_status_green is green
    assert calls == [(REPO, HEAD)], "the agent's head commit, on the task's repository"
    recorded = _recorded(factory, run_id)
    assert (recorded["ci_status_green"], recorded["ci_reason"]) == (green, reason)
    assert recorded["head_commit"] == HEAD


async def test_without_github_app_credentials_ci_reads_as_not_green(
    factory: sessionmaker[Session],
) -> None:
    run_id = _seed(factory)

    result = await _merge(factory, run_id)

    assert result.ci_status_green is False
    assert _recorded(factory, run_id)["ci_reason"].startswith("ci_unreadable:")


@pytest.mark.parametrize(
    ("spec", "validated", "source"),
    [
        (None, True, "no_spec"),
        ("passing", True, "spec_rollup"),
        ("partial", False, "spec_rollup"),
        ("failing", False, "spec_rollup"),
        ("stale", False, "spec_rollup"),
        ("none", False, "spec_rollup"),
        ("no_rollup", False, "spec_rollup"),
    ],
)
async def test_spec_validated_is_the_spec_engines_rollup_or_true_with_no_spec(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    spec: str | None,
    validated: bool,
    source: str,
) -> None:
    """Criterion 3: the recorded guard result says which it was."""
    run_id = _seed(factory, spec=spec)
    _reads(monkeypatch, [_done("tests", "success")])

    result = await _merge(factory, run_id)

    assert result.spec_validated is validated
    recorded = _recorded(factory, run_id)
    assert (recorded["spec_validated"], recorded["spec_source"]) == (validated, source)
