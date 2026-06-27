"""Postgres integration tests for the F22 multi-repo models.

Exercises the real Postgres code paths: the PRGroup ``workflow_run_id`` unique
constraint, the AgentRepoWorkspace ``(agent_run_id, repo_id)`` unique constraint,
the enum columns (PRGroupStatus / RepoRole), and the FKs to ``workflow_run`` /
``agent_run`` / ``task``. Uses the shared ``pg_engine`` fixture; parks without
Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    AgentRepoWorkspace,
    AgentRun,
    PRGroup,
    Project,
    Task,
    WorkflowRun,
    Workspace,
)
from forge_db.models.enums import PRGroupStatus, RepoRole

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
        title="multi-repo task",
        repo_targets=[
            {"repo": "github.com/org/api", "role": "primary"},
            {
                "repo": "github.com/org/web",
                "role": "secondary",
                "depends_on": ["github.com/org/api"],
            },
        ],
    )
    session.add(task)
    session.flush()
    run = WorkflowRun(workspace_id=ws.id, task_id=task.id)
    session.add(run)
    session.flush()
    agent = AgentRun(workspace_id=ws.id, workflow_run_id=run.id, task_id=task.id)
    session.add(agent)
    session.flush()
    return ws.id, run.id, agent.id


def test_pr_group_roundtrip(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, _agent_id = _seed(session)
        group = PRGroup(
            workspace_id=ws_id,
            workflow_run_id=run_id,
            repo_count=2,
            merge_order=["github.com/org/api", "github.com/org/web"],
            status=PRGroupStatus.OPEN,
            merged_repo_ids=[],
        )
        session.add(group)
        session.commit()
        loaded = session.get(PRGroup, group.id)
        assert loaded is not None
        assert loaded.status is PRGroupStatus.OPEN
        assert loaded.merge_order == ["github.com/org/api", "github.com/org/web"]


def test_pr_group_unique_per_workflow_run(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, run_id, _agent_id = _seed(session)
        session.add(PRGroup(workspace_id=ws_id, workflow_run_id=run_id, repo_count=1))
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(PRGroup(workspace_id=ws_id, workflow_run_id=run_id, repo_count=1))
            session.commit()
        session.rollback()


def test_agent_repo_workspace_roundtrip_and_unique(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _run_id, agent_id = _seed(session)
        session.add(
            AgentRepoWorkspace(
                workspace_id=ws_id,
                agent_run_id=agent_id,
                repo_id="github.com/org/api",
                role=RepoRole.PRIMARY,
                worktree_path="/wt/api",
                branch_name="forge/TASK-401",
                base_branch="main",
                base_commit_sha="a" * 40,
            )
        )
        session.commit()

        # A second worktree for a different repo under the same run is fine.
        session.add(
            AgentRepoWorkspace(
                workspace_id=ws_id,
                agent_run_id=agent_id,
                repo_id="github.com/org/web",
                role=RepoRole.SECONDARY,
                worktree_path="/wt/web",
                branch_name="forge/TASK-401",
                base_branch="main",
                base_commit_sha="b" * 40,
            )
        )
        session.commit()

        # But the same (agent_run, repo) pair is rejected.
        with pytest.raises(IntegrityError):
            session.add(
                AgentRepoWorkspace(
                    workspace_id=ws_id,
                    agent_run_id=agent_id,
                    repo_id="github.com/org/api",
                    role=RepoRole.PRIMARY,
                    worktree_path="/wt/api2",
                    branch_name="forge/TASK-401",
                    base_branch="main",
                    base_commit_sha="c" * 40,
                )
            )
            session.commit()
        session.rollback()
