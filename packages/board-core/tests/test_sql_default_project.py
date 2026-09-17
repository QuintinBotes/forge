"""Default-project resolution for the DB-backed board (hermetic, SQLite).

``task.project_id`` is a required FK, but ``TaskDTO.project_id`` is optional and
the board's own New-task dialog never asks for a project. Under the in-memory
service that mismatch is invisible — nothing enforces the FK — so it only
surfaced once the compose files started selecting the durable backend, as a
``NotNullViolation`` escaping to the client as a 500.

The service now resolves the workspace's default project instead, and says so
plainly when the workspace has none.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_board import SqlAlchemyBoardService
from forge_board.exceptions import NoProjectError
from forge_contracts import TaskDTO
from forge_db.base import Base
from forge_db.models import Project, Workspace


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    Base.metadata.drop_all(engine)


def _workspace(factory: sessionmaker[Session], *projects: str) -> tuple[uuid.UUID, list[uuid.UUID]]:
    ws = uuid.uuid4()
    ids: list[uuid.UUID] = []
    with factory() as session:
        session.add(Workspace(id=ws, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        session.flush()
        for key in projects:
            project_id = uuid.uuid4()
            session.add(Project(id=project_id, workspace_id=ws, name=key, key=key))
            session.flush()
            ids.append(project_id)
        session.commit()
    return ws, ids


def test_a_task_without_a_project_lands_in_the_default_one(
    factory: sessionmaker[Session],
) -> None:
    """The regression: this used to be a NOT NULL violation → HTTP 500."""
    ws, [project_id] = _workspace(factory, "DEMO")
    svc = SqlAlchemyBoardService(factory, ws)

    task = svc.create_task(TaskDTO(title="Filed from the New task dialog"))

    assert task.project_id == project_id
    assert svc.get_task(task.id).project_id == project_id


def test_an_explicit_project_still_wins(factory: sessionmaker[Session]) -> None:
    ws, [_first, second] = _workspace(factory, "AAA", "BBB")
    svc = SqlAlchemyBoardService(factory, ws)

    task = svc.create_task(TaskDTO(title="Filed deliberately", project_id=second))

    assert task.project_id == second


def test_the_default_is_the_oldest_project(factory: sessionmaker[Session]) -> None:
    """Stable and predictable, rather than whichever row the database returns."""
    ws, [first, _second] = _workspace(factory, "AAA", "BBB")
    svc = SqlAlchemyBoardService(factory, ws)

    assert svc.create_task(TaskDTO(title="One")).project_id == first


def test_a_workspace_with_no_project_says_so(factory: sessionmaker[Session]) -> None:
    ws, _ = _workspace(factory)
    svc = SqlAlchemyBoardService(factory, ws)

    with pytest.raises(NoProjectError, match="no project to attach this to"):
        svc.create_task(TaskDTO(title="Nowhere to put this"))


def test_the_default_never_reaches_across_workspaces(
    factory: sessionmaker[Session],
) -> None:
    """A tenant with no project must not inherit another tenant's."""
    _other_ws, _ = _workspace(factory, "OTHER")
    ws, _ = _workspace(factory)
    svc = SqlAlchemyBoardService(factory, ws)

    with pytest.raises(NoProjectError):
        svc.create_task(TaskDTO(title="Nowhere to put this"))
