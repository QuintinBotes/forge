"""Both risk tables reject UPDATE and DELETE on Postgres (#193 criterion 2, spec A §9.6).

Uses the shared ``pg_engine`` fixture; skips (parked) without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from scf_contracts import RiskClass, RiskEvaluation
from scf_db.base import Base
from scf_db.models import Workspace
from scf_db.risk_repository import RiskRepository

pytestmark = pytest.mark.usefixtures("pg_engine")

_REJECTED = (ProgrammingError, IntegrityError, InternalError)


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _rows(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        repo = RiskRepository(session)
        classification = repo.record_classification(
            ws.id,
            RiskEvaluation(derived_class=RiskClass.HIGH, final_class=RiskClass.HIGH),
            commit_sha="a" * 40,
            signals={"paths": ["app/auth/login.py"], "lines_added": 2},
            enforcement_mode="shadow",
        )
        observation = repo.record_gate_observation(
            ws.id,
            classification_id=classification.id,
            gate_name="deploy",
            actual_decision="allow",
            risk_decision="gated",
            agreed=False,
        )
        session.commit()
        return classification.id, observation.id


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("risk_classification", "final_class", "minimal"),
        ("risk_gate_observation", "agreed", True),
    ],
)
def test_update_and_delete_are_rejected(
    factory: sessionmaker[Session], table: str, column: str, value: object
) -> None:
    ids = dict(zip(("risk_classification", "risk_gate_observation"), _rows(factory), strict=True))
    row_id = str(ids[table])

    for statement, params in (
        (f"UPDATE {table} SET {column} = :v WHERE id = :i", {"v": value, "i": row_id}),
        (f"DELETE FROM {table} WHERE id = :i", {"i": row_id}),
    ):
        with factory() as session:
            with pytest.raises(_REJECTED, match="append-only"):
                session.execute(text(statement), params)
                session.commit()
            session.rollback()

    with factory() as session:
        count = session.scalar(text(f"SELECT count(*) FROM {table} WHERE id = :i"), {"i": row_id})
        assert count == 1


def test_an_observation_is_refused_for_another_workspaces_classification(
    factory: sessionmaker[Session],
) -> None:
    classification_id, _ = _rows(factory)
    with factory() as session:
        other = Workspace(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        session.add(other)
        session.flush()
        with pytest.raises(ValueError, match="no risk classification"):
            RiskRepository(session).record_gate_observation(
                other.id,
                classification_id=classification_id,
                gate_name="merge",
                actual_decision="allow",
                risk_decision="allow",
                agreed=True,
            )
