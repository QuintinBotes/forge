"""Postgres integration tests for the F23 traceability projection models.

Exercises the real Postgres code paths the SQLite unit tests cannot: JSONB list
columns, the ``UNIQUE(spec_id, criterion_ext_id)`` link constraint, and the
``UNIQUE(spec_id)`` rollup constraint. Uses the shared ``pg_engine`` fixture;
parks without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    Project,
    SpecDocument,
    TraceabilityCriterionLink,
    TraceabilitySpecRollup,
    Workspace,
)

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    spec = SpecDocument(
        workspace_id=ws.id,
        project_id=project.id,
        spec_key=f"SPEC-{uuid.uuid4().hex[:4]}",
        name="Customer endpoint",
    )
    session.add(spec)
    session.flush()
    return ws.id, project.id, spec.id


def _link(ws_id, project_id, spec_id, criterion: str) -> TraceabilityCriterionLink:
    return TraceabilityCriterionLink(
        workspace_id=ws_id,
        project_id=project_id,
        spec_id=spec_id,
        spec_key="SPEC-1",
        criterion_ext_id=criterion,
        criterion_text="text",
        requirement_ext_ids=["R1", "R3"],
        status="validated",
        satisfied=True,
        test_refs=["t::a1"],
        diff_refs=["src/x.py"],
        task_ids=["TASK-1"],
        pr_numbers=[42],
        report_spec_version=2,
        current_spec_version=2,
    )


def test_link_jsonb_roundtrip_and_unique(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id, spec_id = _seed(session)
        session.add(_link(ws_id, project_id, spec_id, "A1"))
        session.commit()

        row = session.query(TraceabilityCriterionLink).one()
        assert row.requirement_ext_ids == ["R1", "R3"]
        assert row.pr_numbers == [42]
        assert row.status == "validated"

        # Duplicate (spec_id, criterion_ext_id) is rejected.
        with pytest.raises(IntegrityError):
            session.add(_link(ws_id, project_id, spec_id, "A1"))
            session.commit()
        session.rollback()


def test_rollup_unique_per_spec(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id, spec_id = _seed(session)

        def _rollup() -> TraceabilitySpecRollup:
            return TraceabilitySpecRollup(
                workspace_id=ws_id,
                project_id=project_id,
                spec_id=spec_id,
                spec_key="SPEC-1",
                spec_name="Customer endpoint",
                spec_status="validated",
                total_requirements=2,
                covered_requirements=2,
                total_criteria=3,
                validated_criteria=3,
                failed_criteria=0,
                uncovered_criteria=0,
                claimed_criteria=0,
                stale_criteria=0,
                requirement_coverage=1,
                acceptance_criteria_coverage=1,
                uncovered_requirement_ext_ids=[],
                validation_status="passing",
                gap_count=0,
                projection_version=1,
            )

        session.add(_rollup())
        session.commit()

        stored = session.query(TraceabilitySpecRollup).one()
        assert stored.validation_status == "passing"
        assert stored.projection_version == 1

        with pytest.raises(IntegrityError):
            session.add(_rollup())
            session.commit()
        session.rollback()
