"""Spec M M3: ``POST /agent/runs`` with a ``tracker_ref`` (and acceptance 5/6).

A ``tracker_ref`` resolves through the PM link store / connection to the same
objective builder the webhook dispatch uses; unknown or foreign refs are 404;
caller-forged tracker context is stripped; a workspace with no tracker keeps
today's native run behaviour unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_api.db import get_db
from forge_api.main import create_app
from forge_api.routers.agent import (
    get_agent_enqueue,
    get_agent_runner,
    get_tracker_adapter_factory,
)
from forge_contracts import AgentObjective
from forge_contracts.enums import Direction
from forge_contracts.pm import ExternalTask, StatusCategory
from forge_contracts.pm import PMProvider as ContractsProvider
from forge_db.models import Project, RepositoryConnection, Task, Workspace
from forge_db.models.enums import PMConnectionStatus
from forge_db.models.enums import PMProvider as DbPMProvider
from forge_db.models.pm import PMConnection, PMTaskLink
from forge_integrations.pm.errors import ExternalNotFound

TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c9")


class FakeTrackerAdapter:
    provider = ContractsProvider.github_issues

    def __init__(self) -> None:
        self.fetch_calls: list[str] = []

    def map_status(self, value: str, direction: Direction) -> str:
        return StatusCategory.started.value

    def map_priority(self, value: str, direction: Direction) -> str:
        return value

    def map_fields(self, fields: dict, direction: Direction) -> dict:
        return dict(fields)

    async def fetch_external(self, external_id: str) -> ExternalTask:
        self.fetch_calls.append(external_id)
        if external_id == "999":
            raise ExternalNotFound("no such issue")
        return ExternalTask(
            provider=ContractsProvider.github_issues,
            external_id=external_id,
            external_key=f"acme/widgets#{external_id}",
            url=f"https://github.com/acme/widgets/issues/{external_id}",
            title="Tracker title",
            description_md="Do it.\n\n## Acceptance criteria\n- It works\n",
            status_name="open",
            status_category=StatusCategory.unstarted,
            labels=["forge:run"],
            external_updated_at=datetime(2026, 11, 7, tzinfo=UTC),
        )


@pytest.fixture
def enqueued() -> list[AgentObjective]:
    return []


@pytest.fixture
def tracker_adapter() -> FakeTrackerAdapter:
    return FakeTrackerAdapter()


@pytest.fixture
def client(
    authenticate_app: Callable[..., FastAPI],
    agent_session: Session,
    enqueued: list[AgentObjective],
    tracker_adapter: FakeTrackerAdapter,
) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    model = ScriptedModelClient(responses=[], default=finish_response("done"))
    app.dependency_overrides[get_agent_runner] = lambda: AgentRunner(model)
    app.dependency_overrides[get_db] = lambda: agent_session
    app.dependency_overrides[get_agent_enqueue] = lambda: enqueued.append
    app.dependency_overrides[get_tracker_adapter_factory] = lambda: (
        lambda connection: tracker_adapter
    )
    with TestClient(app) as c:
        yield c


def _seed_tracker(
    session: Session,
    *,
    workspace_id: uuid.UUID = TEST_WORKSPACE_ID,
    with_link: bool = False,
    external_project_key: str = "acme/widgets",
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Seed project + repo connection + github_issues PM connection (+ link)."""
    if session.get(Workspace, workspace_id) is None:
        session.add(Workspace(id=workspace_id, name="Ws", slug=f"ws-{str(workspace_id)[-4:]}"))
        session.flush()
    project = Project(workspace_id=workspace_id, name="Core", key=f"K{str(workspace_id)[-2:]}")
    session.add(project)
    session.flush()
    session.add(
        RepositoryConnection(
            workspace_id=workspace_id, repo_id="acme/widgets", name="widgets", is_active=True
        )
    )
    conn = PMConnection(
        workspace_id=workspace_id,
        provider=DbPMProvider.GITHUB_ISSUES,
        name="GitHub Issues",
        project_id=project.id,
        external_project_key=external_project_key,
        external_project_id="10",
        config={"dispatch": {"repository": "acme/widgets"}},
        status=PMConnectionStatus.CONNECTED,
    )
    session.add(conn)
    session.flush()
    task_id = None
    if with_link:
        task = Task(workspace_id=workspace_id, project_id=project.id, key="CORE-42", title="T")
        session.add(task)
        session.flush()
        task_id = task.id
        session.add(
            PMTaskLink(
                workspace_id=workspace_id,
                connection_id=conn.id,
                forge_task_id=task.id,
                provider=DbPMProvider.GITHUB_ISSUES,
                external_id="42",
                external_key="acme/widgets#42",
                external_url="https://github.com/acme/widgets/issues/42",
            )
        )
    session.commit()
    return conn.id, task_id


