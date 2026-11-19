"""A task's held-out acceptance scenarios over the API (#176 criteria 1, 2 and 9)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_contracts import UserRole
from scf_db.base import Base
from scf_db.models import Project, Task, User, Workspace
from scf_db.models.audit import AuditLog
from scf_db.models.enums import UserRole as DbUserRole

WS = uuid.UUID("00000000-0000-0000-0000-0000000001b6")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000002b6")
SCENARIO = {
    "title": "Refunds settle in the original currency",
    "files": [{"path": "tests/held_out/test_refunds.py", "content": "def test_refund(): ..."}],
    "command": "pytest tests/held_out",
}


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield sf
    engine.dispose()


def _task(factory: sessionmaker[Session], workspace_id: uuid.UUID = WS) -> uuid.UUID:
    with factory() as s:
        if s.get(Workspace, workspace_id) is None:
            s.add(Workspace(id=workspace_id, name=str(workspace_id), slug=str(workspace_id)))
            s.flush()
        project = Project(workspace_id=workspace_id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
        s.add(project)
        s.flush()
        task = Task(workspace_id=workspace_id, project_id=project.id, key="CORE-1", title="Refunds")
        s.add(task)
        s.commit()
        return task.id


def _person(
    role: UserRole = UserRole.MEMBER, workspace_id: uuid.UUID = WS, *, decides: bool = True
) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role=role,
        auth_method="session_jwt" if decides else "api_key",
        scopes=["*"],
        decides=decides,
        credential_id=None if decides else uuid.uuid4(),
    )


@pytest.fixture
def client_as(factory: sessionmaker[Session]) -> Callable[[Principal], TestClient]:
    app = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db

    def _client(principal: Principal) -> TestClient:
        app.dependency_overrides[get_current_principal] = lambda: principal
        return TestClient(app)

    return _client


def _url(task_id: uuid.UUID, suffix: str = "") -> str:
    return f"/tasks/{task_id}/held-out-scenarios{suffix}"


def test_a_writer_creates_lists_and_deletes_a_draft(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    """Criterion 1: created as a draft, with who drafted it."""
    task_id = _task(factory)
    drafter = _person()
    client = client_as(drafter)

    created = client.post(_url(task_id), json=SCENARIO)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["state"] == "draft" and body["approved_by"] is None
    assert body["drafted_by"] == f"user:{drafter.user_id}"
    assert [f["path"] for f in body["files"]] == ["tests/held_out/test_refunds.py"]
    assert [s["id"] for s in client.get(_url(task_id)).json()] == [body["id"]]
    with factory() as s:
        [audit] = s.scalars(select(AuditLog).where(AuditLog.action == "held_out.created")).all()
        assert audit.actor_id == drafter.user_id
        assert "pytest" not in str(audit.details)  # who, never the scenario itself

    assert client.delete(_url(task_id, f"/{body['id']}")).status_code == 204
    assert client.get(_url(task_id)).json() == []


def test_a_reader_without_write_access_is_refused(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    task_id = _task(factory)
    scenario_id = client_as(_person()).post(_url(task_id), json=SCENARIO).json()["id"]
    reader = client_as(_person(UserRole.VIEWER))

    assert reader.post(_url(task_id), json=SCENARIO).status_code == 403
    assert reader.get(_url(task_id)).status_code == 403
    assert reader.post(_url(task_id, f"/{scenario_id}/approve")).status_code == 403
    assert reader.delete(_url(task_id, f"/{scenario_id}")).status_code == 403


def test_another_workspace_sees_nothing(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    task_id = _task(factory)
    scenario_id = client_as(_person()).post(_url(task_id), json=SCENARIO).json()["id"]
    _task(factory, WS2)
    rival = client_as(_person(workspace_id=WS2))

    assert rival.get(_url(task_id)).status_code == 404
    assert rival.post(_url(task_id), json=SCENARIO).status_code == 404
    assert rival.post(_url(task_id, f"/{scenario_id}/approve")).status_code == 404
    assert rival.delete(_url(task_id, f"/{scenario_id}")).status_code == 404


def test_only_a_person_who_did_not_draft_it_approves_a_scenario(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    """Criterion 2: recorded with who approved it and when."""
    task_id = _task(factory)
    drafter = _person()
    scenario_id = client_as(drafter).post(_url(task_id), json=SCENARIO).json()["id"]

    # The drafter, and a credential that is not a person, are refused.
    assert client_as(drafter).post(_url(task_id, f"/{scenario_id}/approve")).status_code == 403
    machine = _person(decides=False)
    assert client_as(machine).post(_url(task_id, f"/{scenario_id}/approve")).status_code == 403
    assert client_as(drafter).get(_url(task_id)).json()[0]["state"] == "draft"

    approver = _person()
    approved = client_as(approver).post(_url(task_id, f"/{scenario_id}/approve"))

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["state"] == "approved"
    assert body["approved_by"] == str(approver.user_id) and body["approved_at"]


def test_the_drafters_own_key_cannot_approve_it(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    """The same person cannot draft through one credential and approve through another."""
    task_id = _task(factory)
    key = _person(decides=False)
    scenario_id = client_as(key).post(_url(task_id), json=SCENARIO).json()["id"]
    same_person = _person()
    same_person = same_person.model_copy(update={"user_id": key.user_id})

    assert client_as(same_person).post(_url(task_id, f"/{scenario_id}/approve")).status_code == 403


@pytest.mark.parametrize(
    ("owner_role", "status_code"), [("viewer", 403), ("member", 200)], ids=["demoted", "member"]
)
def test_a_persons_key_approves_with_its_owners_current_role(
    factory: sessionmaker[Session],
    client_as: Callable[[Principal], TestClient],
    owner_role: str,
    status_code: int,
) -> None:
    """A key minted while its owner was a member cannot approve after the owner is
    demoted, as on every approval decision."""
    task_id = _task(factory)
    scenario_id = client_as(_person()).post(_url(task_id), json=SCENARIO).json()["id"]
    key = Principal(
        user_id=uuid.uuid4(),
        workspace_id=WS,
        role=UserRole.MEMBER,
        auth_method="api_key",
        scopes=["*"],
        decides=True,
        credential_id=uuid.uuid4(),
        surface="api",
    )
    with factory() as s:
        s.add(
            User(id=key.user_id, workspace_id=WS, email="o@acme.test", role=DbUserRole(owner_role))
        )
        s.commit()

    resp = client_as(key).post(_url(task_id, f"/{scenario_id}/approve"))

    assert resp.status_code == status_code, resp.text
    if status_code == 200:
        with factory() as s:
            [audit] = s.scalars(select(AuditLog).where(AuditLog.action == "held_out.approved"))
            assert audit.details["key_id"] == str(key.credential_id)
            assert audit.details["surface"] == "api"


def test_two_files_for_one_path_are_refused(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    """One target file with two contents would leave the approver unsure which runs."""
    task_id = _task(factory)
    files = [
        {"path": "tests/x.py", "content": "one"},
        {"path": "./tests//x.py", "content": "two"},
    ]

    resp = client_as(_person()).post(_url(task_id), json={**SCENARIO, "files": files})

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/etc/passwd", "tests/../../outside.py", ".git/config", "C:/x.py", "./"],
)
def test_a_path_that_leaves_the_checkout_is_refused(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient], path: str
) -> None:
    """Criterion 9."""
    task_id = _task(factory)
    body = {**SCENARIO, "files": [{"path": path, "content": "x"}]}

    assert client_as(_person()).post(_url(task_id), json=body).status_code == 422


def test_a_scenarios_results_are_read_by_id_and_title_by_writers_only(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    """#286 criterion 7: each run of a scenario on a change reads back by its id and
    title, never its files or command, and only for a person with write access."""
    from scf_api.services.held_out import record_result
    from scf_db.models.held_out import HeldOutScenario

    task_id = _task(factory)
    writer = client_as(_person())
    scenario_id = writer.post(_url(task_id), json=SCENARIO).json()["id"]
    run_id, other_run = uuid.uuid4(), uuid.uuid4()
    with factory() as s:
        row = s.get(HeldOutScenario, uuid.UUID(scenario_id))
        assert row is not None
        for run, passed in ((run_id, False), (other_run, True)):
            outcome = {
                "passed": passed,
                "exit_code": 0 if passed else 1,
                "timed_out": False,
                "duration_ms": 12,
                "reason": None if passed else "exited_nonzero",
            }
            record_result(
                s, row, workflow_run_id=run, attempt=0, head_commit="ab" * 20, outcome=outcome
            )
        s.commit()

    resp = writer.get(_url(task_id, "/results"), params={"workflow_run_id": str(run_id)})

    assert resp.status_code == 200, resp.text
    [result] = resp.json()
    assert result.pop("recorded_at")
    assert result == {
        "scenario_id": scenario_id,
        "title": SCENARIO["title"],
        "workflow_run_id": str(run_id),
        "attempt": 0,
        "head_commit": "ab" * 20,
        "passed": False,
        "exit_code": 1,
        "timed_out": False,
        "duration_ms": 12,
        "reason": "exited_nonzero",
    }
    assert len(writer.get(_url(task_id, "/results")).json()) == 2
    assert client_as(_person(UserRole.VIEWER)).get(_url(task_id, "/results")).status_code == 403
    _task(factory, WS2)
    assert client_as(_person(workspace_id=WS2)).get(_url(task_id, "/results")).status_code == 404
