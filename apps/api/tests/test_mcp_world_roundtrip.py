"""The world model over forge's own MCP server, driven by forge's own MCP client (spec I §5, §8)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.routers.mcp_server import get_mcp_server_audit_log
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import Workspace, WorldFact, WorldProposal
from forge_mcp.transports.http import HttpMcpTransport

WS = uuid.UUID("00000000-0000-0000-0000-00000000cc01")
WS2 = uuid.UUID("00000000-0000-0000-0000-00000000cc02")
SENTINEL = "zz-sentinel-argument-zz"
WORLD_TOOLS = {
    "world_search",
    "world_entity",
    "world_facts",
    "world_changes",
    "world_conflicts",
    "world_decision_drift",
    "world_propose",
}


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add_all(
            [Workspace(id=WS, name="Acme", slug="acme"), Workspace(id=WS2, name="Other", slug="o")]
        )
        session.commit()
    yield sf
    engine.dispose()


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


def _client(
    factory: sessionmaker[Session], audit: AuditLog, role: UserRole, ws: uuid.UUID = WS
) -> TestClient:
    app: FastAPI = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    principal = Principal(
        user_id=uuid.uuid4(), workspace_id=ws, role=role, auth_method="test", scopes=["*"]
    )
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_mcp_server_audit_log] = lambda: audit
    return TestClient(app)


def _seed(client: TestClient) -> dict:
    resp = client.post(
        "/world/ontology/relations",
        json={"key": "based_in", "label": "based in", "kind": "state", "range_class": "value"},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/world/facts",
        json={
            "subject": "Acme",
            "phrase": "based in",
            "predicate_key": "based_in",
            "value": "Berlin",
            "valid_from": "2020",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _mcp(client: TestClient) -> HttpMcpTransport:
    return HttpMcpTransport("http://testserver/mcp", client=client)


def _structured(result: dict) -> dict:
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


def test_member_sees_all_seven_world_tools(factory, audit) -> None:
    with _mcp(_client(factory, audit, UserRole.MEMBER)) as mcp:
        names = {tool.name for tool in mcp.list_tools()}
    assert names >= WORLD_TOOLS


def test_viewer_cannot_see_or_call_world_propose(factory, audit) -> None:
    client = _client(factory, audit, UserRole.VIEWER)
    with _mcp(client) as mcp:
        names = {tool.name for tool in mcp.list_tools()}
        assert names >= WORLD_TOOLS - {"world_propose"}
        assert "world_propose" not in names
    reply = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "world_propose",
                "arguments": {"subject": "Acme", "phrase": "is", "value": "x"},
            },
        },
    ).json()
    assert reply["error"]["code"] == -32003
    with factory() as session:
        assert session.scalars(select(WorldProposal)).all() == []


def test_world_facts_returns_the_seeded_fact_with_evidence_fields(factory, audit) -> None:
    client = _client(factory, audit, UserRole.MEMBER)
    fact = _seed(client)
    with _mcp(client) as mcp:
        result = mcp.call_tool("world_facts", {"predicate": "based_in"})
        facts = _structured(result)["items"]
        assert [f["value"] for f in facts] == ["Berlin"]
        assert facts[0]["id"] == fact["id"]

        found = _structured(mcp.call_tool("world_search", {"query": "Acme"}))["items"]
        assert found and found[0]["id"] == fact["subject_id"]

        profile = _structured(mcp.call_tool("world_entity", {"entity_id": fact["subject_id"]}))
        assert [f["value"] for f in profile["facts"] if f["predicate_key"] == "based_in"] == [
            "Berlin"
        ]

        past = mcp.call_tool("world_facts", {"predicate": "based_in", "at": "2019-01-01T00:00:00Z"})
        assert _structured(past)["items"] == []


def test_foreign_workspace_ids_look_nonexistent(factory, audit) -> None:
    fact = _seed(_client(factory, audit, UserRole.MEMBER))
    other = _client(factory, audit, UserRole.MEMBER, ws=WS2)
    with _mcp(other) as mcp:
        result = mcp.call_tool("world_entity", {"entity_id": fact["subject_id"]})
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == f"no entity {fact['subject_id']}"


def test_world_propose_creates_a_proposal_marked_mcp_and_no_fact(factory, audit) -> None:
    client = _client(factory, audit, UserRole.MEMBER)
    _seed(client)
    with _mcp(client) as mcp:
        result = mcp.call_tool(
            "world_propose",
            {
                "subject": "Acme",
                "phrase": "based in",
                "predicate_key": "based_in",
                "value": "Paris",
                "valid_from": "2024",
                "rationale": "press release",
            },
        )
    proposal = _structured(result)
    assert proposal["status"] == "proposed"
    assert proposal["proposer_kind"] == "mcp"
    with factory() as session:
        values = [f.value for f in session.scalars(select(WorldFact)) if f.predicate_key]
        assert "Paris" not in values


def test_every_call_is_audited_once_without_its_arguments(factory, audit) -> None:
    client = _client(factory, audit, UserRole.MEMBER)
    with _mcp(client) as mcp:
        mcp.call_tool("world_search", {"query": SENTINEL})
        mcp.call_tool("world_conflicts", {})
    entries = audit.query(category=AuditCategory.MCP_CALL)
    assert [e.target for e in entries] == ["world_search", "world_conflicts"]
    assert all(e.payload_hash and len(e.payload_hash) == 64 for e in entries)
    assert SENTINEL not in json.dumps([e.model_dump(mode="json") for e in entries])


def test_decision_drift_over_mcp(factory, audit) -> None:
    client = _client(factory, audit, UserRole.MEMBER)
    fact = _seed(client)
    decision = client.post(
        "/world/decisions",
        json={
            "title": "Open the Berlin office",
            "outcome": "approved",
            "fact_ids": [fact["id"]],
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert decision.status_code == 201, decision.text
    resp = client.post(
        f"/world/facts/{fact['id']}/supersede",
        json={
            "subject": "Acme",
            "phrase": "based in",
            "predicate_key": "based_in",
            "value": "Munich",
            "valid_from": "2020",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    with _mcp(client) as mcp:
        drift = _structured(
            mcp.call_tool("world_decision_drift", {"decision_id": decision.json()["id"]})
        )
    assert drift["drifted"] is True
    assert drift["items"][0]["status"] == "superseded"
