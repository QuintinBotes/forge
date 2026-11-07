"""API tests for the /world router (world model): happy paths, RBAC, 404s.

Harness mirrors the attestation API tests: hermetic SQLite (StaticPool,
``create_all``), principal injected via dependency overrides, and the world
service bound to the request session with a fake clock so record times are
deterministic. Ingest injects a fake model client through the
``resolve_world_model_client`` dependency; the 503 test uses the real
resolver with no provider configured.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.services.world_service import (
    get_world_service,
    make_world_service,
    resolve_world_model_client,
)
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent, UserRole
from forge_db.base import Base
from forge_db.models import AuditLog, Workspace
from forge_world import WorldModelService

WS = uuid.UUID("00000000-0000-0000-0000-00000000bb01")
WS2 = uuid.UUID("00000000-0000-0000-0000-00000000bb02")
MEMBER = uuid.UUID("00000000-0000-0000-0000-00000000bb11")
MEMBER2 = uuid.UUID("00000000-0000-0000-0000-00000000bb12")
AGENT = uuid.UUID("00000000-0000-0000-0000-00000000bb13")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def tick(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)  # type: ignore[arg-type]


class FakeModelClient:
    def __init__(self, script: list[str] | None = None) -> None:
        self._script = list(script or [])
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._script:
            return ModelResponse(content=self._script.pop(0))
        return ModelResponse(content='{"statements": []}')

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        yield ModelStreamEvent(type="text", text=self.complete(request).content)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add(Workspace(id=WS, name="Acme WS", slug="acme-ws"))
        session.add(Workspace(id=WS2, name="Other WS", slug="other-ws"))
        session.commit()
    yield sf
    engine.dispose()


def _principal(
    role: UserRole = UserRole.MEMBER,
    *,
    user_id: uuid.UUID = MEMBER,
    workspace_id: uuid.UUID = WS,
) -> Principal:
    return Principal(
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        email="world@test.local",
        auth_method="test",
        scopes=["*"],
    )


def _client(
    factory: sessionmaker[Session],
    clock: FakeClock,
    principal: Principal,
    *,
    model_client: FakeModelClient | None = None,
) -> TestClient:
    app: FastAPI = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    def _service(
        session: Session = Depends(_get_db),  # noqa: B008 - test override seam
    ) -> WorldModelService:
        return make_world_service(session, clock=clock)

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_world_service] = _service
    app.dependency_overrides[get_current_principal] = lambda: principal
    if model_client is not None:
        app.dependency_overrides[resolve_world_model_client] = lambda: model_client
    return TestClient(app)


def _declare_based_in(client: TestClient) -> None:
    resp = client.post(
        "/world/ontology/relations",
        json={
            "key": "based_in",
            "label": "based in",
            "kind": "state",
            "range_class": "value",
            "functional": True,
        },
    )
    assert resp.status_code == 201, resp.text


def _assert_based(client: TestClient, value: str, valid_from: str | None = "2020") -> dict:
    resp = client.post(
        "/world/facts",
        json={
            "subject": "Acme",
            "phrase": "based in",
            "predicate_key": "based_in",
            "value": value,
            "valid_from": valid_from,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- ontology --------------------------------------------------------------- #


def test_ontology_create_list_and_validation(factory, clock) -> None:
    client = _client(factory, clock, _principal())
    resp = client.post("/world/ontology/classes", json={"key": "org", "label": "Organisation"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["key"] == "org"

    resp = client.post("/world/ontology/classes", json={"key": "BadKey", "label": "Nope"})
    assert resp.status_code == 422

    resp = client.post("/world/ontology/classes", json={"key": "org", "label": "Again"})
    assert resp.status_code == 409

    _declare_based_in(client)
    relations = client.get("/world/ontology/relations").json()["items"]
    keys = {item["key"] for item in relations}
    assert {"based_in", "known_as"} <= keys  # system relation included

    classes = client.get("/world/ontology/classes").json()["items"]
    assert [item["key"] for item in classes] == ["org"]


def test_viewer_cannot_write(factory, clock) -> None:
    viewer = _client(factory, clock, _principal(UserRole.VIEWER))
    resp = viewer.post("/world/ontology/classes", json={"key": "org", "label": "Organisation"})
    assert resp.status_code == 403
    resp = viewer.post("/world/facts", json={"subject": "Acme", "phrase": "sells", "value": "x"})
    assert resp.status_code == 403
    assert viewer.get("/world/facts").status_code == 200


# --- facts: assert / dedup / two clocks / supersede / retract --------------- #


def test_assert_dedup_and_bitemporal_reads(factory, clock) -> None:
    client = _client(factory, clock, _principal())
    _declare_based_in(client)

    first = _assert_based(client, "Berlin")
    assert first["created"] is True
    # The world clock alone: before Berlin's 2020 start nothing holds.
    before = client.get(
        "/world/facts", params={"predicate": "based_in", "at": "2019-01-01T00:00:00Z"}
    ).json()["items"]
    assert before == []
    again = _assert_based(client, "Berlin")
    assert again["created"] is False
    assert again["id"] == first["id"]

    clock.tick(days=1)
    resp = client.post(
        f"/world/facts/{first['id']}/supersede",
        json={
            "subject": "Acme",
            "phrase": "based in",
            "predicate_key": "based_in",
            "value": "Munich",
            "valid_from": "2020",
        },
    )
    assert resp.status_code == 201, resp.text

    old = client.get(
        "/world/facts",
        params={
            "predicate": "based_in",
            "at": "2021-01-01T00:00:00Z",
            "as_of": first["recorded_at"],
        },
    ).json()["items"]
    assert [item["value"] for item in old] == ["Berlin"]

    now = client.get(
        "/world/facts",
        params={"predicate": "based_in", "at": "2021-01-01T00:00:00Z"},
    ).json()["items"]
    assert [item["value"] for item in now] == ["Munich"]


def test_assert_unknown_relation_and_retract_conflict(factory, clock) -> None:
    client = _client(factory, clock, _principal())
    resp = client.post(
        "/world/facts",
        json={
            "subject": "Acme",
            "phrase": "haunts",
            "predicate_key": "haunts",
            "value": "Berlin",
        },
    )
    assert resp.status_code == 422

    _declare_based_in(client)
    fact = _assert_based(client, "Berlin")
    resp = client.post(f"/world/facts/{fact['id']}/retract")
    assert resp.status_code == 200, resp.text
    assert resp.json()["invalidated_reason"] == "retracted"
    resp = client.post(f"/world/facts/{fact['id']}/retract")
    assert resp.status_code == 409  # already invalidated


def test_cross_workspace_ids_are_404(factory, clock) -> None:
    owner = _client(factory, clock, _principal())
    _declare_based_in(owner)
    fact = _assert_based(owner, "Berlin")

    foreign = _client(factory, clock, _principal(workspace_id=WS2))
    assert foreign.get(f"/world/entities/{fact['subject_id']}").status_code == 404
    assert foreign.post(f"/world/facts/{fact['id']}/retract").status_code == 404
    assert foreign.get("/world/facts").json()["items"] == []


def test_audit_event_emitted_per_mutation(factory, clock) -> None:
    client = _client(factory, clock, _principal())
    resp = client.post("/world/ontology/classes", json={"key": "person", "label": "Person"})
    assert resp.status_code == 201, resp.text
    resp = client.post("/world/ontology/classes", json={"key": "org", "label": "Organisation"})
    assert resp.status_code == 201, resp.text
    _declare_based_in(client)
    fact = _assert_based(client, "Berlin")
    resp = client.post(
        "/world/ontology/relations",
        json={"key": "works_at", "label": "works at", "kind": "state"},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/world/facts",
        json={
            "subject": "Ada",
            "subject_type": "person",
            "phrase": "works at",
            "predicate_key": "works_at",
            "object": "Globex",
            "object_type": "org",
        },
    )
    assert resp.status_code == 201, resp.text
    works = resp.json()

    with factory() as session:
        actions = list(
            session.scalars(select(AuditLog.action).where(AuditLog.workspace_id == WS)).all()
        )
    assert "world.fact_asserted" in actions
    assert "world.relation_created" in actions
    with factory() as session:
        rows = list(
            session.scalars(select(AuditLog).where(AuditLog.action == "world.fact_asserted"))
        )
        by_target = {str(row.target_id): row for row in rows}
        assert by_target[fact["id"]].target_id == uuid.UUID(fact["id"])
        # Entity creation/typing side effects are audited on the same
        # event, with the object id (minor 14).
        details = by_target[works["id"]].details
        assert details["object_id"] == works["object_id"]
        assert set(details["created_entity_ids"]) == {
            works["subject_id"],
            works["object_id"],
        }
        assert set(details["typed_entity_ids"]) == {works["subject_id"], works["object_id"]}


# --- proposals ---------------------------------------------------------------- #


def _propose_based(client: TestClient, **extra: object) -> dict:
    body: dict[str, object] = {
        "subject": "Acme",
        "phrase": "based in",
        "predicate_key": "based_in",
        "value": "Berlin",
        "valid_from": "2020",
        "rationale": "annual report",
    }
    body.update(extra)
    resp = client.post("/world/proposals", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_agent_runner_proposes_but_cannot_decide(factory, clock) -> None:
    member = _client(factory, clock, _principal())
    _declare_based_in(member)
    agent = _client(factory, clock, _principal(UserRole.AGENT_RUNNER, user_id=AGENT))
    proposal = _propose_based(agent, verdict="accept", gate_confidence=0.9)
    assert proposal["status"] == "proposed"
    assert proposal["gate"]["decision"] == "auto"  # shadow verdict recorded
    assert proposal["proposer_kind"] == "agent"

    # agent-runner lacks WRITE: deciding is 403 at the permission gate.
    resp = agent.post(f"/world/proposals/{proposal['id']}/decision", json={"accept": True})
    assert resp.status_code == 403

    # Isolate the WRITE check from the proposer rule: the member's own
    # proposal must also be refused to the agent, for the WRITE reason,
    # and must remain undecided (M12).
    other = _propose_based(member)
    resp = agent.post(f"/world/proposals/{other['id']}/decision", json={"accept": True})
    assert resp.status_code == 403
    assert "lacks permission 'write'" in resp.json()["detail"]
    listed = member.get("/world/proposals", params={"status": "proposed"}).json()
    assert other["id"] in [item["id"] for item in listed["items"]]

    resp = member.post(f"/world/proposals/{proposal['id']}/decision", json={"accept": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proposal"]["status"] == "accepted"
    assert body["fact"]["proposal_id"] == proposal["id"]
    assert body["fact"]["source_kind"] == "agent"


def test_agent_runner_cannot_claim_person_provenance(factory, clock) -> None:
    member = _client(factory, clock, _principal())
    _declare_based_in(member)
    agent = _client(factory, clock, _principal(UserRole.AGENT_RUNNER, user_id=AGENT))
    proposal = _propose_based(agent, proposer_kind="person")
    assert proposal["proposer_kind"] == "agent"

    resp = member.post(f"/world/proposals/{proposal['id']}/decision", json={"accept": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["fact"]["source_kind"] == "agent"


def test_member_cannot_decide_own_proposal(factory, clock) -> None:
    proposer = _client(factory, clock, _principal())
    _declare_based_in(proposer)
    proposal = _propose_based(proposer)

    resp = proposer.post(f"/world/proposals/{proposal['id']}/decision", json={"accept": True})
    assert resp.status_code == 403  # decider must not be the proposer

    other = _client(factory, clock, _principal(user_id=MEMBER2))
    resp = other.post(f"/world/proposals/{proposal['id']}/decision", json={"accept": False})
    assert resp.status_code == 422  # rejection needs a reason

    resp = other.post(
        f"/world/proposals/{proposal['id']}/decision",
        json={"accept": False, "reason": "stale"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["status"] == "rejected"

    listed = other.get("/world/proposals", params={"status": "rejected"}).json()
    assert [item["id"] for item in listed["items"]] == [proposal["id"]]


# --- merges, derive, conflicts ------------------------------------------------- #


def test_merges_via_api(factory, clock) -> None:
    client = _client(factory, clock, _principal())
    a = client.post(
        "/world/facts", json={"subject": "Acme Inc", "phrase": "sells", "value": "w"}
    ).json()
    b = client.post(
        "/world/facts",
        json={"subject": "Acme Subsidiary", "phrase": "sells", "value": "g"},
    ).json()

    resp = client.post(
        "/world/merges",
        json={"survivor_id": a["subject_id"], "merged_id": a["subject_id"]},
    )
    assert resp.status_code == 409

    resp = client.post(
        "/world/merges",
        json={
            "survivor_id": a["subject_id"],
            "merged_id": b["subject_id"],
            "reason": "same company",
        },
    )
    assert resp.status_code == 201, resp.text
    merge_id = resp.json()["id"]

    profile = client.get(f"/world/entities/{b['subject_id']}").json()
    assert profile["resolved_id"] == a["subject_id"]

    resp = client.post(f"/world/merges/{merge_id}/revert", json={"reason": "oops"})
    assert resp.status_code == 200, resp.text
    profile = client.get(f"/world/entities/{b['subject_id']}").json()
    assert profile["resolved_id"] == b["subject_id"]


def test_derive_and_conflicts_endpoints(factory, clock) -> None:
    client = _client(factory, clock, _principal())
    resp = client.post(
        "/world/ontology/relations",
        json={"key": "part_of", "label": "part of", "kind": "state", "transitive": True},
    )
    assert resp.status_code == 201, resp.text
    for subject, target in (("Alpha", "Beta"), ("Beta", "Gamma")):
        resp = client.post(
            "/world/facts",
            json={
                "subject": subject,
                "phrase": "part of",
                "predicate_key": "part_of",
                "object": target,
            },
        )
        assert resp.status_code == 201, resp.text

    first = client.post("/world/derive").json()
    assert first["created"] == 1  # Alpha part_of Gamma
    second = client.post("/world/derive").json()
    assert second["created"] == 0 and second["invalidated"] == 0

    # Two overlapping explicit Berlin/Munich values conflict.
    _declare_based_in(client)
    client.post(
        "/world/facts",
        json={
            "subject": "Acme",
            "phrase": "based in",
            "predicate_key": "based_in",
            "value": "Berlin",
            "valid_from": "2020",
            "valid_to": "2025",
        },
    )
    client.post(
        "/world/facts",
        json={
            "subject": "Acme",
            "phrase": "based in",
            "predicate_key": "based_in",
            "value": "Munich",
            "valid_from": "2023",
        },
    )
    conflicts = client.get("/world/conflicts", params={"at": "2024-01-01T00:00:00Z"}).json()[
        "items"
    ]
    assert any(item["kind"] == "functional_overlap" for item in conflicts)


# --- decisions ------------------------------------------------------------------- #


def test_decisions_and_drift_via_api(factory, clock) -> None:
    client = _client(factory, clock, _principal())

    def _sell(subject: str, value: str) -> dict:
        resp = client.post(
            "/world/facts",
            json={"subject": subject, "phrase": "sells", "value": value, "valid_from": "2020"},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    fact_a = _sell("Acme", "widgets")
    fact_b = _sell("Globex", "gadgets")

    resp = client.post(
        "/world/decisions",
        json={
            "title": "Supplier pick",
            "outcome": "both approved",
            "fact_ids": [fact_a["id"], fact_b["id"]],
            "links": {"spec_key": "SPEC-1"},
        },
    )
    assert resp.status_code == 201, resp.text
    decision = resp.json()

    drift = client.get(f"/world/decisions/{decision['id']}/drift").json()
    assert drift["drifted"] is False

    resp = client.post(
        f"/world/facts/{fact_a['id']}/supersede",
        json={
            "subject": "Acme",
            "phrase": "sells",
            "value": "sprockets",
            "valid_from": "2020",
        },
    )
    assert resp.status_code == 201, resp.text
    successor_id = resp.json()["id"]

    drift = client.get(f"/world/decisions/{decision['id']}/drift").json()
    assert drift["drifted"] is True
    item = [entry for entry in drift["items"] if entry["status"] == "superseded"]
    assert len(item) == 1
    assert item[0]["superseded_by"] == successor_id

    foreign = _client(factory, clock, _principal(workspace_id=WS2))
    assert foreign.get(f"/world/decisions/{decision['id']}").status_code == 404


# --- entities: search and changes ----------------------------------------------- #


def test_entity_search_and_changes(factory, clock) -> None:
    client = _client(factory, clock, _principal())
    _declare_based_in(client)
    fact = _assert_based(client, "Berlin")

    found = client.get("/world/entities", params={"q": "acm"}).json()["items"]
    assert len(found) == 1
    entity_id = found[0]["id"]

    clock.tick(days=1)
    resp = client.post(f"/world/facts/{fact['id']}/retract")
    assert resp.status_code == 200

    changes = client.get(f"/world/entities/{entity_id}/changes").json()["items"]
    kinds = [
        (entry["kind"], entry["reason"]) for entry in changes if entry["fact_id"] == fact["id"]
    ]
    assert kinds == [("asserted", None), ("invalidated", "retracted")]


# --- ingest ----------------------------------------------------------------------- #


def test_ingest_503_without_model(factory, clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    client = _client(factory, clock, _principal())  # real resolver, no provider
    resp = client.post(
        "/world/ingest",
        json={"source_ref": "doc.md", "title": "Doc", "text": "Acme exists."},
    )
    assert resp.status_code == 503
    assert "provider" in resp.json()["detail"]


def test_ingest_with_fake_model(factory, clock) -> None:
    text = "Acme moved to Berlin in 2020."
    reply = json.dumps(
        {
            "statements": [
                {
                    "quote": "Acme moved to Berlin in 2020",
                    "subject": "Acme",
                    "subject_kind": "named",
                    "phrase": "moved to",
                    "object": None,
                    "value": "Berlin",
                    "object_kind": "value",
                    "time": {"from": "2020", "to": None, "words": "in 2020"},
                    "mood": None,
                    "qualifiers": {},
                },
                {
                    "quote": "Acme moved to London",
                    "subject": "Acme",
                    "subject_kind": "named",
                    "phrase": "moved to",
                    "object": None,
                    "value": "London",
                    "object_kind": "value",
                    "time": None,
                    "mood": None,
                    "qualifiers": {},
                },
            ]
        }
    )
    fake = FakeModelClient([reply])
    client = _client(factory, clock, _principal(), model_client=fake)
    resp = client.post("/world/ingest", json={"source_ref": "doc.md", "title": "Doc", "text": text})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["facts_created"] == 1
    assert body["drops_by_reason"] == {"quote_not_in_chunk": 1}
    assert fake.requests[0].temperature == 0.0

    # Ingest runs on the real clock (production path); read with a wide as_of.
    facts = client.get("/world/facts", params={"as_of": "2999-01-01T00:00:00Z"}).json()["items"]
    moved = [item for item in facts if item["phrase"] == "moved to"]
    assert len(moved) == 1
    assert moved[0]["evidence"][0]["quote"] == "Acme moved to Berlin in 2020"
