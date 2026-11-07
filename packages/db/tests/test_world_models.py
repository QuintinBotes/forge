"""World-model (H1) table guards provable without a live Postgres.

The Postgres lane proves the trigger executes; these prove the DDL at
least compiles for the Postgres dialect (C1), the tombstone CHECK
exists and bites on SQLite (minor 4), the supersedes index exists
(minor 16), and dropping emits the guard function's cleanup (minor 17).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import forge_db.models  # noqa: F401  (registers all models)
from forge_db.base import Base
from forge_db.models.world import WorldFact


def test_world_ddl_compiles_for_postgres() -> None:
    out: list[str] = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *a, **k: out.append(str(sql.compile(dialect=postgresql.dialect()))),
    )
    Base.metadata.create_all(engine, checkfirst=False)
    assert any("forge_world_fact_guard" in statement for statement in out)


def test_world_drop_compiles_function_cleanup_for_postgres() -> None:
    out: list[str] = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *a, **k: out.append(str(sql.compile(dialect=postgresql.dialect()))),
    )
    Base.metadata.drop_all(engine, checkfirst=False)
    assert any("DROP FUNCTION IF EXISTS forge_world_fact_guard" in statement for statement in out)


def test_supersedes_index_exists() -> None:
    names = {index.name for index in WorldFact.__table__.indexes}
    assert "ix_world_fact_supersedes" in names


def _fact_row(**overrides: object) -> WorldFact:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values: dict[str, object] = {
        "workspace_id": uuid.uuid4(),
        "subject_id": uuid.uuid4(),
        "phrase": "based in",
        "predicate_key": None,
        "object_id": None,
        "value": "Berlin",
        "layer": "open",
        "source_kind": "person",
        "actor": "tester",
        "dedup_key": uuid.uuid4().hex,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return WorldFact(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        # A live row carrying a reason.
        {"invalidated_at": None, "invalidated_reason": "retracted"},
        # A tombstoned row with no reason.
        {"invalidated_at": datetime(2026, 1, 2, tzinfo=UTC), "invalidated_reason": None},
        # A reason outside the allowed list.
        {
            "invalidated_at": datetime(2026, 1, 2, tzinfo=UTC),
            "invalidated_reason": "exploded",
        },
    ],
)
def test_tombstone_pair_check_refuses_unpaired_stamps(overrides: dict[str, object]) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(_fact_row(**overrides))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


def test_tombstone_pair_check_accepts_paired_stamp() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(
                _fact_row(
                    invalidated_at=datetime(2026, 1, 2, tzinfo=UTC),
                    invalidated_reason="retracted",
                )
            )
            session.add(_fact_row())
            session.commit()
    finally:
        engine.dispose()
