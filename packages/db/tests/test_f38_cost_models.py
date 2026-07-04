"""Postgres integration tests for the F38 cost-ledger models (AC2 substance).

Exercises the real constraints: the unique ``(workspace_id, request_id)``
idempotency index, the generated ``total_tokens`` column, workspace CASCADE vs
task SET NULL, enum round-trips, and the 0021 migration's global price seed.
Uses the shared ``pg_engine`` fixture; parks without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    CostEvent,
    CostEventKind,
    ModelPrice,
    Project,
    Task,
    Workspace,
)

pytestmark = pytest.mark.usefixtures("pg_engine")

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


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
    project = Project(workspace_id=ws.id, name="Forge", key=f"F{uuid.uuid4().hex[:5]}")
    session.add(project)
    session.flush()
    task = Task(
        workspace_id=ws.id, project_id=project.id, key=f"F-{uuid.uuid4().hex[:6]}", title="T"
    )
    session.add(task)
    session.flush()
    return ws.id, project.id, task.id


def _event(ws_id: uuid.UUID, task_id: uuid.UUID | None = None, **overrides) -> CostEvent:
    defaults: dict = {
        "workspace_id": ws_id,
        "task_id": task_id,
        "kind": CostEventKind.COMPLETION,
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_usd": Decimal("0.001"),
        "request_id": f"req-{uuid.uuid4().hex[:10]}",
        "occurred_at": NOW,
        "phase": "executing",
    }
    defaults.update(overrides)
    return CostEvent(**defaults)


def test_cost_event_round_trip_generated_column_and_enum(factory) -> None:
    with factory() as session:
        ws_id, _, task_id = _seed(session)
        session.add(_event(ws_id, task_id))
        session.commit()
        row = session.scalars(select(CostEvent)).one()
        assert row.kind is CostEventKind.COMPLETION
        assert row.total_tokens == 150  # GENERATED ALWAYS AS ... STORED
        assert Decimal(row.cost_usd) == Decimal("0.001")


def test_cost_event_unique_workspace_request_id(factory) -> None:
    with factory() as session:
        ws_id, _, _ = _seed(session)
        session.add(_event(ws_id, request_id="dup-1"))
        session.commit()
        session.add(_event(ws_id, request_id="dup-1"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        # Same request_id in a DIFFERENT workspace is fine (per-tenant key).
        ws2 = Workspace(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        session.add(ws2)
        session.flush()
        session.add(_event(ws2.id, request_id="dup-1"))
        session.commit()


def test_cost_event_task_delete_sets_null_workspace_delete_cascades(factory) -> None:
    with factory() as session:
        ws_id, _, task_id = _seed(session)
        session.add(_event(ws_id, task_id))
        session.commit()

        # Deleting the task keeps the billing record (SET NULL).
        session.delete(session.get(Task, task_id))
        session.commit()
        row = session.scalars(select(CostEvent)).one()
        assert row.task_id is None

        # Deleting the workspace removes the tenant's ledger (CASCADE).
        session.delete(session.get(Workspace, ws_id))
        session.commit()
        assert session.scalars(select(CostEvent)).all() == []


def test_model_price_global_and_override_rows(factory) -> None:
    with factory() as session:
        ws_id, _, _ = _seed(session)
        session.add_all(
            [
                ModelPrice(
                    workspace_id=None,
                    provider="anthropic",
                    model="claude-sonnet-4-5",
                    kind=CostEventKind.COMPLETION,
                    prompt_usd_per_1k=Decimal("0.003"),
                    completion_usd_per_1k=Decimal("0.015"),
                    effective_from=NOW,
                ),
                ModelPrice(
                    workspace_id=ws_id,
                    provider="anthropic",
                    model="claude-sonnet-4-5",
                    kind=CostEventKind.COMPLETION,
                    prompt_usd_per_1k=Decimal("0.001"),
                    completion_usd_per_1k=Decimal("0.005"),
                    effective_from=NOW,
                ),
            ]
        )
        session.commit()
        rows = session.scalars(select(ModelPrice)).all()
        assert {r.workspace_id for r in rows} == {None, ws_id}
        assert all(r.currency == "USD" for r in rows)


def test_migration_0021_seeds_global_prices(tmp_path) -> None:
    """The 0021 seed inserts global defaults (workspace_id NULL) exactly once."""
    db_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(db_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(db_root / "migrations"))
    db_file = tmp_path / "f38_seed.db"
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with Session(engine) as session:
            prices = session.scalars(select(ModelPrice)).all()
            assert len(prices) >= 5
            assert all(p.workspace_id is None for p in prices)
            kinds = {p.kind for p in prices}
            assert CostEventKind.COMPLETION in kinds and CostEventKind.EMBEDDING in kinds
    finally:
        engine.dispose()
