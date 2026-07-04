"""Tests for the /observability/* API routes (Task 1.14 — observability + audit).

The router exposes the immutable audit log and step-level run traces for the
trace viewer. These hit the ASGI app via httpx with an isolated, overridden
:class:`ObservabilityService` (no live services).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI

from forge_api.deps import get_current_principal
from forge_api.main import app
from forge_api.observability.audit import AuditCategory
from forge_api.observability.service import ObservabilityService, get_observability_service
from forge_contracts import Step
from forge_contracts.enums import RunStatus, StepKind

# The audit log and run traces are workspace-scoped (Phase-2 bug fix r3): the
# router scopes every read to the authenticated principal's workspace. The
# ``authenticate_app`` fixture injects a principal in ``TEST_WORKSPACE_ID``
# (conftest), so these same-tenant tests record their entries/runs under that
# workspace; cross-tenant denial is covered in ``test_rbac_tenant_r3.py``.
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def service(
    authenticate_app: Callable[..., FastAPI],
) -> Iterator[ObservabilityService]:
    svc = ObservabilityService()
    app.dependency_overrides[get_observability_service] = lambda: svc
    # Real auth is enforced; inject an authenticated principal on the shared app
    # and clean it up so it never leaks into other tests using the module app.
    authenticate_app(app)
    try:
        yield svc
    finally:
        app.dependency_overrides.pop(get_observability_service, None)
        app.dependency_overrides.pop(get_current_principal, None)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_audit_endpoint_returns_recorded_entries(
    client: httpx.AsyncClient, service: ObservabilityService
) -> None:
    run = uuid.uuid4()
    service.audit.record(
        category=AuditCategory.AGENT_ACTION,
        action="plan",
        run_id=run,
        workspace_id=TEST_WORKSPACE_ID,
    )
    service.audit.record(
        category=AuditCategory.TOOL_CALL,
        action="write_file",
        run_id=run,
        workspace_id=TEST_WORKSPACE_ID,
    )

    resp = await client.get("/observability/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert {e["action"] for e in body} == {"plan", "write_file"}


async def test_audit_endpoint_filters_by_category(
    client: httpx.AsyncClient, service: ObservabilityService
) -> None:
    service.audit.record(
        category=AuditCategory.AGENT_ACTION, action="plan", workspace_id=TEST_WORKSPACE_ID
    )
    service.audit.record(
        category=AuditCategory.TOOL_CALL,
        action="write_file",
        workspace_id=TEST_WORKSPACE_ID,
    )

    resp = await client.get("/observability/audit", params={"category": "tool_call"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["action"] == "write_file"


async def test_audit_endpoint_redacts_secrets(
    client: httpx.AsyncClient, service: ObservabilityService
) -> None:
    service.audit.record(
        category=AuditCategory.TOOL_CALL,
        action="call_api",
        metadata={"api_key": "sk-SECRET1234567890"},
        workspace_id=TEST_WORKSPACE_ID,
    )
    resp = await client.get("/observability/audit")
    assert resp.status_code == 200
    assert "sk-SECRET1234567890" not in resp.text


async def test_run_trace_endpoint_returns_ordered_steps(
    client: httpx.AsyncClient, service: ObservabilityService
) -> None:
    run = uuid.uuid4()
    service.record_run(
        run,
        steps=[
            Step(index=1, kind=StepKind.TOOL_CALL),
            Step(index=0, kind=StepKind.PLAN),
        ],
        status=RunStatus.SUCCEEDED,
        workspace_id=TEST_WORKSPACE_ID,
    )
    resp = await client.get(f"/observability/runs/{run}/trace")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_steps"] == 2
    assert [s["index"] for s in body["steps"]] == [0, 1]
    assert body["steps"][0]["kind"] == "plan"


async def test_run_trace_endpoint_404_for_unknown_run(
    client: httpx.AsyncClient, service: ObservabilityService
) -> None:
    resp = await client.get(f"/observability/runs/{uuid.uuid4()}/trace")
    assert resp.status_code == 404


async def test_audit_endpoint_requires_no_body_and_lists_empty_by_default(
    client: httpx.AsyncClient, service: ObservabilityService
) -> None:
    resp = await client.get("/observability/audit")
    assert resp.status_code == 200
    assert resp.json() == []
