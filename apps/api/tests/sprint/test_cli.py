"""F26 ``forge-cli sprint`` subcommand tests (reconcile + velocity)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

import forge_api.cli as cli
from forge_board.sprint_service import SprintService
from forge_contracts.enums import TaskStatus
from forge_db.base import Base
from forge_db.models import Project, Sprint, Task, Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


@pytest.fixture
def factory(monkeypatch) -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add(Workspace(id=WS, name="Acme", slug="acme"))
        session.flush()
        session.add(Project(id=PROJECT, workspace_id=WS, name="Core", key="CORE"))
        session.flush()
        sprint = Sprint(
            workspace_id=WS,
            project_id=PROJECT,
            name="S1",
            status="planned",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 14),
        )
        session.add(sprint)
        session.flush()
        session.add(
            Task(
                workspace_id=WS,
                project_id=PROJECT,
                key="CORE-1",
                title="t",
                status=TaskStatus.IN_PROGRESS,
                estimate=5,
                sprint_id=sprint.id,
            )
        )
        session.commit()
    monkeypatch.setattr(cli, "_sprint_session_factory", lambda: sf)
    return sf


def test_parser_accepts_sprint_commands() -> None:
    args = cli.build_parser().parse_args(["sprint", "reconcile", str(uuid.uuid4())])
    assert args.group == "sprint" and args.command == "reconcile"
    args = cli.build_parser().parse_args(["sprint", "velocity", str(PROJECT), "--json"])
    assert args.as_json is True


def test_reconcile_command(factory, capsys) -> None:
    sid = (
        SprintService(factory)
        .start(
            workspace_id=WS,
            sprint_id=__sprint_id(factory),
        )
        .id
    )
    assert cli.main(["sprint", "reconcile", str(sid)]) == 0
    assert "reconciled" in capsys.readouterr().out


def test_reconcile_missing_returns_1(factory, capsys) -> None:
    assert cli.main(["sprint", "reconcile", str(uuid.uuid4())]) == 1


def test_velocity_command(factory, capsys) -> None:
    assert cli.main(["sprint", "velocity", str(PROJECT)]) == 0
    assert "velocity for project" in capsys.readouterr().out


def test_velocity_missing_project_returns_1(factory, capsys) -> None:
    assert cli.main(["sprint", "velocity", str(uuid.uuid4())]) == 1


def __sprint_id(factory) -> uuid.UUID:
    from sqlalchemy import select

    with factory() as session:
        return session.execute(select(Sprint.id)).scalar_one()
