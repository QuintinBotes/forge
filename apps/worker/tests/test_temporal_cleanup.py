"""``forge.cleanup_worktree`` is bound in the worker (spec K §4.11).

Before, it was unbound, so cancelling a production feature run failed the run
with ``ActivityNotConfigured`` instead of reaching ``cancelled``.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import forge_worker.temporal_main as tm
from forge_agent.sandbox import WorktreeSandbox
from forge_contracts import RunStatus, WorkflowState
from forge_db.base import Base
from forge_db.models import AgentRun, Project, Task, Workspace
from forge_db.models import WorkflowRun as DbWorkflowRun
from forge_worker.run_workspace import run_branch
from forge_workflow.store import InMemoryWorkflowStore, SqlAlchemyWorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.engine import TemporalWorkflowEngine
from forge_workflow.temporal.payloads import CleanupInput
from forge_workflow.temporal.worker import build_temporal_worker
from forge_workflow.temporal.workflows import FeatureWorkflow

WS = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
TASK_QUEUE = "forge-k-p2-cleanup"


@pytest.fixture
async def time_skip_env() -> AsyncIterator[Any]:
    """As ``packages/workflow-engine/tests/temporal/conftest.py``."""
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except Exception as exc:  # pragma: no cover - offline sandbox
        pytest.skip(f"PARKED: Temporal test server unavailable: {exc}")
    try:
        yield env
    finally:
        await env.shutdown()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The workspace's bare mirror of ``acme/app``."""
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("app\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run([*git, *args], cwd=work, check=True, capture_output=True)
    path = root / str(WS) / "acme" / "app"
    path.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(path)], check=True, capture_output=True
    )
    return path


