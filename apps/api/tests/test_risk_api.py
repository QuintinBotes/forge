"""Risk classifications, per-gate agreement and a dry-run classification over the API.

#193 criteria 6, 7 and 8: every route needs the read permission, answers 401 to an
anonymous caller, and never returns another workspace's rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.auth import rbac
from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_contracts import RiskClass, RiskEvaluation, UserRole
from scf_db.base import Base
from scf_db.models import (
    AgentRun,
    RiskClassification,
    RiskGateObservation,
    Workspace,
)
from scf_db.risk_repository import RiskRepository

WS = uuid.UUID("00000000-0000-0000-0000-0000000001c3")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000002c3")
RULES = {
    "default": "low",
    "derive": [
        {"id": "docs", "paths": ["docs/**"], "class": "minimal"},
        {"id": "auth", "paths": ["app/auth/**"], "class": "high"},
        {"paths": ["infra/**"], "class": "critical"},
    ],
}
ROUTES = [
    ("GET", "/risk/classifications", None),
    ("GET", "/risk/agreement", None),
    ("POST", "/risk/classify", {"changes": [{"path": "README.md"}]}),
]


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        for ws in (WS, WS2):
            s.add(Workspace(id=ws, name=str(ws), slug=str(ws)))
        s.commit()
    yield sf
    engine.dispose()


def _person(role: UserRole = UserRole.VIEWER, workspace_id: uuid.UUID = WS) -> Principal:
    return Principal(
        user_id=uuid.uuid4(), workspace_id=workspace_id, role=role, auth_method="session_jwt"
    )


@pytest.fixture
def client_as(factory: sessionmaker[Session]) -> Callable[[Principal | None], TestClient]:
    app = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db

    def _client(principal: Principal | None) -> TestClient:
        """``None`` is an anonymous caller (the overrides are the app's, so use one at a time)."""
        if principal is None:
            app.dependency_overrides.pop(get_current_principal, None)
        else:
            app.dependency_overrides[get_current_principal] = lambda: principal
        return TestClient(app)

    return _client


def _classify(
    factory: sessionmaker[Session],
    workspace_id: uuid.UUID,
    commit_sha: str,
    *,
    spec_key: str | None = None,
    final: RiskClass = RiskClass.LOW,
) -> RiskClassification:
    with factory() as s:
        run = AgentRun(workspace_id=workspace_id, inputs={})
        s.add(run)
        s.flush()
        row = RiskRepository(s).record_classification(
            workspace_id,
            RiskEvaluation(derived_class=final, final_class=final, matched_rule_ids=["r"]),
            commit_sha=commit_sha,
            signals={"paths": ["app/x.py"], "lines_added": 1},
            enforcement_mode="shadow",
            agent_run_id=run.id,
            spec_key=spec_key,
        )
        s.commit()
        return row


def _observe(
    factory: sessionmaker[Session],
    classification: RiskClassification,
    gate: str,
    agreed: bool,
    *,
    age: timedelta | None = None,
) -> None:
    with factory() as s:
        if age is None:
            RiskRepository(s).record_gate_observation(
                classification.workspace_id,
                classification_id=classification.id,
                gate_name=gate,
                actual_decision="allow",
                risk_decision="allow" if agreed else "block",
                agreed=agreed,
            )
        else:  # an old observation, written as it would have been then
            s.add(
                RiskGateObservation(
                    workspace_id=classification.workspace_id,
                    classification_id=classification.id,
                    gate_name=gate,
                    actual_decision="allow",
                    risk_decision="allow",
                    agreed=agreed,
                    created_at=datetime.now(UTC) - age,
                )
            )
        s.commit()


def test_classifications_are_listed_newest_first_and_filtered(
    factory: sessionmaker[Session], client_as: Callable[[Principal | None], TestClient]
) -> None:
    first = _classify(factory, WS, "a" * 40, spec_key="spec-a")
    second = _classify(factory, WS, "b" * 40, final=RiskClass.HIGH)
    _classify(factory, WS2, "a" * 40, spec_key="spec-a")
    client = client_as(_person())

    listed = client.get("/risk/classifications")

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert {row["id"] for row in body} == {str(first.id), str(second.id)}
    by_id = {row["id"]: row for row in body}
    assert by_id[str(second.id)]["final_class"] == "high"
    assert by_id[str(first.id)]["matched_rule_ids"] == ["r"]
    assert by_id[str(first.id)]["enforcement_mode"] == "shadow"

    def ids(**params: str) -> list[str]:
        return [row["id"] for row in client.get("/risk/classifications", params=params).json()]

    assert ids(commit_sha="a" * 40) == [str(first.id)]
    assert ids(spec_key="spec-a") == [str(first.id)]
    assert ids(agent_run_id=str(second.agent_run_id)) == [str(second.id)]
    assert ids(workflow_run_id=str(uuid.uuid4())) == []


def test_another_workspace_sees_none_of_these_rows(
    factory: sessionmaker[Session], client_as: Callable[[Principal | None], TestClient]
) -> None:
    mine = _classify(factory, WS, "c" * 40)
    _observe(factory, mine, "deploy", True)
    other = client_as(_person(workspace_id=WS2))

    assert other.get("/risk/classifications").json() == []
    assert other.get("/risk/classifications", params={"commit_sha": "c" * 40}).json() == []
    assert other.get("/risk/agreement").json()["gates"] == []


def test_agreement_is_computed_per_gate_over_the_requested_window(
    factory: sessionmaker[Session], client_as: Callable[[Principal | None], TestClient]
) -> None:
    mine = _classify(factory, WS, "d" * 40)
    theirs = _classify(factory, WS2, "d" * 40)
    for agreed in (True, True, False):
        _observe(factory, mine, "deploy", agreed)
    _observe(factory, mine, "merge", True)
    _observe(factory, mine, "approval", False, age=timedelta(days=40))
    _observe(factory, theirs, "deploy", False)
    client = client_as(_person())

    month = client.get("/risk/agreement")
    quarter = client.get("/risk/agreement", params={"days": 90})

    assert month.status_code == 200, month.text
    assert month.json()["days"] == 30
    assert month.json()["gates"] == [
        {"gate_name": "deploy", "observations": 3, "agreed": 2, "agreement_rate": 2 / 3},
        {"gate_name": "merge", "observations": 1, "agreed": 1, "agreement_rate": 1.0},
    ]
    assert [g["gate_name"] for g in quarter.json()["gates"]] == ["approval", "deploy", "merge"]
    assert quarter.json()["gates"][0]["agreement_rate"] == 0.0
    assert client.get("/risk/agreement", params={"days": 0}).status_code == 422


def test_a_dry_run_returns_the_derived_class_and_writes_nothing(
    factory: sessionmaker[Session], client_as: Callable[[Principal | None], TestClient]
) -> None:
    client = client_as(_person())
    changes = [
        {"path": "docs/guide.md", "lines_added": 4},
        {"path": "app/auth/login.py", "lines_added": 2, "lines_removed": 1},
        {"path": "infra/main.tf", "lines_added": 1},
    ]

    classified = client.post("/risk/classify", json={"changes": changes, "rules": RULES})
    docs_only = client.post("/risk/classify", json={"changes": changes[:1], "rules": RULES})
    no_rules = client.post("/risk/classify", json={"changes": changes})

    assert classified.status_code == 200, classified.text
    assert classified.json() == {
        "risk_class": "critical",
        "matched_rule_ids": ["docs", "auth", "derive[2]"],
    }
    # The policy default is the floor: a minimal rule cannot lower a change below it.
    assert docs_only.json() == {"risk_class": "low", "matched_rule_ids": ["docs"]}
    # No rules, no classification (FR-007).
    assert no_rules.json() == {"risk_class": None, "matched_rule_ids": []}
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(RiskClassification)) == 0
        assert s.scalar(select(func.count()).select_from(RiskGateObservation)) == 0


def test_a_dry_run_rejects_invalid_rules_and_changes(
    client_as: Callable[[Principal | None], TestClient],
) -> None:
    client = client_as(_person())
    path = [{"path": "a.py"}]

    unknown_class = {"default": "severe"}
    assert (
        client.post("/risk/classify", json={"changes": path, "rules": unknown_class}).status_code
        == 422
    )
    negative = [{"path": "a.py", "lines_added": -1}]
    assert client.post("/risk/classify", json={"changes": negative}).status_code == 422
    assert client.post("/risk/classify", json={"changes": [{"path": ""}]}).status_code == 422


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_each_route_needs_a_caller_with_the_read_permission(
    client_as: Callable[[Principal | None], TestClient],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    assert client_as(None).request(method, path, json=body).status_code == 401
    viewer = client_as(_person(UserRole.VIEWER))
    assert viewer.request(method, path, json=body).status_code == 200  # read is enough

    monkeypatch.setitem(rbac.ROLE_PERMISSIONS, UserRole.VIEWER, frozenset())
    assert viewer.request(method, path, json=body).status_code == 403