def test_tracker_ref_builds_objective_from_refetched_issue(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    tracker_adapter: FakeTrackerAdapter,
) -> None:
    connection_id, _ = _seed_tracker(agent_session)
    resp = client.post("/agent/runs", json={"tracker_ref": "github_issues:acme/widgets#42"})
    assert resp.status_code == 202, resp.text
    assert tracker_adapter.fetch_calls == ["42"]
    (objective,) = enqueued
    assert objective.objective == "Tracker title"
    assert [c.text for c in objective.acceptance_criteria] == ["It works"]
    assert objective.context["source"] == "github_issues:acme/widgets#42"
    assert objective.context["tracker"]["connection_id"] == str(connection_id)
    assert objective.context["actor"].startswith("user:")
    assert objective.context["repo_full_name"] == "acme/widgets"
    assert objective.context["workspace_id"] == str(TEST_WORKSPACE_ID)


def test_tracker_ref_resolves_url_form_project_key(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
    tracker_adapter: FakeTrackerAdapter,
) -> None:
    """A connection stored with a URL-form key still resolves owner/repo refs."""
    _seed_tracker(agent_session, external_project_key="https://github.com/acme/widgets")
    resp = client.post("/agent/runs", json={"tracker_ref": "github_issues:acme/widgets#42"})
    assert resp.status_code == 202, resp.text
    assert tracker_adapter.fetch_calls == ["42"]


def test_tracker_ref_resolves_through_link_store_to_task(
    client: TestClient,
    agent_session: Session,
    enqueued: list[AgentObjective],
) -> None:
    _, task_id = _seed_tracker(agent_session, with_link=True)
    resp = client.post("/agent/runs", json={"tracker_ref": "github_issues:acme/widgets#42"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["task_id"] == str(task_id)
    assert enqueued[0].task_id == task_id


@pytest.mark.parametrize(
    "ref",
    ["github_issues:acme/widgets#999", "github_issues:other/repo#1", "nonsense", "jira:ENG-1"],
)
def test_unknown_tracker_ref_is_404(
    client: TestClient, agent_session: Session, enqueued: list[AgentObjective], ref: str
) -> None:
    _seed_tracker(agent_session)
    resp = client.post("/agent/runs", json={"tracker_ref": ref})
    assert resp.status_code == 404
    assert enqueued == []


def test_foreign_tracker_ref_is_404(
    client: TestClient, agent_session: Session, enqueued: list[AgentObjective]
) -> None:
    _seed_tracker(agent_session, workspace_id=OTHER_WORKSPACE_ID, with_link=True)
    resp = client.post("/agent/runs", json={"tracker_ref": "github_issues:acme/widgets#42"})
    assert resp.status_code == 404
    assert enqueued == []


def test_caller_forged_tracker_context_is_stripped(
    client: TestClient, enqueued: list[AgentObjective]
) -> None:
    resp = client.post(
        "/agent/runs",
        json={
            "objective": "plain",
            "context": {
                "tracker": {"connection_id": "evil", "external_id": "1"},
                "source": "github_issues:evil/repo#1",
                "actor": "pm:evil",
                "note": "kept",
            },
        },
    )
    assert resp.status_code == 202, resp.text
    (objective,) = enqueued
    assert "tracker" not in objective.context
    assert "source" not in objective.context
    assert "actor" not in objective.context
    assert objective.context["note"] == "kept"


def test_no_tracker_workspace_keeps_native_behaviour(
    client: TestClient, enqueued: list[AgentObjective]
) -> None:
    """Acceptance 5: with no tracker connected, runs behave exactly as before."""
    resp = client.post("/agent/runs", json={"objective": "Investigate the bug"})
    assert resp.status_code == 202, resp.text
    (objective,) = enqueued
    assert objective.objective == "Investigate the bug"
    assert "tracker" not in objective.context
    assert "source" not in objective.context


def test_objective_required_without_tracker_ref(client: TestClient) -> None:
    resp = client.post("/agent/runs", json={})
    assert resp.status_code == 422
