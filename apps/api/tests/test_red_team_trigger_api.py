"""Integration tests for the Red-Team Gate V1 parity + trigger endpoint (Task 20).

Two surfaces over real handlers on hermetic SQLite (mirrors
``test_red_team_api.py`` / ``test_workflow_router.py`` conventions):

* the V1 (FSM) gate-arrival mint — a run transitioned into ``spec_review``
  through ``POST /workflow/runs/{id}/transition`` persists ONE honest parked
  verdict row (no adversary is configured in the API process), exactly where
  the Temporal spine scans (post-``submit_spec_for_review``, pre-human-gate);
* ``POST /workflow/runs/{id}/red-team`` — explicit trigger: 202 + record id,
  appends to the scan history, 409 off-gate, 404 unknown/foreign, 403 viewer.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.workflow import (
    WorkflowOwnership,
    get_workflow_engine,
    get_workflow_ownership,
)
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import Workspace
from forge_workflow import WorkflowEngineImpl

WS = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000e2")

PARKED_EVIDENCE = {"parked": True, "reason": "no adversary model/sandbox wired"}

#: created -> spec_drafting -> clarification -> spec_review (the human spec gate).
_TO_SPEC_REVIEW = ("generate_spec_draft", "gather_clarifications", "submit_spec_for_review")


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=WS2, name="Rival", slug="rival"))
        s.commit()
    yield sf
    engine.dispose()


def _principal(workspace_id: uuid.UUID = WS, role: UserRole = UserRole.MEMBER) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role=role,
        email="member@acme.test",
        auth_method="test",
        scopes=["*"],
    )


@pytest.fixture
def harness(factory: sessionmaker[Session]):
    """(app, make_client) with a fresh V1 engine + ownership per test."""
    app: FastAPI = create_app()
    engine = WorkflowEngineImpl()
    ownership = WorkflowOwnership()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_workflow_engine] = lambda: engine
    app.dependency_overrides[get_workflow_ownership] = lambda: ownership

    def make_client(principal: Principal) -> TestClient:
        app.dependency_overrides[get_current_principal] = lambda: principal
        return TestClient(app)

    return app, make_client


def _start(client: TestClient) -> str:
    resp = client.post("/workflow/runs", json={"task_id": str(uuid.uuid4())})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _drive(client: TestClient, run_id: str, *events: str) -> None:
    for event in events:
        resp = client.post(f"/workflow/runs/{run_id}/transition", json={"event": event})
        assert resp.status_code == 200, f"{event}: {resp.text}"


def _records(client: TestClient, run_id: str) -> dict:
    resp = client.get(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# V1 gate-arrival parity                                                       #
# --------------------------------------------------------------------------- #


def test_v1_run_reaching_spec_review_persists_parked_verdict(harness) -> None:
    """A V1 run transitioned into ``spec_review`` mints one honest parked
    verdict row — same shape as the Temporal default — with no extra calls."""
    _, make_client = harness
    client = make_client(_principal())
    run_id = _start(client)

    _drive(client, run_id, *_TO_SPEC_REVIEW)

    body = _records(client, run_id)
    assert body["latest"] is not None
    assert body["latest"]["verdict"] == "survived"
    assert body["latest"]["kind"] == "parked"
    assert body["latest"]["evidence"] == PARKED_EVIDENCE
    assert body["latest"]["adversary_model"] is None
    assert body["latest"]["coder_model"] is None
    assert len(body["records"]) == 1


def test_v1_gate_mint_is_once_per_run(harness) -> None:
    """Re-entering the gate (changes requested -> resubmit) does not rescan."""
    _, make_client = harness
    client = make_client(_principal())
    run_id = _start(client)
    _drive(client, run_id, *_TO_SPEC_REVIEW)
    _drive(client, run_id, "spec_changes_requested", "submit_spec_for_review")

    body = _records(client, run_id)
    assert len(body["records"]) == 1


def test_v1_pre_gate_states_record_nothing(harness) -> None:
    """No verdict is minted before the run reaches the gate."""
    _, make_client = harness
    client = make_client(_principal())
    run_id = _start(client)
    _drive(client, run_id, "generate_spec_draft")

    body = _records(client, run_id)
    assert body["latest"] is None
    assert body["records"] == []


# --------------------------------------------------------------------------- #
# POST /workflow/runs/{id}/red-team (trigger)                                  #
# --------------------------------------------------------------------------- #


def test_trigger_returns_202_and_get_returns_the_verdict(harness) -> None:
    _, make_client = harness
    client = make_client(_principal())
    run_id = _start(client)
    _drive(client, run_id, *_TO_SPEC_REVIEW)

    resp = client.post(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["workflow_run_id"] == run_id
    assert body["verdict"] == "survived"
    assert body["kind"] == "parked"
    record_id = body["record_id"]

    # NOTE: ``latest`` identity is not asserted — SQLite's second-resolution
    # CURRENT_TIMESTAMP ties the gate-arrival mint with the triggered scan and
    # the repository tiebreaks on ``id.desc()`` (random UUID order), a
    # documented property of ``RedTeamRepository.get_by_run`` (see
    # ``test_red_team_api.py``). Membership + shape is the stable contract.
    got = _records(client, run_id)
    triggered = [r for r in got["records"] if r["id"] == record_id]
    assert len(triggered) == 1
    assert triggered[0]["verdict"] == "survived"
    assert triggered[0]["kind"] == "parked"
    assert triggered[0]["evidence"] == PARKED_EVIDENCE


def test_trigger_appends_to_the_scan_history(harness) -> None:
    """The gate-arrival mint plus an explicit trigger = two records."""
    _, make_client = harness
    client = make_client(_principal())
    run_id = _start(client)
    _drive(client, run_id, *_TO_SPEC_REVIEW)  # mints record #1

    resp = client.post(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 202, resp.text

    body = _records(client, run_id)
    assert len(body["records"]) == 2
    assert resp.json()["record_id"] in {r["id"] for r in body["records"]}


def test_trigger_off_gate_is_409(harness) -> None:
    """A run not at a gateable state (freshly created) conflicts."""
    _, make_client = harness
    client = make_client(_principal())
    run_id = _start(client)

    resp = client.post(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 409, resp.text
    assert "gateable" in resp.json()["detail"]

    # And nothing was recorded (no silent gating either way).
    assert _records(client, run_id)["records"] == []


def test_trigger_unknown_run_is_404(harness) -> None:
    _, make_client = harness
    client = make_client(_principal())

    resp = client.post(f"/workflow/runs/{uuid.uuid4()}/red-team")
    assert resp.status_code == 404


def test_trigger_foreign_workspace_run_is_404(harness) -> None:
    """Another workspace's run id reads as nonexistent (no existence leak)."""
    _, make_client = harness
    owner_client = make_client(_principal(workspace_id=WS))
    run_id = _start(owner_client)
    _drive(owner_client, run_id, *_TO_SPEC_REVIEW)

    foreign_client = make_client(_principal(workspace_id=WS2))
    resp = foreign_client.post(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 404


def test_trigger_requires_write_permission(harness) -> None:
    """A read-only viewer cannot trigger a scan (matches the router's other
    write endpoints)."""
    _, make_client = harness
    member = make_client(_principal())
    run_id = _start(member)
    _drive(member, run_id, *_TO_SPEC_REVIEW)

    viewer = make_client(_principal(role=UserRole.VIEWER))
    resp = viewer.post(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 403
