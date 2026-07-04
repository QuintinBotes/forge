"""F38: the gateway emits forge_mcp_* metrics on every tool call."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge_mcp.testing import sample_connection, sample_transport
from forge_mcp_gateway.app import create_gateway_app
from forge_mcp_gateway.manager import MCPConnectionManager
from forge_obs.metrics import RecordingMetrics, reset_metrics, set_metrics


@pytest.fixture
def registered() -> TestClient:
    manager = MCPConnectionManager(transport_factory=lambda conn: sample_transport())
    client = TestClient(create_gateway_app(manager=manager))
    resp = client.post("/connections", json=sample_connection().model_dump(mode="json"))
    assert resp.status_code == 201, resp.text
    return client


@pytest.fixture
def metrics(registered: TestClient) -> RecordingMetrics:
    # Installed AFTER app creation: create_gateway_app's setup_telemetry installs
    # the env-driven (no-op) providers; the facade is resolved per call, so a
    # recording registry swapped in afterwards observes the emission.
    real = RecordingMetrics(service="forge-mcp-gateway")
    set_metrics(real)
    yield real
    reset_metrics()


def test_tool_call_records_mcp_metrics(registered: TestClient, metrics: RecordingMetrics) -> None:
    resp = registered.post(
        "/connections/confluence-engineering/tools/call",
        json={"name": "search_pages", "arguments": {"query": "auth"}},
    )
    assert resp.status_code == 200
    name = sample_connection().name
    assert metrics.counter_value("forge_mcp_calls_total", connection=name, status="ok") == 1
    assert len(metrics.histogram_values("forge_mcp_call_latency_seconds", connection=name)) == 1


def test_failed_tool_call_records_error_status(
    registered: TestClient, metrics: RecordingMetrics
) -> None:
    resp = registered.post(
        "/connections/confluence-engineering/tools/call",
        json={"name": "delete_page", "arguments": {"id": "x"}},
    )
    assert resp.status_code == 403  # write tool on a read-only connection
    name = sample_connection().name
    assert metrics.counter_value("forge_mcp_calls_total", connection=name, status="error") == 1
