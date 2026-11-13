"""Spec L seam 4: approvals pushed on ``/ws`` and answered from another surface.

Acceptance 3 runs end to end with real authentication: a member mints a
personal token, a ``/ws`` client authenticated with it hears the gate open,
decides it, hears the decision with its surface, and the audit log records it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import forge_api.auth.service as auth_service_module
import forge_api.db
from forge_api.auth.service import AuthService
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.observability.redaction import redact_mapping
from forge_api.observability.service import ObservabilityService, get_observability_service
from forge_api.realtime.broadcaster import get_broadcaster
from forge_api.realtime.manager import ConnectionManager, get_connection_manager
from forge_api.routers.realtime import get_api_key_socket_lifetime
from forge_api.services.approval_service import build_gate_registry, get_approval_service
from forge_approval import (
    ENFORCED_GATES,
    ApprovalAuthorizer,
    ApprovalService,
    GateType,
    InMemoryApprovalRepository,
)
from forge_approval.providers import InMemoryGrantStore
from forge_auth.tokens import encode_session_jwt
from forge_contracts import RealtimeEvent, RealtimeEventType
from forge_contracts.auth import SessionClaims
from forge_contracts.enums import UserRole
from forge_db.models import User, Workspace

AUTH_SECRET = "approval-push-test-secret"
WS = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000d3")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


def session_jwt(user_id: uuid.UUID, role: UserRole) -> str:
    now = int(time.time())
    claims = SessionClaims(
        sub=user_id, wsid=WS, role=role, email="p@acme.test", exp=now + 3600, iat=now
    )
    return encode_session_jwt(claims, secret=AUTH_SECRET)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Stack:
    client: TestClient
    auth: AuthService
    manager: ConnectionManager
    app: Any


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch, workflow_db: Any) -> Iterator[Stack]:
    with workflow_db.factory() as s:  # the people keys decide as
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        for uid, role in ((MEMBER, UserRole.MEMBER), (ADMIN, UserRole.ADMIN)):
            s.add(User(id=uid, workspace_id=WS, email=f"{uid}@acme.test", role=role))
        s.commit()
    auth = AuthService(secret_key=b"9" * 32, auth_secret=AUTH_SECRET)
    monkeypatch.setattr(auth_service_module, "_default_service", auth)
    observability = ObservabilityService()
    approvals = ApprovalService(
        InMemoryApprovalRepository(),
        build_gate_registry(InMemoryGrantStore()),
        ApprovalAuthorizer(),
        audit=observability.audit,
        redactor=redact_mapping,
    )
    manager = ConnectionManager()
    app = workflow_db.install(create_app())
    app.dependency_overrides[get_approval_service] = lambda: approvals
    app.dependency_overrides[get_observability_service] = lambda: observability
    app.dependency_overrides[get_connection_manager] = lambda: manager
    with TestClient(app) as client:
        yield Stack(client, auth, manager, app)


def open_gate(client: TestClient, token: str, **body: Any) -> str:
    resp = client.post(
        "/approvals",
        headers=bearer(token),
        json={
            "gate_type": "pr",
            "subject_type": "changeset",
            "subject_id": str(uuid.uuid4()),
            "requested_actor": f"agent:{uuid.uuid4()}",
            "title": "ship the fix",
            "gate_payload": {"diff": "secret-ish detail"},
            **body,
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def decide(client: TestClient, token: str, approval_id: str, decision: str = "approve") -> Any:
    return client.post(
        f"/approvals/{approval_id}/decision", headers=bearer(token), json={"decision": decision}
    )


def wait_connected(manager: ConnectionManager, count: int = 1) -> None:
    deadline = time.monotonic() + 5
    while manager.connection_count(WS) != count:
        assert time.monotonic() < deadline, f"never reached {count} socket(s)"
        time.sleep(0.01)


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


# --------------------------------------------------------------------------- #
# Acceptance 3                                                                  #
# --------------------------------------------------------------------------- #


def test_an_approval_answered_from_tervin_resolves_with_tervin_as_the_surface(
    stack: Stack,
) -> None:
    client = stack.client
    minted = client.post(
        "/auth/personal-tokens",
        headers=bearer(session_jwt(MEMBER, UserRole.MEMBER)),
        json={"name": "tervin on laptop", "surface": "tervin", "decide": True},
    )
    assert minted.status_code == 201, minted.text
    token, key_id = minted.json()["token"], minted.json()["id"]

    with client.websocket_connect("/ws", headers=bearer(token)) as ws:
        wait_connected(stack.manager)
        approval_id = open_gate(client, session_jwt(ADMIN, UserRole.ADMIN))

        requested = ws.receive_json()
        assert (requested["type"], requested["approval_id"]) == ("approval.requested", approval_id)
        assert requested["payload"]["title"] == "ship the fix"
        assert "gate_payload" not in requested["payload"]

        resp = decide(client, token, approval_id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"

        decided = ws.receive_json()
    assert (decided["type"], decided["approval_id"]) == ("approval.decided", approval_id)
    assert decided["payload"]["status"] == "approved"
    assert decided["payload"]["decided_by"] == f"user:{MEMBER}"
    assert decided["payload"]["surface"] == "tervin"
    assert (decided["observed_by"], decided["enforceable"]) == ("forge", True)
    assert RealtimeEvent.model_validate({**decided, "workspace_id": str(WS)}).recorded_at

    audit = client.get(
        "/observability/audit",
        headers=bearer(session_jwt(ADMIN, UserRole.ADMIN)),
        params={"category": "approval"},
    )
    assert audit.status_code == 200, audit.text
    (decision,) = [e for e in audit.json() if e["status"] == "approved"]
    assert decision["workspace_id"] == str(WS)
    assert decision["actor"] == f"user:{MEMBER}"
    assert decision["metadata"] == {
        "request_id": approval_id,
        "surface": "tervin",
        "key_id": key_id,
    }
    # The gate's opening is in the workspace's audit too (it carried no workspace before).
    assert [e["status"] for e in audit.json()] == ["requested", "approved"]


@pytest.mark.parametrize(
    ("credential", "surface"),
    [
        ("session", "web"),
        ("unscoped key", "api"),
        ("personal token", "tervin"),
    ],
)
def test_the_decided_event_names_the_credentials_surface(
    stack: Stack, credential: str, surface: str
) -> None:
    session = session_jwt(MEMBER, UserRole.MEMBER)
    token = {
        "session": lambda: session,
        "unscoped key": lambda: stack.auth.bootstrap_key(
            workspace_id=WS, name="web", role=UserRole.MEMBER, user_id=MEMBER
        )[1],
        "personal token": lambda: stack.client.post(
            "/auth/personal-tokens",
            headers=bearer(session),
            json={"name": "t", "surface": "tervin", "decide": True},
        ).json()["token"],
    }[credential]()
    socket = RecordingSocket()
    stack.client.portal.call(stack.manager.connect, WS, socket)  # type: ignore[arg-type]
    approval_id = open_gate(stack.client, session_jwt(ADMIN, UserRole.ADMIN))

    assert decide(stack.client, token, approval_id).status_code == 200

    (decided,) = [e for e in socket.sent if e["type"] == "approval.decided"]
    assert decided["payload"]["surface"] == surface
    assert decided["payload"]["decided_by"] == f"user:{MEMBER}"


def test_a_session_decision_records_the_web_surface(stack: Stack) -> None:
    approval_id = open_gate(stack.client, session_jwt(ADMIN, UserRole.ADMIN))
    assert decide(stack.client, session_jwt(MEMBER, UserRole.MEMBER), approval_id).is_success

    audit = stack.client.get(
        "/observability/audit", headers=bearer(session_jwt(ADMIN, UserRole.ADMIN))
    ).json()
    (decision,) = [e for e in audit if e["status"] == "approved"]
    assert decision["metadata"]["surface"] == "web"
    assert decision["metadata"]["key_id"] is None


def test_a_surface_header_is_ignored(stack: Stack) -> None:
    approval_id = open_gate(stack.client, session_jwt(ADMIN, UserRole.ADMIN))
    headers = {**bearer(session_jwt(MEMBER, UserRole.MEMBER)), "X-Forge-Surface": "tervin"}
    resp = stack.client.post(
        f"/approvals/{approval_id}/decision", headers=headers, json={"decision": "approve"}
    )
    assert resp.status_code == 200
    audit = stack.client.get(
        "/observability/audit", headers=bearer(session_jwt(ADMIN, UserRole.ADMIN))
    ).json()
    assert [e["metadata"]["surface"] for e in audit if e["status"] == "approved"] == ["web"]


# --------------------------------------------------------------------------- #
# approval.updated                                                              #
# --------------------------------------------------------------------------- #


def test_partial_approval_and_escalation_are_updates_not_decisions(stack: Stack) -> None:
    client = stack.client
    socket = RecordingSocket()
    client.portal.call(stack.manager.connect, WS, socket)  # type: ignore[arg-type]
    admin = session_jwt(ADMIN, UserRole.ADMIN)

    quorum = open_gate(client, admin, required_approvals=2)
    assert decide(client, session_jwt(MEMBER, UserRole.MEMBER), quorum).json()["status"] == (
        "pending"
    )
    incident = open_gate(client, admin, gate_type="incident_remediation")
    escalated = decide(client, session_jwt(MEMBER, UserRole.MEMBER), incident, "escalate")
    assert escalated.json()["status"] == "pending"
    assert decide(client, session_jwt(OTHER, UserRole.MEMBER), quorum).json()["status"] == (
        "approved"
    )

    events = [(e["type"], e["approval_id"], e["payload"]) for e in socket.sent]
    kinds = [(kind, aid, p.get("reason")) for kind, aid, p in events]
    assert kinds == [
        ("approval.requested", quorum, None),
        ("approval.updated", quorum, "partially_approved"),
        ("approval.requested", incident, None),
        ("approval.updated", incident, "escalated"),
        ("approval.decided", quorum, None),
    ]
    assert events[1][2]["status"] == "pending"
    assert events[3][2]["risk_level"] == "critical"
    assert socket.sent[3]["enforceable"] is False  # incident remediation only records


def test_enforced_gates_are_the_gates_whose_resolution_hook_acts() -> None:
    registry = build_gate_registry(InMemoryGrantStore())
    assert {gate for gate in GateType if registry.hook(gate) is not None} == ENFORCED_GATES


# --------------------------------------------------------------------------- #
# V1 engine gate open                                                           #
# --------------------------------------------------------------------------- #


class RecordingBroadcaster:
    def __init__(self) -> None:
        self.events: list[RealtimeEvent] = []

    async def publish(self, topic: str, event: RealtimeEvent) -> None:
        self.events.append(event)

    def subscribe(self, topic: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def test_the_v1_engine_announces_the_gate_it_opens(
    workflow_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forge_api.db, "get_session_factory", lambda: workflow_db.factory)
    approvals = ApprovalService(
        InMemoryApprovalRepository(),
        build_gate_registry(InMemoryGrantStore()),
        ApprovalAuthorizer(),
        redactor=redact_mapping,
    )
    broadcaster = RecordingBroadcaster()
    principal = Principal(user_id=MEMBER, workspace_id=WS, role=UserRole.MEMBER, auth_method="test")
    app = workflow_db.install(create_app())
    app.dependency_overrides[get_approval_service] = lambda: approvals
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_broadcaster] = lambda: broadcaster
    client = TestClient(app)
    task_id = workflow_db.seed_task(WS)
    run_id = client.post("/workflow/runs", json={"task_id": str(task_id)}).json()["id"]

    for event in ("generate_spec_draft", "gather_clarifications", "submit_spec_for_review"):
        resp = client.post(f"/workflow/runs/{run_id}/transition", json={"event": event})
        assert resp.status_code == 200, resp.text

    (event,) = broadcaster.events
    assert (event.type, event.workspace_id) == (RealtimeEventType.APPROVAL_REQUESTED, WS)
    assert event.payload["gate"] == "spec"
    assert event.payload["workflow_run_id"] == run_id
    assert event.payload["title"] == "workflow task"
    assert event.enforceable is True


# --------------------------------------------------------------------------- #
# /ws for tokens                                                                #
# --------------------------------------------------------------------------- #


def test_a_header_key_socket_closes_after_its_lifetime_and_a_revoked_key_cannot_reconnect(
    stack: Stack,
) -> None:
    stack.app.dependency_overrides[get_api_key_socket_lifetime] = lambda: 0.2
    info, key = stack.auth.bootstrap_key(workspace_id=WS, name="tervin", role=UserRole.MEMBER)

    with stack.client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
        wait_connected(stack.manager)
        wait_connected(stack.manager, 0)  # the server closed it; fails rather than hangs
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1000

    assert stack.auth.api_keys.revoke(WS, info.id)
    with (
        stack.client.websocket_connect("/ws", headers=bearer(key)) as ws,
        pytest.raises(WebSocketDisconnect) as refused,
    ):
        ws.receive_json()
    assert refused.value.code == 1008


def test_a_session_socket_has_no_lifetime(stack: Stack) -> None:
    stack.app.dependency_overrides[get_api_key_socket_lifetime] = lambda: 0.05
    token = session_jwt(MEMBER, UserRole.MEMBER)

    with stack.client.websocket_connect(f"/ws?token={token}") as ws:
        wait_connected(stack.manager)
        time.sleep(0.3)
        ws.portal.call(stack.manager.broadcast, WS, {"type": "task.updated"})
        assert ws.receive_json() == {"type": "task.updated"}
    # A session token sent in a header is still a session: no lifetime either.
    with stack.client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        wait_connected(stack.manager)
        time.sleep(0.3)
        ws.portal.call(stack.manager.broadcast, WS, {"type": "task.updated"})
        assert ws.receive_json() == {"type": "task.updated"}


def _closes_within(stack: Stack, url: str, headers: dict[str, str] | None = None) -> int:
    """Open a socket, wait for the server to close it, return the close code."""
    with stack.client.websocket_connect(url, headers=headers or {}) as ws:
        wait_connected(stack.manager)
        wait_connected(stack.manager, 0)  # fails rather than hangs if never closed
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    return closed.value.code


def test_the_webs_query_token_socket_with_an_unscoped_key_has_no_lifetime(stack: Stack) -> None:
    stack.app.dependency_overrides[get_api_key_socket_lifetime] = lambda: 0.05
    _, key = stack.auth.bootstrap_key(
        workspace_id=WS, name="web", role=UserRole.MEMBER, user_id=MEMBER
    )

    with stack.client.websocket_connect(f"/ws?token={key}") as ws:
        wait_connected(stack.manager)
        time.sleep(0.3)
        ws.portal.call(stack.manager.broadcast, WS, {"type": "task.updated"})
        assert ws.receive_json() == {"type": "task.updated"}


def test_a_narrowed_key_socket_has_a_lifetime_even_on_the_query_string(stack: Stack) -> None:
    stack.app.dependency_overrides[get_api_key_socket_lifetime] = lambda: 0.2
    token = stack.client.post(
        "/auth/personal-tokens",
        headers=bearer(session_jwt(MEMBER, UserRole.MEMBER)),
        json={"name": "t", "surface": "tervin", "decide": False},
    ).json()["token"]

    assert _closes_within(stack, f"/ws?token={token}") == 1000


def test_a_socket_lifetime_never_outlives_the_key(stack: Stack) -> None:
    # The configured lifetime is long; the key expires first.
    _, key = stack.auth.bootstrap_key(
        workspace_id=WS,
        name="short",
        role=UserRole.MEMBER,
        expires_at=datetime.now(UTC) + timedelta(seconds=0.5),
    )

    assert _closes_within(stack, "/ws", headers=bearer(key)) == 1000
