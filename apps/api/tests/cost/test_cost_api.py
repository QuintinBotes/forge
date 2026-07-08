"""F38 Cost API: rollups, RBAC, tenant isolation, audited mutations (AC9/10/14/15)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from cost_support import (  # noqa: F401 - imported fixtures are pytest-discovered
    FOREIGN_TASK_ID,
    NOW,
    OTHER_WS_ID,
    TASK_ID,
    WS_ID,
    CapturingAuditSink,
    audit_sink,
    client_factory,
    cost_service,
    ledger,
    price_book,
    price_store,
)
from fastapi.testclient import TestClient

from forge_contracts import UserRole


def test_task_cost_summary_by_phase(client_factory: Callable[..., TestClient]) -> None:
    """AC9: total == sum of the task's events; buckets keyed by phase."""
    res = client_factory(UserRole.VIEWER).get(f"/tasks/{TASK_ID}/cost")
    assert res.status_code == 200
    body = res.json()
    assert body["scope"] == "task" and body["group_by"] == "phase"
    assert Decimal(body["total_cost_usd"]) == Decimal("0.43")
    by_key = {b["key"]: Decimal(b["cost_usd"]) for b in body["buckets"]}
    assert by_key == {
        "spec_drafting": Decimal("0.04"),
        "executing": Decimal("0.34"),
        "verifying": Decimal("0.05"),
    }
    assert sum(by_key.values()) == Decimal(body["total_cost_usd"])


def test_summary_group_by_provider_and_model(client_factory) -> None:
    """AC10: bucket sums equal the scoped total for every group_by."""
    client = client_factory(UserRole.VIEWER)
    for group_by in ("provider", "model", "tier", "strategy", "none"):
        res = client.get(
            "/cost/summary",
            params={"scope": "workspace", "scope_id": str(WS_ID), "group_by": group_by},
        )
        assert res.status_code == 200
        body = res.json()
        total = Decimal(body["total_cost_usd"])
        assert total == Decimal("0.43")
        assert sum(Decimal(b["cost_usd"]) for b in body["buckets"]) == total


def test_timeseries_day_by_provider(client_factory) -> None:
    res = client_factory(UserRole.VIEWER).get(
        "/cost/timeseries",
        params={"scope": "task", "scope_id": str(TASK_ID), "bucket": "day", "group_by": "provider"},
    )
    assert res.status_code == 200
    body = res.json()
    assert set(body["series"]) == {"anthropic", "openai"}
    total = sum(Decimal(str(cost)) for points in body["series"].values() for _, cost in points)
    assert total == Decimal("0.43")


def test_summary_defaults_to_caller_workspace(client_factory) -> None:
    res = client_factory(UserRole.VIEWER).get("/cost/summary")
    assert res.status_code == 200
    assert Decimal(res.json()["total_cost_usd"]) == Decimal("0.43")


def test_cross_workspace_scope_is_404(client_factory) -> None:
    """AC14: foreign scope ids do not leak existence."""
    client = client_factory(UserRole.ADMIN)
    assert client.get(f"/tasks/{FOREIGN_TASK_ID}/cost").status_code == 404
    # A workspace scope_id that is not the caller's workspace is 404 too.
    res = client.get(
        "/cost/summary", params={"scope": "workspace", "scope_id": str(FOREIGN_TASK_ID)}
    )
    assert res.status_code == 404
    # From the other workspace's own principal, its task resolves (empty ledger).
    other = client_factory(UserRole.VIEWER, workspace_id=OTHER_WS_ID)
    assert other.get(f"/tasks/{FOREIGN_TASK_ID}/cost").status_code == 200


def test_unauthenticated_is_401(client_factory) -> None:
    assert client_factory(authenticated=False).get("/cost/summary").status_code == 401


