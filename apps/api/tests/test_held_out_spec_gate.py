"""Held-out scenarios are decided at the run's spec gate (#288 criteria 1, 2 and 4)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_api.schemas.held_out import HeldOutScenarioCreate
from scf_api.services.approval_service import get_approval_service
from scf_api.services.held_out import (
    ScenariosUndecided,
    ensure_scenarios_decided,
    store_agent_draft,
    withdraw_run_scenarios,
)
from scf_approval import (
    ApprovalAuthorizer,
    ApprovalService,
    GateRegistry,
    InMemoryApprovalRepository,
)
from scf_approval.models import GateType
from scf_contracts import UserRole
from scf_db.base import Base
from scf_db.models import Project, Task, WorkflowRun, Workspace
from scf_db.models.audit import AuditLog

WS = uuid.UUID("00000000-0000-0000-0000-000000000288")
MARKER = "HELD-OUT-MARKER-288c"
SCENARIO = HeldOutScenarioCreate.model_validate(
    {
        "title": "A refund settles in the original currency",
        "files": [{"path": "tests/held_out/test_refund.py", "content": f"# {MARKER}\n"}],
        "command": f"echo {MARKER}",
    }
)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


@pytest.fixture
def approvals() -> ApprovalService:
    return ApprovalService(InMemoryApprovalRepository(), GateRegistry(), ApprovalAuthorizer())


@pytest.fixture
def client_as(
    factory: sessionmaker[Session], approvals: ApprovalService
) -> Callable[[Principal], TestClient]:
    app = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_approval_service] = lambda: approvals

    def _client(principal: Principal) -> TestClient:
        app.dependency_overrides[get_current_principal] = lambda: principal
        return TestClient(app)

    return _client


def _person(role: UserRole = UserRole.MEMBER) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=WS,
        role=role,
        auth_method="session_jwt",
        scopes=["*"],
        decides=True,
    )


def _draft(s: Session, task_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    return store_agent_draft(
        s,
        workspace_id=WS,
        task_id=task_id,
        body=SCENARIO,
        drafted_by="agent:verifier/gpt-6-astra",
        agent_run_id=uuid.uuid4(),
        workflow_run_id=run_id,
    ).id


def _run_with_drafts(factory: sessionmaker[Session], count: int = 2) -> tuple[uuid.UUID, ...]:
    """A run whose verifier drafted ``count`` scenarios: ``(task, run, *scenarios)``."""
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(workspace_id=WS, project_id=project.id, key="CORE-288", title="Refunds")
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.flush()
        ids = [_draft(s, task.id, run.id) for _ in range(count)]
        s.commit()
        return task.id, run.id, *ids


def _spec_gate(approvals: ApprovalService, task_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    request = asyncio.run(
        approvals.create(
            workspace_id=WS,
            gate_type=GateType.SPEC,
            subject_type="workflow_run",
            subject_id=run_id,
            workflow_run_id=run_id,
            task_id=task_id,
            requested_actor=f"agent:{run_id}",
        )
    )
    return request.id


def _url(task_id: uuid.UUID, scenario_id: uuid.UUID, action: str = "") -> str:
    return f"/tasks/{task_id}/held-out-scenarios/{scenario_id}{action}"


def test_a_person_reads_and_rejects_a_drafted_scenario(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    """Criterion 1: the content is read through the #176 route; a rejection is final."""
    task_id, run_id, scenario_id = _run_with_drafts(factory, 1)
    client = client_as(_person())

    [listed] = client.get(f"/tasks/{task_id}/held-out-scenarios").json()
    assert listed["command"] == f"echo {MARKER}" and listed["workflow_run_id"] == str(run_id)

    rejected = client.post(_url(task_id, scenario_id, "/reject"))

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "rejected" and rejected.json()["approved_by"] is None
    # A rejected scenario is never run: it can never become approved.
    assert client.post(_url(task_id, scenario_id, "/approve")).status_code == 409
    assert client.post(_url(task_id, scenario_id, "/reject")).status_code == 200
    assert client.get(f"/tasks/{task_id}/held-out-scenarios").json()[0]["state"] == "rejected"


