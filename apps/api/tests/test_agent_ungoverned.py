"""Outside development a repository-changing run is governed or refused (#277).

Owner decision 2026-11-15 (spec FR-005): when the durable engine is unavailable,
a production deployment refuses a repository-changing run; an ungoverned run is
allowed only in development, labelled as such.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_agent import AgentRunner
from scf_agent.testing import ScriptedModelClient, finish_response
from scf_api.db import get_db
from scf_api.main import create_app
from scf_api.routers.agent import get_agent_enqueue, get_agent_runner
from scf_api.settings import get_settings
from scf_contracts import AgentObjective
from scf_db.models import AgentRun, Project, RepositoryConnection, Task
from scf_db.models.runs import WorkflowRun

#: conftest's test workspace.
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
REPO = "acme/widgets"


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    def _set(name: str) -> None:
        monkeypatch.setenv("FORGE_ENVIRONMENT", name)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def enqueued() -> list[AgentObjective]:
    return []


@pytest.fixture
def client(
    authenticate_app: Callable[..., FastAPI],
    agent_session: Session,
    enqueued: list[AgentObjective],
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    runner = AgentRunner(ScriptedModelClient(responses=[], default=finish_response("unused")))
    app.dependency_overrides[get_agent_runner] = lambda: runner
    app.dependency_overrides[get_db] = lambda: agent_session
    app.dependency_overrides[get_agent_enqueue] = lambda: enqueued.append
    with TestClient(app) as c:
        yield c


def _task(session: Session) -> Task:
    project = Project(workspace_id=TEST_WORKSPACE_ID, name="Core", key="CORE")
    session.add(project)
    session.flush()
    task = Task(workspace_id=TEST_WORKSPACE_ID, project_id=project.id, key="CORE-1", title="W")
    session.add(task)
    session.add(RepositoryConnection(workspace_id=TEST_WORKSPACE_ID, repo_id=REPO, name=REPO))
    session.commit()
    return task


def _body(task: Task, **extra: Any) -> dict[str, Any]:
    return {
        "objective": "Make widgets accessible",
        "task_id": str(task.id),
        "repo_targets": [{"repo": REPO}],
        **extra,
    }


def _nothing_ran(session: Session, enqueued: list[AgentObjective]) -> bool:
    return enqueued == [] and session.scalars(select(AgentRun)).all() == []


def test_production_without_the_durable_engine_refuses_a_repository_run(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    environment: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 2: the postgres_fsm backend in production refuses, naming why."""
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "postgres_fsm")
    environment("production")
    task = _task(agent_session)

    resp = client.post("/agent/runs", json=_body(task))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == ("ungoverned_run_refused: the durable engine is not selected")
    assert _nothing_ran(agent_session, enqueued)


def test_production_refuses_a_supervised_repository_run(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    environment: Callable[[str], None],
    fake_temporal: Any,
) -> None:
    """Criterion 4: a supervised run stays off the workflow, so production refuses it."""
    environment("production")
    task = _task(agent_session)

    resp = client.post("/agent/runs", json=_body(task, execution_mode="supervised_multi_agent"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "ungoverned_run_refused: supervised runs are not governed"
    assert _nothing_ran(agent_session, enqueued) and fake_temporal.started == []


def test_development_keeps_the_direct_path_for_a_supervised_run(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    environment: Callable[[str], None],
    fake_temporal: Any,
) -> None:
    """Criterion 4: in development a supervised run stays on its current path."""
    environment("development")
    task = _task(agent_session)

    resp = client.post("/agent/runs", json=_body(task, execution_mode="supervised_multi_agent"))

    assert resp.status_code == 202, resp.text
    assert resp.json()["workflow_run_id"] is None
    assert len(enqueued) == 1 and fake_temporal.started == []


def test_development_keeps_the_direct_path_without_the_durable_engine(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    environment: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 3: development with postgres_fsm runs as today (labelled downstream)."""
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "postgres_fsm")
    environment("development")
    task = _task(agent_session)

    resp = client.post("/agent/runs", json=_body(task))

    assert resp.status_code == 202, resp.text
    assert len(enqueued) == 1


def test_production_still_runs_an_objective_that_changes_no_repository(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    environment: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "postgres_fsm")
    environment("production")

    resp = client.post("/agent/runs", json={"objective": "Summarise the open incidents"})

    assert resp.status_code == 202, resp.text
    assert len(enqueued) == 1


def test_a_durable_engine_that_cannot_start_the_run_refuses_it(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    environment: Callable[[str], None],
    fake_temporal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 2: Temporal selected but unreachable is a typed refusal, never the
    direct path."""
    environment("production")
    task = _task(agent_session)

    async def unreachable(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(fake_temporal, "start_workflow", unreachable)

    resp = client.post("/agent/runs", json=_body(task))

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "durable_engine_unavailable"
    assert _nothing_ran(agent_session, enqueued)
    [run] = agent_session.scalars(select(WorkflowRun)).all()
    assert run.status.value == "failed"  # the engine records the failed start
