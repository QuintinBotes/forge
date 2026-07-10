"""F40 PM-depth sprint router integration tests: configurable estimation
scales and the estimate-change history read (the two sub-deliverables of
delta 3 that a prior review found unreachable via any HTTP surface).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from forge_board.sprint_service import SprintService

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def test_estimation_scale_create_and_list(client: TestClient, project_id: uuid.UUID) -> None:
    created = client.post(
        "/estimation-scales",
        json={
            "project_id": str(project_id),
            "name": "Fibonacci",
            "unit": "points",
            "values": [1, 2, 3, 5, 8, 13],
            "is_default": True,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Fibonacci"
    assert body["values"] == [1, 2, 3, 5, 8, 13]
    assert body["is_default"] is True

    listed = client.get("/estimation-scales", params={"project_id": str(project_id)})
    assert listed.status_code == 200
    assert any(s["id"] == body["id"] for s in listed.json())

    updated = client.patch(
        f"/estimation-scales/{body['id']}", json={"values": [1, 2, 3, 5, 8, 13, 21]}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["values"] == [1, 2, 3, 5, 8, 13, 21]


def test_estimation_scale_snaps_task_estimates(
    client: TestClient,
    project_id: uuid.UUID,
    service: SprintService,
    seeded: dict[str, uuid.UUID],
) -> None:
    client.post(
        "/estimation-scales",
        json={
            "project_id": str(project_id),
            "name": "Fibonacci",
            "values": [1, 2, 3, 5, 8, 13],
            "is_default": True,
        },
    )
    task_id = seeded["task0"]
    # 6 is not on the scale -> snaps to the nearest declared value, 5.
    service.set_task_estimate(workspace_id=WS_ID, task_id=task_id, estimate=6)
    history = client.get(f"/tasks/{task_id}/estimate-history")
    assert history.status_code == 200, history.text
    assert history.json()[-1]["points_after"] == 5


def test_estimate_history_endpoint(
    client: TestClient,
    service: SprintService,
    seeded: dict[str, uuid.UUID],
) -> None:
    task_id = seeded["task0"]
    service.set_task_estimate(workspace_id=WS_ID, task_id=task_id, estimate=8)
    resp = client.get(f"/tasks/{task_id}/estimate-history")
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert len(entries) == 1
    assert entries[0]["points_before"] == 3  # seeded estimate
    assert entries[0]["points_after"] == 8