def test_viewer_reads_admin_mutates(client_factory, audit_sink: CapturingAuditSink) -> None:
    """AC14: viewer+ read; POST prices / reprice require admin."""
    viewer = client_factory(UserRole.VIEWER)
    assert viewer.get("/cost/prices").status_code == 200
    price_body = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "kind": "completion",
        "prompt_usd_per_1k": "0.001",
        "completion_usd_per_1k": "0.005",
    }
    assert viewer.post("/cost/prices", json=price_body).status_code == 403
    assert viewer.post("/cost/reprice", json={"since": NOW.isoformat()}).status_code == 403
    member = client_factory(UserRole.MEMBER)
    assert member.post("/cost/prices", json=price_body).status_code == 403

    admin = client_factory(UserRole.ADMIN)
    created = admin.post("/cost/prices", json=price_body)
    assert created.status_code == 201
    assert created.json()["workspace_id"] == str(WS_ID)
    assert [e.action for e in audit_sink.events] == ["cost.price_set"]


def test_price_list_includes_globals_and_overrides(client_factory) -> None:
    admin = client_factory(UserRole.ADMIN)
    admin.post(
        "/cost/prices",
        json={
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
            "kind": "completion",
            "prompt_usd_per_1k": "0.001",
            "completion_usd_per_1k": "0.005",
        },
    )
    res = admin.get("/cost/prices", params={"provider": "anthropic"})
    assert res.status_code == 200
    items = res.json()["items"]
    workspace_ids = {item["workspace_id"] for item in items}
    assert None in workspace_ids and str(WS_ID) in workspace_ids


def test_price_kind_validated(client_factory) -> None:
    res = client_factory(UserRole.ADMIN).post(
        "/cost/prices",
        json={
            "provider": "p",
            "model": "m",
            "kind": "video",
            "prompt_usd_per_1k": "1",
            "completion_usd_per_1k": "1",
        },
    )
    assert res.status_code == 422


def test_reprice_applies_current_price_book_and_audits(
    client_factory, price_book, audit_sink: CapturingAuditSink
) -> None:
    """AC15 via the API: rows on/after `since` are recomputed; audited; idempotent."""
    import uuid as _uuid
    from datetime import timedelta

    from forge_obs.cost.models import ModelPrice

    price_book.add(
        ModelPrice(
            id=_uuid.uuid4(),
            provider="anthropic",
            model="claude-sonnet-4-5",
            kind="completion",
            prompt_usd_per_1k=Decimal("0.001"),
            completion_usd_per_1k=Decimal("0.001"),
            effective_from=NOW - timedelta(days=1),
        )
    )
    admin = client_factory(UserRole.ADMIN)
    res = admin.post(
        "/cost/reprice",
        json={"since": NOW.isoformat(), "provider": "anthropic", "model": "claude-sonnet-4-5"},
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 3  # r1, r2, r4 (anthropic rows)
    assert [e.action for e in audit_sink.events][-1] == "cost.repriced"

    # Idempotent: a second run updates nothing further (still audited).
    again = admin.post(
        "/cost/reprice",
        json={"since": NOW.isoformat(), "provider": "anthropic", "model": "claude-sonnet-4-5"},
    )
    assert again.json()["updated"] == 0

    # New per-row cost: 1000/1k*0.001 + 100/1k*0.001 = 0.0011 (x3) + 0.06 openai.
    summary = admin.get(f"/tasks/{TASK_ID}/cost").json()
    assert Decimal(summary["total_cost_usd"]) == Decimal("0.0633")


def test_observability_metrics_exposition(client_factory) -> None:
    """The internal scrape surface renders the recording registry, else empty."""
    from forge_obs.metrics import RecordingMetrics, reset_metrics, set_metrics

    client = client_factory(UserRole.VIEWER)
    try:
        reset_metrics()
        empty = client.get("/observability/metrics")
        assert empty.status_code == 200 and empty.text == ""

        real = RecordingMetrics(service="forge-api")
        real.record_task_completion(status="completed", duration_seconds=None)
        set_metrics(real)
        res = client.get("/observability/metrics")
        assert res.status_code == 200
        assert 'forge_task_completions_total{status="completed"} 1.0' in res.text
    finally:
        reset_metrics()
