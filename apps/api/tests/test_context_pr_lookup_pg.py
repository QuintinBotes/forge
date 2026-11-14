"""The closed-PR → run lookup on real Postgres (JSON path and uuid range queries)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.context_service import resolve_run_for_pull_request
from forge_db.base import Base
from forge_db.models import AgentRun, Workspace

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine: Engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    Base.metadata.drop_all(pg_engine)


def _run(factory, *, number: int | None, repo: str = "org/api") -> uuid.UUID:
    with factory() as session:
        workspace = Workspace(name="W", slug=f"w-{uuid.uuid4().hex[:8]}")
        session.add(workspace)
        session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            inputs={"context": {"repo_full_name": repo}},
            output=(
                {"artifacts": {"pull_request": {"status": "opened", "number": number}}}
                if number is not None
                else None
            ),
        )
        session.add(run)
        session.commit()
        return run.id


def test_recorded_pr_number_and_repo_resolve_on_postgres(factory) -> None:
    wanted = _run(factory, number=7)
    _run(factory, number=8)
    _run(factory, number=7, repo="other/repo")
    with factory() as session:
        found = resolve_run_for_pull_request(
            session, repo_full_name="org/api", number=7, head_ref=None
        )
        assert found is not None and found.id == wanted
        assert (
            resolve_run_for_pull_request(
                session, repo_full_name="org/api", number=99, head_ref=None
            )
            is None
        )


def test_branch_prefix_resolves_through_a_uuid_range_on_postgres(factory) -> None:
    run_id = _run(factory, number=None)
    with factory() as session:
        found = resolve_run_for_pull_request(
            session, repo_full_name="org/api", number=5, head_ref=f"forge/run-{run_id.hex[:12]}"
        )
        assert found is not None and found.id == run_id
        # A delivery without a repository, or from another repository, never matches.
        for repo in (None, "evil/other"):
            assert (
                resolve_run_for_pull_request(
                    session, repo_full_name=repo, number=5, head_ref=f"forge/run-{run_id.hex[:12]}"
                )
                is None
            )
