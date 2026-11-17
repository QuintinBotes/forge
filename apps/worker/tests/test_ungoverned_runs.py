"""An agent run with no workflow run is labelled ungoverned, and runs only in
development when it changes a repository (#277, owner decision of 2026-11-15)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from scf_api.settings import get_settings
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_worker import agent_runner as agent_runner_module
from scf_worker import multi_agent
from scf_worker.agent_runner import run_agent_task

REPO_OBJECTIVE: dict[str, Any] = {
    "objective": "Add a health endpoint",
    "repo_targets": [{"repo": "acme/widgets"}],
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    def _set(name: str) -> None:
        monkeypatch.setenv("FORGE_ENVIRONMENT", name)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def _never(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("no agent may run for a refused objective")


@pytest.mark.parametrize(
    "payload",
    [REPO_OBJECTIVE, {**REPO_OBJECTIVE, "execution_mode": "supervised_multi_agent"}],
    ids=["single", "supervised"],
)
def test_outside_development_a_repository_run_is_refused_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, environment: Callable[[str], None], payload: dict[str, Any]
) -> None:
    """Criteria 2 and 4: the worker refuses too, whatever enqueued the run."""
    environment("production")
    monkeypatch.setattr(agent_runner_module, "resolve_run_repository", _never)
    monkeypatch.setattr(agent_runner_module, "build_agent_runner", _never)
    monkeypatch.setattr(multi_agent, "run_supervised_objective", _never)

    result = run_agent_task(payload)

    assert result["status"] == RunStatus.FAILED.value
    assert result["error"].startswith("ungoverned_run_refused:")
    assert result["artifacts"]["governance"] == "none"


def test_a_run_with_no_workflow_run_is_labelled_ungoverned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 3: the run's artifacts state ``governance: none``."""
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)

    result = run_agent_task({"objective": "do the thing"})

    assert result["artifacts"]["governance"] == "none"


def test_a_run_inside_a_workflow_run_is_not_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    objective = AgentObjective(
        objective="do the thing", context={"workflow_run_id": str(uuid.uuid4())}
    )

    result, _ = agent_runner_module.run_agent_objective(objective, deliver=False)

    assert "governance" not in result.artifacts


def test_a_supervised_run_is_labelled_ungoverned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 4: supervised runs stay on their path and are marked ``governance: none``."""

    class _Supervisor:
        def __init__(self, _deps: Any) -> None:
            pass

        def run(self, objective: AgentObjective) -> AgentRunResult:
            return AgentRunResult(run_id=uuid.uuid4(), status=RunStatus.SUCCEEDED)

    monkeypatch.setattr(multi_agent, "Supervisor", _Supervisor)
    monkeypatch.setattr(multi_agent, "_resolve_sub_agent_sink", lambda _o: (object(), None))

    result = multi_agent.run_supervised_objective(
        {"objective": "Implement it", "execution_mode": "supervised_multi_agent"}
    )

    assert result.artifacts["governance"] == "none"


def test_a_supervised_runs_label_reaches_the_record_the_api_reads() -> None:
    """Criterion 4 (#277 review): the parent run's row keeps the label under
    ``artifacts``, where ``GET /agent/runs/{id}`` reads it."""
    from sqlalchemy import StaticPool, create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from scf_api.routers.agent import AgentRunStore
    from scf_db.base import Base
    from scf_db.models import AgentRun, Workspace
    from scf_db.models.enums import RunStatus as DbRunStatus

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    workspace_id, run_id = uuid.uuid4(), uuid.uuid4()
    with factory() as s:
        s.add(Workspace(id=workspace_id, name="Acme", slug="acme"))
        s.flush()
        s.add(AgentRun(id=run_id, workspace_id=workspace_id, inputs={}, status=DbRunStatus.PENDING))
        s.commit()
    result = AgentRunResult(
        run_id=run_id, status=RunStatus.SUCCEEDED, artifacts={"governance": "none"}
    )

    multi_agent._finalize_parent_agent_run(factory, result=result)

    with factory() as s:
        read = AgentRunStore(s).get(run_id, workspace_id=workspace_id)
    assert read is not None and read.artifacts["governance"] == "none"
    engine.dispose()
