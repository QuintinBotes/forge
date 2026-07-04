"""F26 velocity dashboard + export tests (AC #11)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from forge_board.sprint_service import SprintService
from forge_contracts.enums import CarryoverTarget, TaskStatus
from forge_db.models import Sprint, Task

from .conftest import PROJECT_ID, WS_ID

API = ""


def _new_task(sf: sessionmaker[Session], points: int) -> uuid.UUID:
    with sf() as session:
        t = Task(
            workspace_id=WS_ID,
            project_id=PROJECT_ID,
            key=f"CORE-{uuid.uuid4().hex[:6]}",
            title="seed",
            status=TaskStatus.IN_PROGRESS,
            estimate=points,
        )
        session.add(t)
        session.commit()
        return t.id


def _completed_sprint(
    service: SprintService,
    sf: sessionmaker[Session],
    *,
    committed: list[int],
    complete_first_n: int,
) -> uuid.UUID:
    with sf() as session:
        sprint = Sprint(
            workspace_id=WS_ID,
            project_id=PROJECT_ID,
            name=f"S-{uuid.uuid4().hex[:4]}",
            status="planned",
            start_date=__import__("datetime").date(2026, 6, 1),
            end_date=__import__("datetime").date(2026, 6, 14),
        )
        session.add(sprint)
        session.commit()
        sid = sprint.id
    task_ids = [_new_task(sf, p) for p in committed]
    for tid in task_ids:
        service.add_task(workspace_id=WS_ID, sprint_id=sid, task_id=tid)
    service.start(workspace_id=WS_ID, sprint_id=sid)
    for tid in task_ids[:complete_first_n]:
        service.set_task_status(workspace_id=WS_ID, task_id=tid, status=TaskStatus.DONE)
    service.complete(workspace_id=WS_ID, sprint_id=sid, carryover=CarryoverTarget.BACKLOG)
    return sid


def test_velocity_dashboard_math(
    client: TestClient, service: SprintService, session_factory, project_id: uuid.UUID
) -> None:
    # Sprint 1: committed [10,10] complete both -> completed 20.
    _completed_sprint(service, session_factory, committed=[10, 10], complete_first_n=2)
    # Sprint 2: committed [10,10,10] complete two -> completed 20, carryover 10.
    _completed_sprint(service, session_factory, committed=[10, 10, 10], complete_first_n=2)

    resp = client.get(f"{API}/projects/{project_id}/velocity?last=6")
    assert resp.status_code == 200, resp.text
    dash = resp.json()
    assert len(dash["sprints"]) == 2  # both completed; oldest -> newest
    completed = [b["completed_points"] for b in dash["sprints"]]
    assert completed == [20, 20]
    assert dash["summary"]["sprint_count"] == 2
    assert dash["summary"]["average_velocity"] == 20.0
    assert dash["summary"]["forecast_high"] == 20.0


def test_velocity_excludes_non_completed(
    client: TestClient, service: SprintService, session_factory, project_id: uuid.UUID
) -> None:
    _completed_sprint(service, session_factory, committed=[5], complete_first_n=1)
    # A planned sprint must not appear in the dashboard.
    client.post(
        f"{API}/projects/{project_id}/sprints",
        json={"name": "planned", "start_date": "2026-07-01", "end_date": "2026-07-14"},
    )
    dash = client.get(f"{API}/projects/{project_id}/velocity").json()
    assert len(dash["sprints"]) == 1


def test_velocity_export_csv(
    client: TestClient, service: SprintService, session_factory, project_id: uuid.UUID
) -> None:
    _completed_sprint(service, session_factory, committed=[8], complete_first_n=1)
    resp = client.get(f"{API}/projects/{project_id}/velocity/export?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    body = resp.text.strip().splitlines()
    assert body[0].startswith("sprint_id,name,end_date")
    assert len(body) == 2  # header + one completed sprint
