"""``POST /agent/runs`` starts a governed workflow run under the Temporal backend (#276)."""

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
from scf_contracts import AgentObjective
from scf_db.models import AgentRun, Project, RepositoryConnection, Task, Workspace
from scf_db.models.runs import WorkflowRun
from scf_workflow.temporal.ids import agent_run_id

#: conftest's test workspace and the principal ``authenticate_app`` installs.
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
REPO = "acme/widgets"


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


def _task(session: Session, *, workspace_id: uuid.UUID = TEST_WORKSPACE_ID, **kw: Any) -> Task:
    project = Project(workspace_id=workspace_id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(
        workspace_id=workspace_id,
        project_id=project.id,
        key=f"CORE-{uuid.uuid4().hex[:4]}",
        title="Widgets",
        **kw,
    )
    session.add(task)
    session.add(RepositoryConnection(workspace_id=workspace_id, repo_id=REPO, name=REPO))
    session.commit()
    return task


def _runs(session: Session) -> list[WorkflowRun]:
    return list(session.scalars(select(WorkflowRun)))


def _body(task: Task | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "objective": "Make widgets accessible",
        "description": "Every widget has a label.",
        "repo_targets": [{"repo": REPO}],
        **extra,
    }
    if task is not None:
        body["task_id"] = str(task.id)
    return body


def test_a_repository_changing_run_starts_a_workflow_run(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    fake_temporal: Any,
) -> None:
    """Criterion 1: 202 with the workflow run's id and the agent run id its agent
    step will take; no agent runs before the workflow's gates."""
    task = _task(agent_session)

    resp = client.post("/agent/runs", json=_body(task))

    assert resp.status_code == 202, resp.text
    [run] = _runs(agent_session)
    body = resp.json()
    assert body["workflow_run_id"] == str(run.id)
    assert body["run_id"] == str(agent_run_id(run.id))
    assert body["task_id"] == str(task.id) and body["status"] == "pending"
    assert enqueued == [], "no Celery agent run: the agent runs inside the workflow"
    assert agent_session.scalars(select(AgentRun)).all() == []
    [params] = fake_temporal.started
    assert params.workflow_run_id == run.id and params.task_id == task.id
    assert params.started_by == f"user:{TEST_USER_ID}"
    trigger = run.context["trigger"]
    assert (trigger["source"], trigger["started_by"]) == ("api", f"user:{TEST_USER_ID}")
    # The objective is kept on the run for forge.run_agent, as issue dispatch keeps it.
    kept = AgentObjective.model_validate(trigger["objective"])
    assert (kept.objective, kept.description) == (
        "Make widgets accessible",
        "Every widget has a label.",
    )
    assert kept.task_id == task.id
    agent_session.refresh(task)
    assert [t["repo"] for t in task.repo_targets] == [REPO]


def test_a_governed_run_without_a_task_is_refused(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    fake_temporal: Any,
) -> None:
    """Criterion 2: a workflow run needs a board task."""
    _task(agent_session)

    resp = client.post("/agent/runs", json=_body(None))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "governed_run_requires_task"
    assert enqueued == [] and _runs(agent_session) == [] and fake_temporal.started == []


def test_a_run_with_no_repository_target_is_unaffected(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    fake_temporal: Any,
) -> None:
    """Criterion 3: an objective that changes no repository is enqueued as before."""
    resp = client.post("/agent/runs", json={"objective": "Summarise the open incidents"})

    assert resp.status_code == 202, resp.text
    assert resp.json()["workflow_run_id"] is None
    [objective] = enqueued
    assert objective.context["agent_run_id"] == resp.json()["run_id"]
    assert _runs(agent_session) == [] and fake_temporal.started == []


def test_a_caller_cannot_set_the_workflow_run_id(
    client: TestClient,
    agent_session: Session,
    fake_temporal: Any,
) -> None:
    """Criterion 4: server-owned context keys never reach the kept objective."""
    task = _task(agent_session)
    forged = {
        "workflow_run_id": str(uuid.uuid4()),
        "agent_run_id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "repo_path": "/etc",
        "tracker": {"ref": "github_issues:acme/widgets#1"},
        "note": "kept",
    }

    resp = client.post("/agent/runs", json=_body(task, context=forged))

    assert resp.status_code == 202, resp.text
    [run] = _runs(agent_session)
    assert resp.json()["workflow_run_id"] == str(run.id)
    assert run.context["trigger"]["objective"]["context"] == {"note": "kept"}


def test_a_second_run_for_an_active_task_is_refused(
    client: TestClient,
    agent_session: Session,
    fake_temporal: Any,
) -> None:
    task = _task(agent_session)
    assert client.post("/agent/runs", json=_body(task)).status_code == 202

    resp = client.post("/agent/runs", json=_body(task))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "governed_run_active"
    assert len(_runs(agent_session)) == 1 and len(fake_temporal.started) == 1


def test_a_task_on_another_repository_is_refused(
    client: TestClient,
    agent_session: Session,
    fake_temporal: Any,
) -> None:
    """Every step of a governed run reads the task's repository: it cannot be two."""
    task = _task(agent_session, repo_targets=[{"repo": "acme/other"}])

    resp = client.post("/agent/runs", json=_body(task))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "task_repository_mismatch"
    assert _runs(agent_session) == [] and fake_temporal.started == []


def test_a_task_of_another_workspace_is_not_found(
    client: TestClient,
    agent_session: Session,
    fake_temporal: Any,
) -> None:
    other = uuid.uuid4()
    agent_session.add(Workspace(id=other, name="Other", slug="other"))
    agent_session.commit()
    foreign = _task(agent_session, workspace_id=other)
    _task(agent_session)  # the repository is connected in the caller's workspace too

    resp = client.post("/agent/runs", json=_body(foreign))

    assert resp.status_code == 404, resp.text
    assert _runs(agent_session) == [] and fake_temporal.started == []


@pytest.mark.parametrize(
    ("setting", "field"),
    [
        ({"instructions": "Only touch docs/"}, "instructions"),
        ({"model": "gpt-6-astra"}, "model"),
        ({"skill_profile": {"name": "strict"}}, "skill_profile"),
        # A supervised request is not refused here: it stays on its direct path,
        # run in development and refused elsewhere (#277, test_agent_ungoverned.py).
        ({"allowed_actions": ["run_command"]}, "allowed_actions"),
        (
            {"repo_targets": [{"repo": REPO}, {"repo": "acme/other"}]},
            "repo_targets",
        ),
        ({"repo_targets": [{"repo": REPO, "skill_profile": "strict"}]}, "repo_targets"),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_a_setting_the_governed_agent_does_not_apply_is_refused(
    client: TestClient,
    agent_session: Session,
    fake_temporal: Any,
    setting: dict[str, Any],
    field: str,
) -> None:
    """#276 review: the workflow's agent step runs the task's settings, so a request
    that sets one it would drop is refused rather than accepted and ignored."""
    task = _task(agent_session)

    resp = client.post("/agent/runs", json=_body(task, **setting))

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith("governed_run_unsupported:") and field in detail
    assert _runs(agent_session) == [] and fake_temporal.started == []


def test_a_supervised_task_is_refused_before_its_gates(
    client: TestClient,
    agent_session: Session,
    fake_temporal: Any,
) -> None:
    """The agent step refuses a supervised task (ADR-0007); refusing at admission
    saves a person approving a spec the run can never act on."""
    task = _task(agent_session, execution_mode="supervised_multi_agent")

    resp = client.post("/agent/runs", json=_body(task))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "governed_run_unsupported: execution_mode"
    assert _runs(agent_session) == [] and fake_temporal.started == []