def test_a_scenario_is_rejected_only_by_a_person_who_did_not_draft_it(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    task_id, _ = _run_with_drafts(factory, 0)
    drafter = _person()
    created = client_as(drafter).post(
        f"/tasks/{task_id}/held-out-scenarios", json=SCENARIO.model_dump()
    )
    scenario_id = uuid.UUID(created.json()["id"])
    machine = _person().model_copy(
        update={"auth_method": "api_key", "decides": False, "credential_id": uuid.uuid4()}
    )

    assert client_as(drafter).post(_url(task_id, scenario_id, "/reject")).status_code == 403
    assert client_as(machine).post(_url(task_id, scenario_id, "/reject")).status_code == 403
    assert client_as(_person()).post(_url(task_id, scenario_id, "/reject")).status_code == 200


def test_the_spec_gate_is_refused_while_a_drafted_scenario_is_undecided(
    factory: sessionmaker[Session],
    approvals: ApprovalService,
    client_as: Callable[[Principal], TestClient],
) -> None:
    """Criterion 2: a typed refusal until each scenario is approved or rejected."""
    task_id, run_id, first, second = _run_with_drafts(factory)
    gate_id = _spec_gate(approvals, task_id, run_id)
    person = client_as(_person())
    approve = {"decision": "approve"}

    with factory() as s, pytest.raises(ScenariosUndecided) as undecided:
        ensure_scenarios_decided(s, workspace_id=WS, workflow_run_id=run_id)
    assert set(undecided.value.scenario_ids) == {first, second}

    refused = person.post(f"/approvals/{gate_id}/decision", json=approve)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "held_out_scenarios_undecided"
    assert set(detail["scenario_ids"]) == {str(first), str(second)}
    assert person.get(f"/approvals/{gate_id}").json()["status"] == "pending"
    assert person.get(f"/approvals/{gate_id}/decisions").json() == []  # no vote recorded

    assert person.post(_url(task_id, first, "/approve")).status_code == 200
    still = person.post(f"/approvals/{gate_id}/decision", json=approve)
    assert still.status_code == 409 and still.json()["detail"]["scenario_ids"] == [str(second)]

    assert person.post(_url(task_id, second, "/reject")).status_code == 200
    approved = person.post(f"/approvals/{gate_id}/decision", json=approve)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


def test_changes_requested_on_the_spec_gate_is_not_refused(
    factory: sessionmaker[Session],
    approvals: ApprovalService,
    client_as: Callable[[Principal], TestClient],
) -> None:
    """Only an approval waits on the scenarios: the spec can always go back for changes."""
    task_id, run_id, _ = _run_with_drafts(factory, 1)
    gate_id = _spec_gate(approvals, task_id, run_id)

    resp = client_as(_person()).post(
        f"/approvals/{gate_id}/decision", json={"decision": "request_changes"}
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.AGENT_RUNNER])
def test_a_caller_who_may_not_approve_hears_that_before_the_scenarios(
    factory: sessionmaker[Session],
    approvals: ApprovalService,
    client_as: Callable[[Principal], TestClient],
    role: UserRole,
) -> None:
    """The authorizer's 403 wins over the undecided scenarios' 409 (review of #288)."""
    task_id, run_id, _ = _run_with_drafts(factory, 1)
    gate_id = _spec_gate(approvals, task_id, run_id)

    resp = client_as(_person(role)).post(
        f"/approvals/{gate_id}/decision", json={"decision": "approve"}
    )

    assert resp.status_code == 403, resp.text


def test_an_escalated_spec_gate_refuses_a_member_before_the_scenarios(
    factory: sessionmaker[Session],
    approvals: ApprovalService,
    client_as: Callable[[Principal], TestClient],
) -> None:
    task_id, run_id, _ = _run_with_drafts(factory, 1)
    gate_id = _spec_gate(approvals, task_id, run_id)
    member = client_as(_person())
    url = f"/approvals/{gate_id}/decision"
    assert member.post(url, json={"decision": "escalate"}).status_code == 200

    resp = member.post(url, json={"decision": "approve"})

    assert resp.status_code == 403, resp.text


