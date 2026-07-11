"""Postgres integration tests for the attestation-table slice (Attested Changesets).

Exercises the real Postgres code paths the SQLite unit tests (``test_models.py``)
cannot: the F39 immutability trigger that blocks UPDATE/DELETE on the append-only
``attestation`` table, and the :class:`~forge_db.attest.repository.AttestationRepository`
insert/get-by-run/list surface. Uses the shared ``pg_engine`` fixture; skips
(parked) without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.attest.repository import AttestationRepository
from forge_db.base import Base
from forge_db.models import AgentRun, Attestation, Project, Task, WorkflowRun, Workspace

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed workspace -> project -> task -> workflow_run + agent_run."""
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
        title="attested changeset task",
    )
    session.add(task)
    session.flush()
    run = WorkflowRun(workspace_id=ws.id, task_id=task.id)
    session.add(run)
    session.flush()
    agent = AgentRun(workspace_id=ws.id, workflow_run_id=run.id, task_id=task.id, model="claude")
    session.add(agent)
    session.flush()
    return ws.id, run.id, agent.id


def _envelope(payload_b64: str = "eyJmb28iOiAiYmFyIn0=") -> dict[str, object]:
    return {
        "payload": payload_b64,
        "payloadType": "application/vnd.in-toto+json",
        "signatures": [{"keyid": "ed25519-v1", "sig": "c2ln"}],
    }


