"""F39 audit API integration tests (hermetic SQLite + real router/service).

AC11 (filters + keyset pagination), AC12 (admin-only + cross-workspace 404),
AC13 (no write endpoints), AC14 (verify endpoint), AC15 (NDJSON export with
chain hashes, offline re-verifiable, ``audit.exported`` recorded, no source row
mutated), AC18 (``detail_ref`` drill-down present).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.services.audit import SqlAuditWriter
from forge_contracts.audit import AuditEvent, compute_entry_hash, compute_payload_hash
from forge_contracts.enums import UserRole
from forge_db.base import Base
from forge_db.models import AuditLog, Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


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
        writer = SqlAuditWriter(s)
        for i in range(6):
            writer.emit(
                AuditEvent(
                    workspace_id=WS,
                    action="tool.call" if i % 2 == 0 else "approval.decided",
                    actor_type="agent_runner" if i % 2 == 0 else "user",
                    actor_id=None if i % 2 == 0 else ADMIN,
                    actor_label=f"actor-{i}",
                    result="success" if i < 5 else "denied",
                    severity="critical" if i == 5 else "info",
                    details={"i": i, "api_key": "sk-ant-superSecretValue123456"},
                    detail_ref={"table": "agent_steps", "id": str(uuid.uuid4())},
                    created_at=NOW + timedelta(minutes=i),
                )
            )
        writer.emit(AuditEvent(workspace_id=WS2, action="tool.call"))
        s.commit()
    yield sf
    engine.dispose()


def _principal(role: UserRole = UserRole.ADMIN, ws: uuid.UUID = WS) -> Principal:
    return Principal(
        user_id=ADMIN,
        workspace_id=ws,
        role=role,
        email="admin@acme.test",
        auth_method="test",
        scopes=["*"],
    )


def _client(factory: sessionmaker[Session], principal: Principal) -> TestClient:
    app: FastAPI = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def test_list_filters_and_keyset_pagination(factory) -> None:
    client = _client(factory, _principal())

    body = client.get("/audit").json()
    assert len(body["items"]) == 6
    assert body["items"][0]["seq"] == 6  # newest first
    assert body["items"][0]["detail_ref"]["table"] == "agent_steps"  # AC18

    assert len(client.get("/audit", params={"action": ["tool.call"]}).json()["items"]) == 3
    assert len(client.get("/audit", params={"actor_type": "user"}).json()["items"]) == 3
    assert len(client.get("/audit", params={"actor_id": str(ADMIN)}).json()["items"]) == 3
    assert len(client.get("/audit", params={"outcome": "denied"}).json()["items"]) == 1
    assert len(client.get("/audit", params={"severity": "critical"}).json()["items"]) == 1
    window = client.get(
        "/audit",
        params={
            "from": (NOW + timedelta(minutes=1)).isoformat(),
            "to": (NOW + timedelta(minutes=2)).isoformat(),
        },
    ).json()["items"]
    assert [e["seq"] for e in window] == [3, 2]

    # Keyset pagination: gapless, duplicate-free, terminates with null cursor.
    seen: list[int] = []
    cursor = None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = client.get("/audit", params=params).json()
        seen.extend(e["seq"] for e in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [6, 5, 4, 3, 2, 1]


def test_secrets_are_redacted_in_api_responses(factory) -> None:
    client = _client(factory, _principal())
    text = client.get("/audit").text
    assert "superSecretValue" not in text  # AC4 via the F37 SecretRedactor
    assert "[REDACTED]" in text


def test_rbac_admin_only(factory) -> None:
    for role in (UserRole.MEMBER, UserRole.VIEWER, UserRole.AGENT_RUNNER):
        client = _client(factory, _principal(role=role))
        assert client.get("/audit").status_code == 403
        assert client.post("/audit/verify", json={}).status_code == 403
        assert client.get("/audit/export").status_code == 403


def test_cross_workspace_entry_is_404(factory) -> None:
    admin_ws1 = _client(factory, _principal())
    entry_id = admin_ws1.get("/audit").json()["items"][0]["id"]
    assert admin_ws1.get(f"/audit/{entry_id}").status_code == 200

    admin_ws2 = _client(factory, _principal(ws=WS2))
    assert admin_ws2.get(f"/audit/{entry_id}").status_code == 404  # no leak
    assert len(admin_ws2.get("/audit").json()["items"]) == 1  # isolation


def test_no_write_endpoints_in_openapi(factory) -> None:
    client = _client(factory, _principal())
    paths = client.get("/openapi.json").json()["paths"]
    audit_paths = {p: spec for p, spec in paths.items() if p.startswith("/audit")}
    assert set(audit_paths) == {
        "/audit",
        "/audit/actions",
        "/audit/export",
        "/audit/verify",
        "/audit/{entry_id}",
    }
    for path, spec in audit_paths.items():
        methods = set(spec)
        assert "put" not in methods and "delete" not in methods and "patch" not in methods
        if path != "/audit/verify":
            assert "post" not in methods


def test_verify_endpoint_clean_and_tampered(factory) -> None:
    client = _client(factory, _principal())
    ok = client.post("/audit/verify", json={}).json()
    assert ok["ok"] is True
    assert ok["entries_checked"] == 6

    with factory() as s:
        s.execute(
            update(AuditLog.__table__)
            .where(AuditLog.__table__.c.seq == 4, AuditLog.__table__.c.workspace_id == WS)
            .values(details={"i": "tampered"})
        )
        s.commit()
    broken = client.post("/audit/verify", json={}).json()
    assert broken["ok"] is False
    assert broken["broken_at_seq"] == 4

    ranged = client.post("/audit/verify", json={"from_seq": 1, "to_seq": 3}).json()
    assert ranged["ok"] is True


def test_actions_vocabulary(factory) -> None:
    client = _client(factory, _principal())
    vocab = client.get("/audit/actions").json()
    assert "approval.decided" in vocab["actions"]
    assert "agent_runner" in vocab["actor_types"]
    assert "blocked" in vocab["outcomes"]
    assert "critical" in vocab["severities"]


def test_export_streams_ndjson_with_hashes_and_records_event(factory) -> None:
    client = _client(factory, _principal())
    response = client.get("/audit/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    lines = [json.loads(line) for line in response.text.strip().splitlines()]
    assert [e["seq"] for e in lines] == [1, 2, 3, 4, 5, 6]
    # Offline re-verification from the export alone (AC15/AC3).
    prev = "0" * 64
    for entry in lines:
        payload = compute_payload_hash(
            {"before": entry["before"], "after": entry["after"], "details": entry["details"]}
        )
        assert entry["payload_hash"] == payload
        assert entry["prev_hash"] == prev
        recomputed = compute_entry_hash(
            prev_hash=prev,
            workspace_id=uuid.UUID(entry["workspace_id"]),
            seq=entry["seq"],
            occurred_at=datetime.fromisoformat(entry["created_at"]),
            actor_type=entry["actor_type"],
            actor_id=uuid.UUID(entry["actor_id"]) if entry["actor_id"] else None,
            actor_label=entry["actor_label"],
            action=entry["action"],
            target_type=entry["target_type"],
            target_id=uuid.UUID(entry["target_id"]) if entry["target_id"] else None,
            scope_type=entry["scope_type"],
            scope_id=uuid.UUID(entry["scope_id"]) if entry["scope_id"] else None,
            result=entry["result"],
            payload_hash=payload,
        )
        assert entry["entry_hash"] == recomputed
        prev = entry["entry_hash"]

    # The export recorded audit.exported (seq 7) and deleted/mutated nothing.
    with factory() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.workspace_id == WS)).all()
        assert len(rows) == 7
        actions = {r.seq: r.action for r in rows}
        assert actions[7] == "audit.exported"
