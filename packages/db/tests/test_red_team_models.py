"""Unit + Postgres-integration tests for the Red-Team Gate persistence slice
(``red_team_record`` — the storage substrate for "survived adversarial review").

The SQLite tests exercise the shared substrate (roundtrip, workspace scoping,
nullable run link, the insert-only repository, and the ``record_red_team_verdict``
recorder that chains a ``redteam.survived`` audit event) the way
``test_models.py`` does for every other model; the Postgres-marked tests exercise
the real code path the SQLite unit tests cannot: the F39 immutability trigger
that blocks UPDATE/DELETE on the append-only table. Uses the shared ``pg_engine``
fixture; skips (parked) without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import Project, RedTeamRecord, Task, WorkflowRun, Workspace
from forge_db.models.audit import AuditLog
from forge_db.models.red_team import VERDICT_BLOCKED, VERDICT_SURVIVED
from forge_db.redteam import (
    REDTEAM_SURVIVED_ACTION,
    RedTeamRepository,
    record_red_team_verdict,
)

CODER_MODEL = "claude-sonnet-4"
ADVERSARY_MODEL = "gpt-5-heavy"


# --------------------------------------------------------------------------- #
# SQLite unit tests                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sqlite_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed workspace -> project -> task -> workflow_run; return (ws_id, run_id)."""
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(
        workspace_id=ws.id,
        project_id=project.id,
        key=f"TASK-{uuid.uuid4().hex[:6]}",
        title="red-team task",
    )
    session.add(task)
    session.flush()
    run = WorkflowRun(workspace_id=ws.id, task_id=task.id)
    session.add(run)
    session.flush()
    return ws.id, run.id


def test_insert_and_roundtrip(sqlite_session: Session) -> None:
    ws_id, run_id = _seed(sqlite_session)
    evidence = {"test": "test_regression.py::test_boom", "stdout": "AssertionError"}
    row = RedTeamRecord(
        workspace_id=ws_id,
        workflow_run_id=run_id,
        verdict=VERDICT_BLOCKED,
        kind="failing_test",
        evidence=evidence,
        adversary_model=ADVERSARY_MODEL,
        coder_model=CODER_MODEL,
    )
    sqlite_session.add(row)
    sqlite_session.commit()

    loaded = sqlite_session.get(RedTeamRecord, row.id)
    assert loaded is not None
    assert loaded.workspace_id == ws_id
    assert loaded.workflow_run_id == run_id
    assert loaded.verdict == VERDICT_BLOCKED
    assert loaded.kind == "failing_test"
    assert loaded.evidence == evidence
    assert loaded.adversary_model == ADVERSARY_MODEL
    assert loaded.coder_model == CODER_MODEL
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


def test_workflow_run_id_is_nullable(sqlite_session: Session) -> None:
    ws_id, _run_id = _seed(sqlite_session)
    row = RedTeamRecord(
        workspace_id=ws_id,
        verdict=VERDICT_SURVIVED,
        kind="parked",
        evidence={"parked": True},
    )
    sqlite_session.add(row)
    sqlite_session.commit()
    assert row.workflow_run_id is None
    # Parked-pass survives with no adversary model.
    assert row.adversary_model is None
    assert row.coder_model is None


def test_repository_insert_and_survived_query(sqlite_session: Session) -> None:
    ws_id, run_id = _seed(sqlite_session)
    repo = RedTeamRepository(sqlite_session)

    repo.insert(
        ws_id,
        verdict=VERDICT_BLOCKED,
        kind="spec_violation",
        evidence={"rule": "no-secrets"},
        adversary_model=ADVERSARY_MODEL,
        coder_model=CODER_MODEL,
        workflow_run_id=run_id,
    )
    survived = repo.insert(
        ws_id,
        verdict=VERDICT_SURVIVED,
        kind="failing_test",
        evidence={"ran": True, "failed": False},
        adversary_model=ADVERSARY_MODEL,
        coder_model=CODER_MODEL,
        workflow_run_id=run_id,
    )
    sqlite_session.commit()

    # get_by_run returns both verdicts; survived_for_run filters to survived only.
    assert len(repo.get_by_run(ws_id, run_id)) == 2
    survived_rows = repo.survived_for_run(ws_id, run_id)
    assert [r.id for r in survived_rows] == [survived.id]


def test_record_verdict_survived_emits_audit(sqlite_session: Session) -> None:
    """A ``survived`` verdict writes the record AND chains a redteam.survived
    audit event whose detail_ref points back at the row — the tamper-evident
    "survived adversarial review" fact the attestation reads."""
    ws_id, run_id = _seed(sqlite_session)

    row, audit_log = record_red_team_verdict(
        sqlite_session,
        ws_id,
        verdict=VERDICT_SURVIVED,
        kind="failing_test",
        evidence={"ran": True, "failed": False},
        adversary_model=ADVERSARY_MODEL,
        coder_model=CODER_MODEL,
        workflow_run_id=run_id,
    )
    sqlite_session.commit()

    assert row.verdict == VERDICT_SURVIVED
    assert audit_log is not None
    assert audit_log.action == REDTEAM_SURVIVED_ACTION
    assert audit_log.detail_ref == {"table": "red_team_record", "id": str(row.id)}

    # The record is discoverable as a survived adversarial review for the run.
    assert [r.id for r in RedTeamRepository(sqlite_session).survived_for_run(ws_id, run_id)] == [
        row.id
    ]
    # Exactly one audit row was chained for the survive.
    events = sqlite_session.scalars(
        select(AuditLog).where(AuditLog.action == REDTEAM_SURVIVED_ACTION)
    ).all()
    assert len(events) == 1


def test_record_verdict_blocked_emits_no_audit(sqlite_session: Session) -> None:
    """A ``blocked`` verdict is recorded but chains no audit event (nothing
    survived to attest)."""
    ws_id, run_id = _seed(sqlite_session)

    row, audit_log = record_red_team_verdict(
        sqlite_session,
        ws_id,
        verdict=VERDICT_BLOCKED,
        kind="failing_test",
        evidence={"ran": True, "failed": True, "stdout": "AssertionError"},
        adversary_model=ADVERSARY_MODEL,
        coder_model=CODER_MODEL,
        workflow_run_id=run_id,
    )
    sqlite_session.commit()

    assert row.verdict == VERDICT_BLOCKED
    assert audit_log is None
    assert RedTeamRepository(sqlite_session).survived_for_run(ws_id, run_id) == []
    assert (
        sqlite_session.scalars(
            select(AuditLog).where(AuditLog.action == REDTEAM_SURVIVED_ACTION)
        ).all()
        == []
    )


# --------------------------------------------------------------------------- #
# Postgres integration tests (immutability)                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.mark.usefixtures("pg_engine")
def test_red_team_record_is_immutable(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id = _seed(session)
        row = RedTeamRecord(
            workspace_id=ws_id,
            workflow_run_id=run_id,
            verdict=VERDICT_SURVIVED,
            kind="failing_test",
            evidence={"ran": True, "failed": False},
            adversary_model=ADVERSARY_MODEL,
            coder_model=CODER_MODEL,
        )
        session.add(row)
        session.commit()
        row_id = row.id

    # A direct UPDATE is blocked by the immutability trigger.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(
                update(RedTeamRecord)
                .where(RedTeamRecord.id == row_id)
                .values(verdict=VERDICT_BLOCKED)
            )
            session.commit()
        session.rollback()

    # A direct DELETE is likewise blocked.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(text("DELETE FROM red_team_record WHERE id = :i"), {"i": str(row_id)})
            session.commit()
        session.rollback()
