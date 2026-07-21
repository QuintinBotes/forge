"""V1/V2 parity for Red-Team Gate verdict building (Task 20).

``forge_workflow.red_team_gate`` is the single source of the gate's verdict
shape for BOTH spines: the Temporal activity's default parked-pass delegates to
it (byte-identical), and the V1 (FSM) path evaluates + persists through
``ensure_red_team_verdict`` / ``run_and_record_red_team`` against the
append-only ``red_team_record`` table (hermetic SQLite here, mirroring
``packages/db/tests/test_red_team_models.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forge_db.base import Base
from forge_db.models import Project, Task, WorkflowRun, Workspace
from forge_db.models.audit import AuditLog
from forge_db.redteam import REDTEAM_SURVIVED_ACTION, RedTeamRepository
from forge_workflow.red_team_gate import (
    REDTEAM_BLOCKED,
    REDTEAM_SURVIVED,
    RedTeamInput,
    RedTeamResult,
    ensure_red_team_verdict,
    evaluate_red_team,
    parked_pass_verdict,
    run_and_record_red_team,
)


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


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed workspace -> project -> task -> workflow_run; return (ws, task, run) ids."""
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
    return ws.id, task.id, run.id


def _inp(ws: uuid.UUID, task: uuid.UUID, run: uuid.UUID) -> RedTeamInput:
    return RedTeamInput(
        workflow_run_id=run,
        workspace_id=ws,
        task_id=task,
        phase="spec",
        idempotency_key=f"{run}:red_team:spec",
    )


# --------------------------------------------------------------------------- #
# Extraction parity (the Temporal default must be byte-identical)              #
# --------------------------------------------------------------------------- #


def test_parked_pass_shape_matches_temporal_default() -> None:
    """The shared parked-pass IS the Temporal activity's default verdict."""
    from forge_workflow.temporal.activities import _default_red_team

    inp = _inp(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    shared = evaluate_red_team(inp)
    temporal = _default_red_team(inp)

    assert shared == temporal
    assert shared.verdict == REDTEAM_SURVIVED
    assert shared.kind == "parked"
    assert shared.evidence == {"parked": True, "reason": "no adversary model/sandbox wired"}
    assert shared.adversary_model is None
    assert shared.coder_model is None
    assert shared == parked_pass_verdict()


def test_payloads_reexport_the_same_objects() -> None:
    """`temporal/payloads` re-exports the extracted types unchanged (same objects),
    so every existing Temporal import path keeps working byte-identically."""
    from forge_workflow import red_team_gate
    from forge_workflow.temporal import payloads

    assert payloads.RedTeamInput is red_team_gate.RedTeamInput
    assert payloads.RedTeamResult is red_team_gate.RedTeamResult
    assert payloads.REDTEAM_BLOCKED is red_team_gate.REDTEAM_BLOCKED
    assert payloads.REDTEAM_SURVIVED is red_team_gate.REDTEAM_SURVIVED


def test_configured_adversary_fn_is_invoked() -> None:
    """When an adversary is configured, the helper runs IT — never the parked-pass."""
    calls: list[RedTeamInput] = []

    def block(inp: RedTeamInput) -> RedTeamResult:
        calls.append(inp)
        return RedTeamResult(
            verdict=REDTEAM_BLOCKED,
            kind="failing_test",
            evidence={"stdout": "AssertionError"},
            adversary_model="gpt-5-heavy",
            coder_model="claude-sonnet-4",
        )

    inp = _inp(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    result = evaluate_red_team(inp, red_team_fn=block)

    assert calls == [inp]
    assert result.blocked
    assert result.kind == "failing_test"


# --------------------------------------------------------------------------- #
# V1 gate persistence (ensure/run_and_record over the append-only table)       #
# --------------------------------------------------------------------------- #


def test_ensure_persists_parked_verdict_once(sqlite_session: Session) -> None:
    """A V1 run reaching its gate persists ONE honest parked verdict row."""
    ws, task, run = _seed(sqlite_session)

    row, created = ensure_red_team_verdict(sqlite_session, ws, workflow_run_id=run, task_id=task)
    assert created is True
    assert row.verdict == REDTEAM_SURVIVED
    assert row.kind == "parked"
    assert row.evidence == {"parked": True, "reason": "no adversary model/sandbox wired"}
    assert row.adversary_model is None
    assert row.coder_model is None
    assert row.workflow_run_id == run

    # Idempotent: the gate re-entered (block -> changes -> resubmit) does not rescan.
    again, created_again = ensure_red_team_verdict(
        sqlite_session, ws, workflow_run_id=run, task_id=task
    )
    assert created_again is False
    assert again.id == row.id
    assert len(RedTeamRepository(sqlite_session).get_by_run(ws, run)) == 1


def test_ensure_survived_chains_the_audit_event(sqlite_session: Session) -> None:
    """The parked survive still chains the ``redteam.survived`` audit event
    (recorded honestly as kind=parked in its details)."""
    ws, task, run = _seed(sqlite_session)
    row, _ = ensure_red_team_verdict(sqlite_session, ws, workflow_run_id=run, task_id=task)

    audit = sqlite_session.scalars(
        select(AuditLog).where(AuditLog.action == REDTEAM_SURVIVED_ACTION)
    ).all()
    assert len(audit) == 1
    assert audit[0].detail_ref == {"table": "red_team_record", "id": str(row.id)}
    assert audit[0].details["kind"] == "parked"


def test_run_and_record_appends_a_new_scan(sqlite_session: Session) -> None:
    """An explicit trigger appends (block -> re-trigger -> survive history)."""
    ws, task, run = _seed(sqlite_session)
    ensure_red_team_verdict(sqlite_session, ws, workflow_run_id=run, task_id=task)

    def block(_: RedTeamInput) -> RedTeamResult:
        return RedTeamResult(
            verdict=REDTEAM_BLOCKED,
            kind="failing_test",
            evidence={"stdout": "AssertionError"},
            adversary_model="gpt-5-heavy",
            coder_model="claude-sonnet-4",
        )

    row = run_and_record_red_team(
        sqlite_session, ws, workflow_run_id=run, task_id=task, red_team_fn=block
    )
    assert row.verdict == REDTEAM_BLOCKED
    rows = RedTeamRepository(sqlite_session).get_by_run(ws, run)
    assert len(rows) == 2
    verdicts = {r.verdict for r in rows}
    assert verdicts == {REDTEAM_BLOCKED, REDTEAM_SURVIVED}