def _seed_task(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(workspace_id=WS, project_id=project.id, key="TASK-1", title="cancel me")
        s.add(task)
        s.commit()
        return task.id


def _leftover_agent_worktree(
    factory: sessionmaker[Session], mirror: Path, task_id: uuid.UUID, run_id: uuid.UUID
) -> Path:
    """An agent run of ``run_id`` whose worktree was never removed (a crash or cancel)."""
    with factory() as s:
        agent = AgentRun(workspace_id=WS, workflow_run_id=run_id, task_id=task_id)
        s.add(agent)
        s.commit()
        agent_id = agent.id
    return WorktreeSandbox(mirror).create(branch=run_branch("forge", agent_id))


async def test_cleanup_is_bound_without_fakes_and_idempotent(
    factory: sessionmaker[Session], mirror: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_ALLOW_FAKE_ACTIVITIES", raising=False)
    task_id = _seed_task(factory)
    run_id = uuid.uuid4()
    with factory() as s:
        s.add(DbWorkflowRun(id=run_id, workspace_id=WS, task_id=task_id))
        s.commit()
    worktree = _leftover_agent_worktree(factory, mirror, task_id, run_id)
    cleanup = tm.build_activities(factory).cleanup_worktree
    inp = CleanupInput(workflow_run_id=run_id, workspace_id=WS)

    assert await cleanup(inp) is True
    assert not worktree.exists()
    assert await cleanup(inp) is True  # nothing left: a no-op
    # A run with no agent runs and a workspace with no mirrors are no-ops too.
    other = CleanupInput(workflow_run_id=uuid.uuid4(), workspace_id=uuid.uuid4())
    assert await cleanup(other) is True


async def test_a_cancelled_feature_run_on_the_workers_activities_reaches_cancelled(
    time_skip_env: Any,
    factory: sessionmaker[Session],
    mirror: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fakes stand in only for the agent / red-team bodies; cleanup is the real one.
    monkeypatch.setenv("FORGE_ALLOW_FAKE_ACTIVITIES", "1")
    task_id = _seed_task(factory)
    activities = tm.build_activities(factory)

    with factory() as session:
        engine = TemporalWorkflowEngine(
            workspace_id=WS,
            store=SqlAlchemyWorkflowStore(session, workspace_id=WS),
            client=time_skip_env.client,
            task_queue=TASK_QUEUE,
            commit=session.commit,
        )
        async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
            run = await engine.astart(task_id)
            assert run.id is not None
            handle: Any = engine.workflow_handle(run.id)
            for _ in range(300):
                if "spec_approved_by_human" in await handle.query(FeatureWorkflow.awaiting):
                    break
                await asyncio.sleep(0.02)
            worktree = _leftover_agent_worktree(factory, mirror, task_id, run.id)

            await engine.atransition(run.id, "cancel")
            result = await handle.result()

    assert result.final_state is WorkflowState.CANCELLED
    assert not worktree.exists()
    with factory() as fresh:
        projected = SqlAlchemyWorkflowStore(fresh, workspace_id=WS).get(run.id)
    assert (projected.current_state, projected.status) == ("cancelled", RunStatus.CANCELLED)


class _DatabaseDown:
    def __enter__(self) -> Session:
        raise OperationalError("SELECT agent_run", {}, Exception("connection refused"))

    def __exit__(self, *_: object) -> None:
        return None


def test_cleanup_reports_false_when_the_database_is_down() -> None:
    cleanup = tm.make_cleanup_fn(_DatabaseDown)  # type: ignore[arg-type]

    assert cleanup(CleanupInput(workflow_run_id=uuid.uuid4(), workspace_id=WS)) is False


def test_cleanup_reports_false_when_git_cannot_remove_the_worktree(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    task_id = _seed_task(factory)
    run_id = uuid.uuid4()
    worktree = _leftover_agent_worktree(factory, mirror, task_id, run_id)
    # A locked worktree refuses a single ``remove --force``: a real git failure.
    subprocess.run(
        ["git", "-C", str(mirror), "worktree", "lock", str(worktree)],
        check=True,
        capture_output=True,
    )

    cleanup = tm.make_cleanup_fn(factory)

    assert cleanup(CleanupInput(workflow_run_id=run_id, workspace_id=WS)) is False
    assert worktree.exists()


async def test_a_cancel_whose_cleanup_fails_still_reaches_cancelled(time_skip_env: Any) -> None:
    store = InMemoryWorkflowStore()
    activities = WorkflowActivities(
        store=store,
        cleanup_fn=tm.make_cleanup_fn(_DatabaseDown),  # type: ignore[arg-type]
        allow_fakes=True,
    )
    engine = TemporalWorkflowEngine(
        workspace_id=WS, store=store, client=time_skip_env.client, task_queue=TASK_QUEUE
    )
    async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
        run = await engine.astart(uuid.uuid4())
        assert run.id is not None
        handle: Any = engine.workflow_handle(run.id)
        for _ in range(300):
            if "spec_approved_by_human" in await handle.query(FeatureWorkflow.awaiting):
                break
            await asyncio.sleep(0.02)

        await engine.atransition(
            run.id, "cancel", actor="user:decider", reason="gate_plan_rejected"
        )
        result = await handle.result()

    assert result.final_state is WorkflowState.CANCELLED
    projected = store.get(run.id)
    assert (projected.current_state, projected.status) == ("cancelled", RunStatus.CANCELLED)


def test_cleanup_reports_false_when_mirror_discovery_fails(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreadable(_workspace_id: uuid.UUID) -> Path:
        raise PermissionError("mirrors root not readable")

    monkeypatch.setattr(tm, "workspace_mirrors", _unreadable)
    _seed_task(factory)

    cleanup = tm.make_cleanup_fn(factory)

    assert cleanup(CleanupInput(workflow_run_id=uuid.uuid4(), workspace_id=WS)) is False


def test_cleanup_out_of_time_reports_false_before_another_git_call(
    factory: sessionmaker[Session], mirror: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow cleanup must answer ``False`` inside the activity's start-to-close,
    never time the activity out (that would fail the cancel)."""
    task_id = _seed_task(factory)
    run_id = uuid.uuid4()
    worktree = _leftover_agent_worktree(factory, mirror, task_id, run_id)
    monkeypatch.setattr(tm, "CLEANUP_BUDGET_SECONDS", 0.0)

    cleanup = tm.make_cleanup_fn(factory)

    assert cleanup(CleanupInput(workflow_run_id=run_id, workspace_id=WS)) is False
    assert worktree.exists()


async def test_cleanup_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    seen: list[int] = []

    def _body(_inp: CleanupInput) -> bool:
        seen.append(threading.get_ident())
        return True

    activities = WorkflowActivities(store=InMemoryWorkflowStore(), cleanup_fn=_body)

    assert await activities.cleanup_worktree(
        CleanupInput(workflow_run_id=uuid.uuid4(), workspace_id=WS)
    )
    assert seen and seen[0] != threading.get_ident()
