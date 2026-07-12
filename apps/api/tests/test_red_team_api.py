"""Integration tests for ``GET /workflow/runs/{run_id}/red-team``
(Red-Team Gate, slice redteam-surface).

Seeds a workspace -> project -> task -> workflow_run (mirroring
``packages/db/tests/test_red_team_models.py``'s ``_seed`` helper) and inserts
``RedTeamRecord`` rows directly via ``RedTeamRepository`` — real handlers,
hermetic SQLite (mirrors ``test_run_replay.py``'s convention), no live model,
sandbox or Temporal worker ever touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import Project, Task, WorkflowRun, Workspace
from forge_db.models.red_team import VERDICT_BLOCKED, VERDICT_SURVIVED, RedTeamRecord
from forge_db.redteam import RedTeamRepository

WS = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000d2")

CODER_MODEL = "claude-sonnet-4"
ADVERSARY_MODEL = "gpt-5-heavy"


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
        s.commit()
    yield sf
    engine.dispose()


def _seed_run(factory: sessionmaker[Session], *, workspace_id: uuid.UUID = WS) -> uuid.UUID:
    """Seed workspace -> project -> task -> workflow_run; return the run id."""
    with factory() as s:
        project = Project(workspace_id=workspace_id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=workspace_id,
            project_id=project.id,
            key=f"TASK-{uuid.uuid4().hex[:6]}",
            title="red-team task",
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=workspace_id, task_id=task.id)
        s.add(run)
        s.commit()
        run_id = run.id
    return run_id


def _seed_scan(
    factory: sessionmaker[Session],
    run_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID = WS,
    verdict: str,
    kind: str,
    evidence: dict[str, object],
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Insert one scan via the real repository.

    ``created_at`` is set explicitly (rather than left to
    ``server_default=func.now()``) when the caller needs deterministic
    ordering across two scans: SQLite's ``CURRENT_TIMESTAMP`` only has
    second resolution, so two inserts in the same test can otherwise tie and
    fall to the repository's ``id.desc()`` tiebreak (effectively random UUID
    order) — a real property of ``RedTeamRepository.get_by_run``, not
    something to work around in the endpoint.
    """
    with factory() as s:
        row = RedTeamRepository(s).insert(
            workspace_id,
            verdict=verdict,
            kind=kind,
            evidence=evidence,
            adversary_model=ADVERSARY_MODEL,
            coder_model=CODER_MODEL,
            workflow_run_id=run_id,
        )
        if created_at is not None:
            s.query(RedTeamRecord).filter(RedTeamRecord.id == row.id).update(
                {"created_at": created_at}
            )
        s.commit()
        record_id = row.id
    return record_id


def _principal(workspace_id: uuid.UUID = WS) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role=UserRole.MEMBER,
        email="member@acme.test",
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


def test_survived_verdict_is_returned_with_evidence(factory) -> None:
    run_id = _seed_run(factory)
    evidence = {"ran": True, "failed": False}
    record_id = _seed_scan(
        factory, run_id, verdict=VERDICT_SURVIVED, kind="failing_test", evidence=evidence
    )
    client = _client(factory, _principal())

    resp = client.get(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["workflow_run_id"] == str(run_id)
    assert body["latest"]["id"] == str(record_id)
    assert body["latest"]["verdict"] == "survived"
    assert body["latest"]["kind"] == "failing_test"
    assert body["latest"]["evidence"] == evidence
    assert body["latest"]["adversary_model"] == ADVERSARY_MODEL
    assert body["latest"]["coder_model"] == CODER_MODEL
    assert len(body["records"]) == 1


def test_blocked_verdict_is_returned_with_evidence(factory) -> None:
    run_id = _seed_run(factory)
    evidence = {"test": "test_regression.py::test_boom", "stdout": "AssertionError"}
    _seed_scan(factory, run_id, verdict=VERDICT_BLOCKED, kind="failing_test", evidence=evidence)
    client = _client(factory, _principal())

    resp = client.get(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["latest"]["verdict"] == "blocked"
    assert body["latest"]["evidence"]["stdout"] == "AssertionError"


def test_latest_scan_wins_after_a_block_then_a_survive(factory) -> None:
    """A blocked scan followed by a re-submitted survive: ``latest`` is the
    newest record and ``records`` carries the full history (newest first)."""
    run_id = _seed_run(factory)
    now = datetime.now(UTC)
    _seed_scan(
        factory,
        run_id,
        verdict=VERDICT_BLOCKED,
        kind="failing_test",
        evidence={"stdout": "AssertionError"},
        created_at=now,
    )
    survived_id = _seed_scan(
        factory,
        run_id,
        verdict=VERDICT_SURVIVED,
        kind="failing_test",
        evidence={"ran": True, "failed": False},
        created_at=now + timedelta(minutes=1),
    )
    client = _client(factory, _principal())

    resp = client.get(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["latest"]["id"] == str(survived_id)
    assert body["latest"]["verdict"] == "survived"
    assert len(body["records"]) == 2
    assert [r["verdict"] for r in body["records"]] == ["survived", "blocked"]


def test_unscanned_run_returns_empty_not_404(factory) -> None:
    """A run that exists but has not been scanned yet reads as an empty
    history, never a 404 — the badge simply has nothing to show."""
    run_id = _seed_run(factory)
    client = _client(factory, _principal())

    resp = client.get(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["workflow_run_id"] == str(run_id)
    assert body["latest"] is None
    assert body["records"] == []


def test_unknown_run_id_returns_empty_not_404(factory) -> None:
    client = _client(factory, _principal())

    resp = client.get(f"/workflow/runs/{uuid.uuid4()}/red-team")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latest"] is None
    assert body["records"] == []


def test_cross_workspace_scan_is_not_visible(factory) -> None:
    """A scan recorded for another workspace's run never surfaces — the
    row-level ``workspace_id`` scope, not run existence, is the boundary."""
    run_id = _seed_run(factory, workspace_id=WS)
    _seed_scan(
        factory,
        run_id,
        workspace_id=WS,
        verdict=VERDICT_SURVIVED,
        kind="failing_test",
        evidence={"ran": True, "failed": False},
    )
    other_workspace_client = _client(factory, _principal(workspace_id=WS2))

    resp = other_workspace_client.get(f"/workflow/runs/{run_id}/red-team")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latest"] is None
    assert body["records"] == []
