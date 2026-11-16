"""Migration 0047: one non-terminal Temporal run per task (#235)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import scf_db.models as models

DB_ROOT = Path(__file__).resolve().parent.parent
INDEX = "uq_workflow_run_active_task"
BEFORE = "0046_platform_api_key_scopes"


def test_upgrade_keeps_the_oldest_active_temporal_run_of_each_task(tmp_path: Path) -> None:
    """A database that already holds duplicates takes the index: the oldest active
    Temporal run of each task stays, later ones are cancelled, an equal ``created_at``
    falls to the lower id, and terminal and V1 runs are untouched.

    (The metadata-driven baseline provisions the index on a fresh chain, so the
    test drops it to stand in for a database created before 0047.)"""
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'forge_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)

    def _day(day: int) -> datetime:
        return datetime(2026, 1, day, tzinfo=UTC)

    try:
        command.upgrade(cfg, BEFORE)
        with Session(engine) as session:
            session.execute(text(f"DROP INDEX {INDEX}"))
            ws = models.Workspace(name="Acme", slug="acme")
            session.add(ws)
            session.flush()
            project = models.Project(workspace_id=ws.id, name="Core", key="CORE")
            session.add(project)
            session.flush()
            task = models.Task(workspace_id=ws.id, project_id=project.id, key="CORE-1", title="T")
            tied = models.Task(workspace_id=ws.id, project_id=project.id, key="CORE-2", title="U")
            session.add_all([task, tied])
            session.flush()

            def _run(
                name: str, task_id: uuid.UUID, status: str, created_at: datetime, **kw: object
            ) -> models.WorkflowRun:
                return models.WorkflowRun(
                    workspace_id=ws.id,
                    task_id=task_id,
                    workflow_name=name,
                    status=status,
                    engine_backend=kw.pop("engine_backend", "temporal"),
                    created_at=created_at,
                    **kw,
                )

            session.add_all(
                [
                    _run("newer", task.id, "running", _day(2)),
                    _run("oldest", task.id, "running", _day(1)),
                    _run("newest", task.id, "escalated", _day(3)),
                    _run("done", task.id, "succeeded", _day(4)),
                    _run("v1-a", task.id, "running", _day(1), engine_backend="postgres_fsm"),
                    _run("v1-b", task.id, "running", _day(2), engine_backend="postgres_fsm"),
                    # One created_at: only the id can choose; the higher id is inserted first.
                    _run("tie-high", tied.id, "running", _day(5), id=uuid.UUID(int=2)),
                    _run("tie-low", tied.id, "running", _day(5), id=uuid.UUID(int=1)),
                ]
            )
            session.commit()

        command.upgrade(cfg, "head")

        unique = {i["name"] for i in inspect(engine).get_indexes("workflow_run") if i["unique"]}
        assert INDEX in unique
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT workflow_name, status, completed_at FROM workflow_run")
            )
            after = {name: (status, ended is not None) for name, status, ended in rows}
        assert after == {
            "oldest": ("running", False),
            "newer": ("cancelled", True),
            "newest": ("cancelled", True),
            "done": ("succeeded", False),
            "v1-a": ("running", False),
            "v1-b": ("running", False),
            "tie-low": ("running", False),
            "tie-high": ("cancelled", True),
        }

        command.downgrade(cfg, BEFORE)
        assert INDEX not in {i["name"] for i in inspect(engine).get_indexes("workflow_run")}
        command.upgrade(cfg, "head")
        assert INDEX in {i["name"] for i in inspect(engine).get_indexes("workflow_run")}
    finally:
        engine.dispose()