def test_a_decided_spec_gate_says_so_before_the_next_rounds_scenarios(
    factory: sessionmaker[Session],
    approvals: ApprovalService,
    client_as: Callable[[Principal], TestClient],
) -> None:
    """A stale approve on a gate that returned changes hears that, not the redraft's ids."""
    task_id, run_id = _run_with_drafts(factory, 0)
    gate_id = _spec_gate(approvals, task_id, run_id)
    url = f"/approvals/{gate_id}/decision"
    assert client_as(_person()).post(url, json={"decision": "request_changes"}).status_code == 200
    with factory() as s:  # the next round's drafts
        _draft(s, task_id, run_id)
        s.commit()

    resp = client_as(_person()).post(url, json={"decision": "approve"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == f"approval {gate_id} is already changes_requested"


def test_a_withdrawn_scenario_is_never_decided_nor_deleted(
    factory: sessionmaker[Session],
    approvals: ApprovalService,
    client_as: Callable[[Principal], TestClient],
) -> None:
    """Criterion 3 (the store's side): withdrawn whatever its state, and never run."""
    task_id, run_id, approved, draft = _run_with_drafts(factory)
    person = client_as(_person())
    assert person.post(_url(task_id, approved, "/approve")).status_code == 200
    # A verifier's draft is decided, not deleted: deleting would pass the spec gate.
    assert person.delete(_url(task_id, draft)).status_code == 409

    with factory() as s:
        withdrawn = withdraw_run_scenarios(s, workspace_id=WS, workflow_run_id=run_id)
        s.commit()
    assert {row.id for row in withdrawn} == {approved, draft}

    states = {
        s["id"]: s["state"] for s in person.get(f"/tasks/{task_id}/held-out-scenarios").json()
    }
    assert states == {str(approved): "withdrawn", str(draft): "withdrawn"}
    for scenario_id in (approved, draft):
        assert person.post(_url(task_id, scenario_id, "/approve")).status_code == 409
        assert person.post(_url(task_id, scenario_id, "/reject")).status_code == 409
    # Nothing drafted for the current spec is undecided.
    gate_id = _spec_gate(approvals, task_id, run_id)
    resp = person.post(f"/approvals/{gate_id}/decision", json={"decision": "approve"})
    assert resp.status_code == 200, resp.text


def test_each_decision_is_audited_with_the_run_and_the_person_never_the_content(
    factory: sessionmaker[Session], client_as: Callable[[Principal], TestClient]
) -> None:
    """Criterion 4."""
    task_id, run_id, first, second = _run_with_drafts(factory)
    approver, rejecter = _person(), _person()
    client_as(approver).post(_url(task_id, first, "/approve"))
    client_as(rejecter).post(_url(task_id, second, "/reject"))
    with factory() as s:
        withdraw_run_scenarios(s, workspace_id=WS, workflow_run_id=run_id)
        s.commit()

    with factory() as s:
        entries = list(s.scalars(select(AuditLog).where(AuditLog.action.like("held_out.%"))))
    by_action = {(e.action, e.details["scenario_id"]): e for e in entries}
    approved = by_action[("held_out.approved", str(first))]
    rejected = by_action[("held_out.rejected", str(second))]
    assert (approved.actor_id, approved.actor_type) == (approver.user_id, "user")
    assert (rejected.actor_id, rejected.actor_type) == (rejecter.user_id, "user")
    for entry in (approved, rejected, by_action[("held_out.withdrawn", str(first))]):
        assert entry.details["workflow_run_id"] == str(run_id)
    assert by_action[("held_out.withdrawn", str(second))].actor_type == "system"
    assert not any(MARKER in json.dumps(e.details, default=str) for e in entries)