def test_insert_via_repository_and_roundtrip(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, agent_id = _seed(session)
        repo = AttestationRepository(session)
        row = repo.insert(
            ws_id,
            subject_digest="sha256:" + "a" * 64,
            predicate_type="https://forge.dev/attestations/changeset/v1",
            envelope=_envelope(),
            payload_hash="b" * 64,
            keyid="ed25519-v1",
            workflow_run_id=run_id,
            agent_run_id=agent_id,
            pr_numbers=[101, 102],
            spec_key="spec-abc",
            spec_version=3,
        )
        session.commit()
        row_id = row.id

    with factory() as session:
        loaded = session.get(Attestation, row_id)
        assert loaded is not None
        assert loaded.subject_digest == "sha256:" + "a" * 64
        assert loaded.predicate_type == "https://forge.dev/attestations/changeset/v1"
        assert loaded.envelope["payloadType"] == "application/vnd.in-toto+json"
        assert loaded.payload_hash == "b" * 64
        assert loaded.keyid == "ed25519-v1"
        assert loaded.workflow_run_id == run_id
        assert loaded.agent_run_id == agent_id
        assert loaded.pr_numbers == [101, 102]
        assert loaded.spec_key == "spec-abc"
        assert loaded.spec_version == 3
        assert loaded.audit_seq is None
        assert loaded.merkle_leaf_hash is None
        assert isinstance(loaded.created_at, object)


def test_insert_defaults_pr_numbers_to_empty_list(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _run_id, _agent_id = _seed(session)
        row = AttestationRepository(session).insert(
            ws_id,
            subject_digest="sha256:" + "c" * 64,
            predicate_type="https://forge.dev/attestations/changeset/v1",
            envelope=_envelope(),
            payload_hash="d" * 64,
            keyid="ed25519-v1",
        )
        session.commit()
        assert row.pr_numbers == []
        assert row.workflow_run_id is None
        assert row.agent_run_id is None


def test_get_by_run_returns_newest_first(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, agent_id = _seed(session)
        repo = AttestationRepository(session)
        first = repo.insert(
            ws_id,
            subject_digest="sha256:" + "1" * 64,
            predicate_type="p",
            envelope=_envelope(),
            payload_hash="1" * 64,
            keyid="k1",
            workflow_run_id=run_id,
            agent_run_id=agent_id,
        )
        session.commit()
        second = repo.insert(
            ws_id,
            subject_digest="sha256:" + "2" * 64,
            predicate_type="p",
            envelope=_envelope(),
            payload_hash="2" * 64,
            keyid="k1",
            workflow_run_id=run_id,
            agent_run_id=agent_id,
        )
        session.commit()

        by_workflow = repo.get_by_run(ws_id, workflow_run_id=run_id)
        assert by_workflow is not None
        assert by_workflow.id == second.id

        by_agent = repo.get_by_run(ws_id, agent_run_id=agent_id)
        assert by_agent is not None
        assert by_agent.id == second.id
        assert first.id != second.id


def test_get_by_run_requires_a_run_id(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _run_id, _agent_id = _seed(session)
        with pytest.raises(ValueError, match="workflow_run_id or agent_run_id"):
            AttestationRepository(session).get_by_run(ws_id)


def test_get_by_run_is_workspace_isolated(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, agent_id = _seed(session)
        other_ws_id, _other_run_id, _other_agent_id = _seed(session)
        AttestationRepository(session).insert(
            ws_id,
            subject_digest="sha256:" + "3" * 64,
            predicate_type="p",
            envelope=_envelope(),
            payload_hash="3" * 64,
            keyid="k1",
            workflow_run_id=run_id,
            agent_run_id=agent_id,
        )
        session.commit()

        found = AttestationRepository(session).get_by_run(other_ws_id, workflow_run_id=run_id)
        assert found is None


def test_list_filters_by_predicate_type_and_spec_key(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, _agent_id = _seed(session)
        repo = AttestationRepository(session)
        repo.insert(
            ws_id,
            subject_digest="sha256:" + "4" * 64,
            predicate_type="changeset/v1",
            envelope=_envelope(),
            payload_hash="4" * 64,
            keyid="k1",
            workflow_run_id=run_id,
            spec_key="spec-a",
        )
        # Commit between inserts: Postgres `now()` is transaction-start time, so
        # two inserts in one transaction would tie on `created_at`.
        session.commit()
        repo.insert(
            ws_id,
            subject_digest="sha256:" + "5" * 64,
            predicate_type="release/v1",
            envelope=_envelope(),
            payload_hash="5" * 64,
            keyid="k1",
            workflow_run_id=run_id,
            spec_key="spec-b",
        )
        session.commit()

        changeset_only = repo.list(ws_id, predicate_type="changeset/v1")
        assert [a.spec_key for a in changeset_only] == ["spec-a"]

        spec_b_only = repo.list(ws_id, spec_key="spec-b")
        assert [a.predicate_type for a in spec_b_only] == ["release/v1"]

        everything = repo.list(ws_id, workflow_run_id=run_id)
        assert len(everything) == 2
        # newest first
        assert everything[0].subject_digest == "sha256:" + "5" * 64


def test_attestation_is_immutable(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, agent_id = _seed(session)
        row = AttestationRepository(session).insert(
            ws_id,
            subject_digest="sha256:" + "6" * 64,
            predicate_type="p",
            envelope=_envelope(),
            payload_hash="6" * 64,
            keyid="k1",
            workflow_run_id=run_id,
            agent_run_id=agent_id,
        )
        session.commit()
        row_id = row.id

    # A direct UPDATE is blocked by the immutability trigger.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(
                update(Attestation).where(Attestation.id == row_id).values(keyid="tampered")
            )
            session.commit()
        session.rollback()

    # A direct DELETE is likewise blocked.
    with factory() as session:
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.execute(text("DELETE FROM attestation WHERE id = :i"), {"i": str(row_id)})
            session.commit()
        session.rollback()


def test_deleting_a_linked_run_is_blocked_by_immutability(factory: sessionmaker[Session]) -> None:
    """``ON DELETE CASCADE`` would DELETE the attestation row; the append-only
    trigger intercepts that DELETE just like a direct one (see the model
    docstring's note on the CASCADE-vs-immutability tension) — so a run that
    has been attested cannot be hard-deleted at all, which is the intended
    fail-closed behavior for a compliance record."""
    with factory() as session:
        ws_id, run_id, agent_id = _seed(session)
        AttestationRepository(session).insert(
            ws_id,
            subject_digest="sha256:" + "7" * 64,
            predicate_type="p",
            envelope=_envelope(),
            payload_hash="7" * 64,
            keyid="k1",
            workflow_run_id=run_id,
            agent_run_id=agent_id,
        )
        session.commit()

    with factory() as session:
        run = session.get(WorkflowRun, run_id)
        assert run is not None
        session.delete(run)
        with pytest.raises((ProgrammingError, IntegrityError)):
            session.commit()
        session.rollback()
