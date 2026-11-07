"""Durable agent admission and reads, with injected model and enqueue dependencies."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_api.db import get_db
from forge_api.deps import get_current_principal
from forge_api.main import create_app
from forge_api.routers.agent import get_agent_enqueue, get_agent_runner
from forge_contracts import AgentObjective
from forge_db.models import AgentRun
from forge_db.models.enums import RunStatus


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
    model = ScriptedModelClient(
        responses=[finish_response("done", confidence=0.95)],
        default=finish_response("done", confidence=0.95),
    )
    runner = AgentRunner(model)
    app.dependency_overrides[get_agent_runner] = lambda: runner
    app.dependency_overrides[get_db] = lambda: agent_session
    app.dependency_overrides[get_agent_enqueue] = lambda: enqueued.append
    with TestClient(app) as c:
        yield c
    assert not model.requests


def test_run_objective_returns_result_with_run_id(client: TestClient) -> None:
    resp = client.post("/agent/runs", json={"objective": "Investigate the bug"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["run_id"]
    assert body["status"] == "pending"


def test_run_then_get_round_trips(client: TestClient) -> None:
    created = client.post("/agent/runs", json={"objective": "Do the thing"})
    run_id = created.json()["run_id"]
    fetched = client.get(f"/agent/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id


def test_get_unknown_run_is_404(client: TestClient) -> None:
    resp = client.get(f"/agent/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_agent_run_is_committed_before_enqueue_and_context_is_trusted(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
) -> None:
    workspace_id = client.app.dependency_overrides[get_current_principal]().workspace_id

    def enqueue(objective: AgentObjective) -> None:
        # A separate session must see the committed row before dispatch.
        with Session(agent_session.get_bind()) as other:
            row = other.get(AgentRun, uuid.UUID(objective.context["agent_run_id"]))
            assert row is not None
            assert row.workspace_id == workspace_id
            assert row.inputs == objective.model_dump(mode="json")
        enqueued.append(objective)

    client.app.dependency_overrides[get_agent_enqueue] = lambda: enqueue
    response = client.post(
        "/agent/runs",
        json={
            "objective": "Do the thing",
            "context": {"workspace_id": str(uuid.uuid4()), "agent_run_id": str(uuid.uuid4())},
        },
    )
    assert response.status_code == 202
    assert len(enqueued) == 1
    objective = enqueued[0]
    assert objective.context["workspace_id"] == str(workspace_id)
    assert objective.context["agent_run_id"] == response.json()["run_id"]
    row = agent_session.get(AgentRun, uuid.UUID(response.json()["run_id"]))
    assert row.status == RunStatus.PENDING
    assert row.steps == []


def test_get_reads_persisted_status_not_cached_response(
    client: TestClient,
    agent_session: Session,
) -> None:
    run_id = client.post("/agent/runs", json={"objective": "work"}).json()["run_id"]
    row = agent_session.get(AgentRun, uuid.UUID(run_id))
    row.status = RunStatus.SUCCEEDED
    row.output = {"summary": "completed by worker", "output": "done"}
    agent_session.commit()
    fetched = client.get(f"/agent/runs/{run_id}").json()
    assert fetched["status"] == "succeeded"
    assert fetched["summary"] == "completed by worker"
    assert fetched["output"] == "done"


def test_agent_enqueue_failure_refuses_and_records_failure(
    client: TestClient,
    agent_session: Session,
) -> None:
    def unavailable(objective: AgentObjective) -> None:
        raise ConnectionError("broker secret must not be exposed")

    client.app.dependency_overrides[get_agent_enqueue] = lambda: unavailable
    response = client.post("/agent/runs", json={"objective": "work"})
    assert response.status_code == 503
    assert "secret" not in response.text
    row = agent_session.scalar(select(AgentRun))
    assert row.status == RunStatus.FAILED
    assert row.output == {"error": "enqueue_failed"}


def test_agent_supervisor_uses_the_admitted_run_as_parent(
    client: TestClient,
    enqueued: list[AgentObjective],
    agent_session: Session,
) -> None:
    response = client.post(
        "/agent/runs",
        json={
            "objective": "work",
            "execution_mode": "supervised_multi_agent",
            "context": {"parent_agent_run_id": str(uuid.uuid4())},
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert enqueued[0].context["parent_agent_run_id"] == run_id
    assert agent_session.get(AgentRun, uuid.UUID(run_id)).is_supervisor


def test_agent_enqueue_uses_worker_task_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from forge_api.routers import agent

    celery = Mock()
    monkeypatch.setattr(agent, "_celery_app", lambda: celery)
    objective = AgentObjective(
        objective="work",
        context={
            "agent_run_id": str(uuid.uuid4()),
            "workspace_id": str(uuid.uuid4()),
        },
    )
    agent.enqueue_agent_run(objective)
    celery.send_task.assert_called_once_with(
        "forge.agent.run",
        args=[objective.model_dump(mode="json")],
        kwargs={"idempotency_key": objective.context["agent_run_id"]},
        queue="forge",
    )


def test_server_owned_context_keys_from_the_caller_are_dropped(
    client: TestClient, enqueued: list[AgentObjective]
) -> None:
    hostile = {
        "repo_path": "/",
        "worktree_path": "/etc",
        "repo_full_name": "other/tenant",
        "role": "implementer",
        "workspace_id": str(uuid.uuid4()),
        "agent_run_id": str(uuid.uuid4()),
        "parent_agent_run_id": str(uuid.uuid4()),
        "note": "kept",
    }
    resp = client.post("/agent/runs", json={"objective": "x", "context": hostile})
    assert resp.status_code == 202, resp.text
    (sent,) = enqueued
    for key in ("repo_path", "worktree_path", "repo_full_name", "role", "parent_agent_run_id"):
        assert key not in sent.context
    assert sent.context["agent_run_id"] == resp.json()["run_id"]
    assert sent.context["workspace_id"] != hostile["workspace_id"]
    assert sent.context["note"] == "kept"


def test_unconnected_repository_is_refused_before_anything_is_stored(
    client: TestClient, agent_session: Session, enqueued: list[AgentObjective]
) -> None:
    resp = client.post(
        "/agent/runs", json={"objective": "x", "repo_targets": [{"repo": "/var/lib/forge"}]}
    )
    assert resp.status_code == 422
    assert "not connected" in resp.json()["detail"]
    assert enqueued == []
    assert agent_session.scalars(select(AgentRun)).all() == []


def test_connected_repository_is_named_server_side(
    client: TestClient, agent_session: Session, enqueued: list[AgentObjective]
) -> None:
    from forge_db.models import RepositoryConnection

    workspace_id = uuid.UUID("00000000-0000-0000-0000-0000000000a1")  # conftest's test workspace
    agent_session.add(
        RepositoryConnection(workspace_id=workspace_id, repo_id="acme/widgets", name="acme/widgets")
    )
    agent_session.commit()
    resp = client.post(
        "/agent/runs", json={"objective": "x", "repo_targets": [{"repo": "acme/widgets"}]}
    )
    assert resp.status_code == 202, resp.text
    assert enqueued[0].context["repo_full_name"] == "acme/widgets"
    assert "repo_path" not in enqueued[0].context
