"""V1/V2 parity for Red-Team Gate verdict building (Task 20).

``scf_workflow.red_team_gate`` is the single source of the gate's verdict
shape for BOTH spines: the Temporal activity's default ``not_run`` delegates to
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

from scf_db.base import Base
from scf_db.models import Project, Task, WorkflowRun, Workspace
from scf_db.models.audit import AuditLog
from scf_db.redteam import REDTEAM_SURVIVED_ACTION, RedTeamRepository
from scf_workflow.red_team_gate import (
    REDTEAM_BLOCKED,
    REDTEAM_NOT_RUN,
    REDTEAM_SURVIVED,
    RedTeamInput,
    RedTeamResult,
    ensure_red_team_verdict,
    evaluate_red_team,
    not_run_verdict,
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


def test_without_an_adversary_the_verdict_is_not_run_on_both_engines() -> None:
    """#174 criterion 6: no adversary is never ``survived``; the shared verdict IS the
    Temporal activity's default."""
    from scf_workflow.temporal.activities import _default_red_team

    inp = _inp(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    shared = evaluate_red_team(inp)
    temporal = _default_red_team(inp)

    assert shared == temporal
    assert shared.verdict == REDTEAM_NOT_RUN
    assert shared.kind == "not_run"
    assert shared.evidence == {"reason": "no_candidate_diff"}  # a spec has no code to attack
    assert shared.adversary_model is None
    assert shared.coder_model is None
    assert shared == not_run_verdict("spec")
    assert not_run_verdict("pr").evidence == {"reason": "red_team_not_configured"}


def test_payloads_reexport_the_same_objects() -> None:
    """`temporal/payloads` re-exports the extracted types unchanged (same objects),
    so every existing Temporal import path keeps working byte-identically."""
    from scf_workflow import red_team_gate
    from scf_workflow.temporal import payloads

    assert payloads.RedTeamInput is red_team_gate.RedTeamInput
    assert payloads.RedTeamResult is red_team_gate.RedTeamResult
    assert payloads.REDTEAM_BLOCKED is red_team_gate.REDTEAM_BLOCKED
    assert payloads.REDTEAM_SURVIVED is red_team_gate.REDTEAM_SURVIVED
    assert payloads.REDTEAM_NOT_RUN is red_team_gate.REDTEAM_NOT_RUN


def test_configured_adversary_fn_is_invoked() -> None:
    """When an adversary is configured, the helper runs IT — never the default."""
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


def test_ensure_persists_one_not_run_verdict_per_phase(sqlite_session: Session) -> None:
    """A V1 run reaching its gate persists ONE ``not_run`` row for its phase, with the
    phase in its evidence (#174)."""
    ws, task, run = _seed(sqlite_session)

    row, created = ensure_red_team_verdict(sqlite_session, ws, workflow_run_id=run, task_id=task)
    assert created is True
    assert row.verdict == REDTEAM_NOT_RUN
    assert row.kind == "not_run"
    assert row.evidence == {"reason": "no_candidate_diff", "phase": "spec"}
    assert row.adversary_model is None
    assert row.coder_model is None
    assert row.workflow_run_id == run

    # Idempotent: the gate re-entered (block -> changes -> resubmit) does not rescan.
    again, created_again = ensure_red_team_verdict(
        sqlite_session, ws, workflow_run_id=run, task_id=task
    )
    assert created_again is False
    assert again.id == row.id

    # Another phase is another scan.
    pr, created_pr = ensure_red_team_verdict(
        sqlite_session, ws, workflow_run_id=run, task_id=task, phase="pr"
    )
    assert created_pr is True and pr.id != row.id
    assert pr.evidence == {"reason": "red_team_not_configured", "phase": "pr"}
    rows = RedTeamRepository(sqlite_session).get_by_run(ws, run)
    assert len(rows) == 2 and REDTEAM_SURVIVED not in {r.verdict for r in rows}


def test_a_record_made_before_phases_counts_as_the_spec_scan(sqlite_session: Session) -> None:
    """A pre-#174 record has no phase in its evidence; it was the run's spec scan."""
    ws, task, run = _seed(sqlite_session)
    legacy = RedTeamRepository(sqlite_session).insert(
        ws, verdict=REDTEAM_SURVIVED, kind="parked", evidence={"parked": True}, workflow_run_id=run
    )

    spec, created = ensure_red_team_verdict(sqlite_session, ws, workflow_run_id=run, task_id=task)
    assert (spec.id, created) == (legacy.id, False)

    _, created_pr = ensure_red_team_verdict(
        sqlite_session, ws, workflow_run_id=run, task_id=task, phase="pr"
    )
    assert created_pr is True


def test_a_pr_scan_is_keyed_on_its_head_commit(sqlite_session: Session) -> None:
    """#283 criterion 6: the same head commit is scanned once; a new head is scanned again."""
    ws, task, run = _seed(sqlite_session)

    def scan(head: str) -> tuple[uuid.UUID, bool]:
        row, created = ensure_red_team_verdict(
            sqlite_session, ws, workflow_run_id=run, task_id=task, phase="pr", head_commit=head
        )
        assert row.evidence["head_commit"] == head
        return row.id, created

    first, created = scan("aaa")
    assert created is True
    assert scan("aaa") == (first, False)
    second, created_again = scan("bbb")
    assert created_again is True and second != first


def test_only_an_adversarys_survive_chains_the_audit_event(sqlite_session: Session) -> None:
    """``not_run`` attests nothing; a survive from a real adversary chains
    ``redteam.survived``."""
    ws, task, run = _seed(sqlite_session)
    ensure_red_team_verdict(sqlite_session, ws, workflow_run_id=run, task_id=task)

    def survive(_: RedTeamInput) -> RedTeamResult:
        return RedTeamResult(
            verdict=REDTEAM_SURVIVED,
            kind="survived",
            adversary_model="gpt-5-heavy",
            coder_model="claude-sonnet-4",
        )

    row = run_and_record_red_team(
        sqlite_session, ws, workflow_run_id=run, task_id=task, red_team_fn=survive
    )

    audit = sqlite_session.scalars(
        select(AuditLog).where(AuditLog.action == REDTEAM_SURVIVED_ACTION)
    ).all()
    assert len(audit) == 1
    assert audit[0].detail_ref == {"table": "red_team_record", "id": str(row.id)}
    assert audit[0].details["kind"] == "survived"


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
    assert verdicts == {REDTEAM_BLOCKED, REDTEAM_NOT_RUN}
