"""Slice RT-7 — run-trace + approvals live push.

Exercises the two remaining realtime producers end to end (write -> emit ->
in-process broadcaster -> ``/ws`` room), mirroring ``test_realtime_publish.py``'s
``_RecordingSocket`` pattern:

* ``POST /observability/runs/{id}/trace`` (the run-trace producer) fans a
  ``run.*`` event out, chosen from the recorded ``status``.
* ``POST /approvals`` / ``POST /approvals/{id}/decision`` (the approval
  producer) fan ``approval.requested`` / ``approval.decided`` out.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.observability.redaction import redact_mapping
from forge_api.observability.service import ObservabilityService, get_observability_service
from forge_api.realtime.manager import ConnectionManager, get_connection_manager
from forge_api.services.approval_service import build_gate_registry, get_approval_service
from forge_approval import (
    ApprovalAuthorizer,
    ApprovalService,
    InMemoryActivityBus,
    InMemoryApprovalRepository,
)
from forge_approval.providers import InMemoryGrantStore
from forge_contracts import RealtimeEventType

# Matches the workspace the ``authenticate_app`` fixture's admin principal
# carries (``tests/conftest.py``), so a socket registered here shares the
# caller's room.
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


class _RecordingSocket:
    """WebSocket stand-in that records the JSON envelopes broadcast to it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


# --------------------------------------------------------------------------- #
# run.* — POST /observability/runs/{run_id}/trace                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def obs_client(
    authenticate_app: Callable[..., FastAPI],
) -> tuple[TestClient, ConnectionManager]:
    app = create_app()
    authenticate_app(app)
    service = ObservabilityService()
    manager = ConnectionManager()
    app.dependency_overrides[get_observability_service] = lambda: service
    app.dependency_overrides[get_connection_manager] = lambda: manager
    return TestClient(app), manager


def test_run_trace_record_emits_started_event(
    obs_client: tuple[TestClient, ConnectionManager],
) -> None:
    client, manager = obs_client
    socket = _RecordingSocket()
    run_id = uuid.uuid4()

    with client:
        client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]
        resp = client.post(
            f"/observability/runs/{run_id}/trace",
            json={"steps": [], "status": "running"},
        )
        assert resp.status_code == 201, resp.text

    assert [e["type"] for e in socket.sent] == [RealtimeEventType.RUN_STARTED.value]
    assert socket.sent[0]["run_id"] == str(run_id)
    assert socket.sent[0]["payload"] == {"status": "running"}
    assert "workspace_id" not in socket.sent[0]


def test_run_trace_record_emits_completed_event(
    obs_client: tuple[TestClient, ConnectionManager],
) -> None:
    client, manager = obs_client
    socket = _RecordingSocket()
    run_id = uuid.uuid4()

    with client:
        client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]
        resp = client.post(
            f"/observability/runs/{run_id}/trace",
            json={
                "steps": [{"index": 0, "kind": "plan"}],
                "status": "succeeded",
                "confidence": 0.95,
            },
        )
        assert resp.status_code == 201, resp.text

    assert [e["type"] for e in socket.sent] == [RealtimeEventType.RUN_COMPLETED.value]
    assert socket.sent[0]["payload"] == {"status": "succeeded"}


def test_run_trace_record_emits_failed_event(
    obs_client: tuple[TestClient, ConnectionManager],
) -> None:
    client, manager = obs_client
    socket = _RecordingSocket()
    run_id = uuid.uuid4()

    with client:
        client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]
        resp = client.post(
            f"/observability/runs/{run_id}/trace",
            json={"steps": [], "status": "failed"},
        )
        assert resp.status_code == 201, resp.text

    assert [e["type"] for e in socket.sent] == [RealtimeEventType.RUN_FAILED.value]


def test_run_trace_record_without_status_emits_updated_event(
    obs_client: tuple[TestClient, ConnectionManager],
) -> None:
    """A step-only update (no terminal status yet) fans ``run.updated`` out."""
    client, manager = obs_client
    socket = _RecordingSocket()
    run_id = uuid.uuid4()

    with client:
        client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]
        resp = client.post(
            f"/observability/runs/{run_id}/trace",
            json={"steps": [{"index": 0, "kind": "plan"}]},
        )
        assert resp.status_code == 201, resp.text

    assert [e["type"] for e in socket.sent] == [RealtimeEventType.RUN_UPDATED.value]
    assert socket.sent[0]["payload"] == {"status": None}


def test_run_trace_broadcast_is_workspace_isolated(
    obs_client: tuple[TestClient, ConnectionManager],
) -> None:
    client, manager = obs_client
    foreign = _RecordingSocket()
    run_id = uuid.uuid4()

    with client:
        client.portal.call(manager.connect, uuid.uuid4(), foreign)  # type: ignore[arg-type]
        client.post(f"/observability/runs/{run_id}/trace", json={"status": "running"})

    assert foreign.sent == []


# --------------------------------------------------------------------------- #
# approval.* — POST /approvals, POST /approvals/{id}/decision                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def approval_client(
    authenticate_app: Callable[..., FastAPI],
) -> Iterator[tuple[TestClient, ConnectionManager]]:
    app = create_app()
    authenticate_app(app)
    service = ApprovalService(
        InMemoryApprovalRepository(),
        build_gate_registry(InMemoryGrantStore()),
        ApprovalAuthorizer(),
        events=InMemoryActivityBus(),
        redactor=redact_mapping,
    )
    manager = ConnectionManager()
    app.dependency_overrides[get_approval_service] = lambda: service
    app.dependency_overrides[get_connection_manager] = lambda: manager
    client = TestClient(app)
    with client:
        yield client, manager


def _open_gate(client: TestClient) -> dict[str, Any]:
    resp = client.post(
        "/approvals",
        json={
            "gate_type": "pr",
            "subject_type": "workflow_run",
            "subject_id": str(uuid.uuid4()),
            "requested_actor": f"agent:{uuid.uuid4()}",
            "title": "pr gate",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_approval_create_emits_requested_event(
    approval_client: tuple[TestClient, ConnectionManager],
) -> None:
    client, manager = approval_client
    socket = _RecordingSocket()
    client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]

    created = _open_gate(client)

    assert [e["type"] for e in socket.sent] == [RealtimeEventType.APPROVAL_REQUESTED.value]
    assert socket.sent[0]["approval_id"] == created["id"]
    assert socket.sent[0]["payload"] == {"gate_type": "pr"}
    assert "workspace_id" not in socket.sent[0]


def test_approval_decision_emits_decided_event(
    approval_client: tuple[TestClient, ConnectionManager],
) -> None:
    client, manager = approval_client
    socket = _RecordingSocket()
    client.portal.call(manager.connect, WORKSPACE, socket)  # type: ignore[arg-type]

    created = _open_gate(client)
    socket.sent.clear()  # isolate the decision event from the requested one

    resp = client.post(
        f"/approvals/{created['id']}/decision",
        json={"decision": "approve", "note": "looks good"},
    )
    assert resp.status_code == 200, resp.text

    assert [e["type"] for e in socket.sent] == [RealtimeEventType.APPROVAL_DECIDED.value]
    assert socket.sent[0]["approval_id"] == created["id"]
    assert socket.sent[0]["payload"] == {"status": "approved"}


def test_approval_broadcast_is_workspace_isolated(
    approval_client: tuple[TestClient, ConnectionManager],
) -> None:
    client, manager = approval_client
    foreign = _RecordingSocket()
    client.portal.call(manager.connect, uuid.uuid4(), foreign)  # type: ignore[arg-type]

    _open_gate(client)

    assert foreign.sent == []
