"""Automations router integration tests (F21).

Covers CRUD + validation + version conflict + human-gate guard + dry-run +
catalog + delete-preserves-audit (ACs 1, 2, 3, 11, 12, 17, 19).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from forge_db.models import AutomationExecution
from forge_db.models.enums import (
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerSource,
    AutomationTriggerType,
)

from .conftest import WS_ID, close_spec_rule_body


def _create(client: TestClient, project_id: uuid.UUID, body: dict | None = None):
    return client.post(f"/projects/{project_id}/automations", json=body or close_spec_rule_body())


def test_create_returns_201_and_persists(client: TestClient, project_id) -> None:
    resp = _create(client, project_id)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["version"] == 1
    assert data["enabled"] is True
    assert data["trigger"]["type"] == "workflow_state_changed"


def test_create_empty_actions_422(client: TestClient, project_id) -> None:
    body = close_spec_rule_body()
    body["actions"] = []
    assert _create(client, project_id, body).status_code == 422


def test_reference_validation_bad_assignee_422(client: TestClient, project_id) -> None:
    body = close_spec_rule_body()
    body["trigger"] = {"type": "task_priority_changed"}
    body["condition"] = {}
    body["actions"] = [{"type": "set_assignee", "assignee_id": str(uuid.uuid4())}]
    resp = _create(client, project_id, body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "rule_validation_error"


def test_human_gate_event_422(client: TestClient, project_id) -> None:
    body = close_spec_rule_body()
    body["actions"] = [{"type": "send_workflow_event", "event": "review_approved_by_human"}]
    resp = _create(client, project_id, body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "action_forbidden_event"


def test_non_gate_event_accepted(client: TestClient, project_id) -> None:
    body = close_spec_rule_body()
    body["actions"] = [{"type": "send_workflow_event", "event": "close"}]
    assert _create(client, project_id, body).status_code == 201


def test_list_get_enable_disable(client: TestClient, project_id) -> None:
    rid = _create(client, project_id).json()["id"]
    assert any(r["id"] == rid for r in client.get(f"/projects/{project_id}/automations").json())
    assert client.get(f"/automations/{rid}").status_code == 200
    assert client.post(f"/automations/{rid}/disable").json()["enabled"] is False
    assert client.post(f"/automations/{rid}/enable").json()["enabled"] is True


def test_version_conflict_409(client: TestClient, project_id) -> None:
    rid = _create(client, project_id).json()["id"]
    # First patch at version 1 -> 200 (version becomes 2).
    r1 = client.patch(f"/automations/{rid}", json={"version": 1, "name": "renamed"})
    assert r1.status_code == 200
    assert r1.json()["version"] == 2
    # Second patch with the stale version 1 -> 409.
    r2 = client.patch(f"/automations/{rid}", json={"version": 1, "name": "again"})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "version_conflict"
    assert r2.json()["detail"]["current_version"] == 2


def test_dry_run_is_side_effect_free(client: TestClient, project_id, seeded) -> None:
    rid = _create(client, project_id).json()["id"]
    resp = client.post(
        f"/automations/{rid}/test",
        json={"task_id": str(seeded["task0"]), "change": {"to_state": "merged"}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trigger_matched"] is True
    assert data["condition_result"] is True
    assert len(data["planned_actions"]) == 1
    # No execution row was written.
    assert client.get(f"/automations/{rid}/executions").json() == []


def test_catalog_completeness(client: TestClient) -> None:
    cat = client.get("/automations/catalog").json()
    trigger_types = {t["type"] for t in cat["triggers"]}
    assert "workflow_state_changed" in trigger_types
    wf = next(t for t in cat["triggers"] if t["type"] == "workflow_state_changed")
    assert "to_state" in wf["required_config"]
    assert "has_spec" in cat["condition_fields"]
    action_types = {a["type"] for a in cat["actions"]}
    assert "close_linked_spec_tasks" in action_types
    assert cat["condition_ops"]


def test_delete_preserves_executions(
    client: TestClient, project_id, session_factory: sessionmaker, seeded
) -> None:
    rid = _create(client, project_id).json()["id"]
    # Seed an execution row that references the rule.
    with session_factory() as session:
        session.add(
            AutomationExecution(
                workspace_id=WS_ID,
                rule_id=uuid.UUID(rid),
                rule_version=1,
                trigger_type=AutomationTriggerType.WORKFLOW_STATE_CHANGED,
                trigger_event_id=uuid.uuid4(),
                trigger_source=AutomationTriggerSource.WORKFLOW_TRANSITION,
                entity_type=AutomationEntityType.TASK,
                entity_id=seeded["task0"],
                status=AutomationExecutionStatus.SUCCEEDED,
                depth=0,
                idempotency_key=f"{rid}:{uuid.uuid4()}",
            )
        )
        session.commit()

    assert client.delete(f"/automations/{rid}").status_code == 204
    assert client.get(f"/automations/{rid}").status_code == 404
    # The audit execution row survives the rule deletion.
    with session_factory() as session:
        rows = list(session.query(AutomationExecution).all())
        assert len(rows) == 1
        assert rows[0].rule_version == 1


def test_cross_workspace_is_404(client_factory, project_id) -> None:
    from forge_contracts import UserRole

    owner = client_factory(role=UserRole.ADMIN)
    rid = _create(owner, project_id).json()["id"]
    from .conftest import OTHER_WS_ID

    stranger = client_factory(role=UserRole.ADMIN, workspace_id=OTHER_WS_ID)
    assert stranger.get(f"/automations/{rid}").status_code == 404
