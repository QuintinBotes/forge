"""The run executor setting, and a launched run's launch, close-out and reconciliation (#239).

Against a fake executor held in memory and SQLite: no Docker, cluster or broker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import scf_worker.agent_runner as agent_runner_module
import scf_worker.launched_runs as launched_runs
import scf_worker.multi_agent as multi_agent
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_db.base import Base
from scf_db.models import AgentRun, AgentRunEvent, Workspace
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_worker.agent_runner import run_agent_task
from scf_worker.beat import BEAT_SCHEDULE, RUN_RECONCILE_TASK, run_reconcile_seconds
from scf_worker.celery_app import _init_run_executor, celery_app
from scf_worker.launched_runs import (
    CLOSE_OUT_TASK,
    EXECUTOR_LOST,
    RECONCILE_GRACE_ENV,
    WALL_CLOCK_EXCEEDED,
    close_out_task,
    reconcile_launched_runs,
)
from scf_worker.run_executor import (
    RUN_EXECUTOR_ENV,
    SANDBOX_BACKEND_ENV,
    ExecutorUnavailable,
    UnitState,
    check_run_executor,
)
from scf_worker.run_record import EXECUTOR_KEY, HANDOFF_KEY, InProcessRunRecord

WS = uuid.UUID("00000000-0000-0000-0000-000000000239")
#: A credential a unit may hold, and the one it must never get.
MODEL_KEY = "sk-" + "m" * 40
DATABASE_URL = "postgresql://scf:db-password-must-not-leak@db/scf"


@dataclass
class FakeExecutor:
    """A :class:`~scf_worker.run_executor.RunExecutor` held in memory."""

    name: str = "docker"
    refuse: str | None = None  # "preflight" or "launch"
    units: dict[str, UnitState] = field(default_factory=dict)
    launched: list[dict[str, Any]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def preflight(self) -> None:
        if self.refuse == "preflight":
            raise ExecutorUnavailable("daemon unreachable")

    def launch(self, run_id: uuid.UUID, workspace_id: uuid.UUID, *, env: Mapping[str, str]) -> str:
        if self.refuse == "launch":
            raise RuntimeError("image pull failed")
        unit = f"unit-{len(self.launched)}"
        self.launched.append({"run_id": run_id, "workspace_id": workspace_id, "env": dict(env)})
        self.units[unit] = UnitState("running")
        return unit

    def state(self, unit: str) -> UnitState:
        return self.units.get(unit, UnitState("missing"))

    def remove(self, unit: str) -> None:
        self.removed.append(unit)
        self.units.pop(unit, None)

    def list_units(self) -> dict[str, uuid.UUID]:
        return {f"unit-{i}": launch["run_id"] for i, launch in enumerate(self.launched)}


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    for module in (agent_runner_module, launched_runs):
        monkeypatch.setattr(module, "create_session_factory", lambda: session_factory)
    for name in ("FORGE_MODEL_PROVIDER", "FORGE_RECORD_RUNS"):
        monkeypatch.delenv(name, raising=False)
    with session_factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
    yield session_factory
    engine.dispose()


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> FakeExecutor:
    fake = FakeExecutor()
    monkeypatch.setenv(RUN_EXECUTOR_ENV, "docker")
    monkeypatch.setattr(launched_runs, "build_executor", lambda _name: fake)
    return fake


@pytest.fixture
def no_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the worker must not run the loop of a launched run")

    monkeypatch.setattr(agent_runner_module, "execute_run", forbidden)
    monkeypatch.setattr(agent_runner_module, "run_objective", forbidden)
    monkeypatch.setattr(multi_agent, "run_supervised_objective", forbidden)


@pytest.fixture(autouse=True)
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def send_task(name: str, args: Any = (), **_kw: Any) -> None:
        assert name == "audit.record"
        sent.append(args[0])

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return sent


def _seed(factory: sessionmaker[Session]) -> tuple[uuid.UUID, dict[str, Any]]:
    """A pending run whose stored inputs are its objective, as the API writes them."""
    run_id = uuid.uuid4()
    context = {"agent_run_id": str(run_id), "workspace_id": str(WS)}
    inputs = AgentObjective(objective="Add a health endpoint", context=context).model_dump(
        mode="json"
    )
    with factory() as s:
        s.add(AgentRun(id=run_id, workspace_id=WS, status=DbRunStatus.PENDING, inputs=inputs))
        s.commit()
    return run_id, inputs


def _hand_off(factory: sessionmaker[Session], run_id: uuid.UUID, **fields: Any) -> None:
    """What the unit's run process records before it exits."""
    record = InProcessRunRecord(run_id, WS, session_factory=factory, redact=lambda text: text)
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED, confidence=0.8, **fields)
    assert record.record_handoff(result)


def _row(factory: sessionmaker[Session], run_id: uuid.UUID) -> AgentRun:
    with factory() as s:
        row = s.get(AgentRun, run_id)
        assert row is not None
        return row


def _types(factory: sessionmaker[Session], run_id: uuid.UUID) -> list[str]:
    with factory() as s:
        events = s.scalars(
            select(AgentRunEvent).where(AgentRunEvent.run_id == run_id).order_by(AgentRunEvent.seq)
        )
        return [e.type for e in events]


def _launched_at(factory: sessionmaker[Session], run_id: uuid.UUID) -> datetime:
    return datetime.fromisoformat(_row(factory, run_id).output[EXECUTOR_KEY]["launched_at"])


# --------------------------------------------------------------------------- #
# Criterion 1: the setting                                                    #
# --------------------------------------------------------------------------- #


def test_an_unknown_executor_stops_the_worker_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUN_EXECUTOR_ENV, "podman")

    with pytest.raises(SystemExit, match=f"{RUN_EXECUTOR_ENV} must be one of"):
        _init_run_executor()


@pytest.mark.parametrize("value", [None, "inline"])
def test_inline_or_unset_runs_the_loop_in_the_worker_as_before(
    value: str | None, factory, executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value is None:
        monkeypatch.delenv(RUN_EXECUTOR_ENV)
    else:
        monkeypatch.setenv(RUN_EXECUTOR_ENV, value)
    run_id, inputs = _seed(factory)

    returned = run_agent_task(inputs)

    assert returned["status"] == "succeeded" and executor.launched == []
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.SUCCEEDED and EXECUTOR_KEY not in row.output
    assert _types(factory, run_id)[-1] == "thread.completed"
    _init_run_executor()  # starts


# --------------------------------------------------------------------------- #
# Criterion 2: launch                                                         #
# --------------------------------------------------------------------------- #


def test_a_run_is_prepared_launched_recorded_and_not_awaited(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_MODEL_API_KEY", MODEL_KEY)
    monkeypatch.setenv("FORGE_DATABASE_URL", DATABASE_URL)
    run_id, inputs = _seed(factory)

    returned = run_agent_task(inputs)

    assert returned["launched"] is True and returned["unit"] == "unit-0"
    [launch] = executor.launched
    assert (launch["run_id"], launch["workspace_id"]) == (run_id, WS)
    assert launch["env"]["FORGE_MODEL_API_KEY"] == MODEL_KEY
    assert not {"FORGE_DATABASE_URL", "FORGE_SECRET_KEY"} & set(launch["env"])
    assert DATABASE_URL not in repr(launch["env"])
    row = _row(factory, run_id)
    assert row.output[EXECUTOR_KEY] == {
        "name": "docker",
        "unit": "unit-0",
        "launched_at": returned["launched_at"],
    }
    assert row.started_at is not None  # prepared in the worker
    assert row.status is DbRunStatus.PENDING and row.completed_at is None
    assert _types(factory, run_id) == []


def test_a_duplicate_delivery_launches_nothing(factory, executor, no_loop) -> None:
    run_id, inputs = _seed(factory)

    first = run_agent_task(inputs)
    second = run_agent_task(inputs)

    assert first["launched"] is True
    assert second == {"launched": False, "run_id": str(run_id)}
    assert len(executor.launched) == 1


def test_a_delivery_that_loses_the_race_to_the_row_lock_launches_nothing(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, inputs = _seed(factory)
    prepare = launched_runs.prepare_run

    def prepare_while_another_delivery_launches(parsed: AgentObjective) -> Any:
        prepared = prepare(parsed)
        with factory() as s:
            row = s.get(AgentRun, run_id)
            row.output = {EXECUTOR_KEY: {"name": "docker", "unit": "theirs", "launched_at": "t"}}
            s.commit()
        return prepared

    monkeypatch.setattr(launched_runs, "prepare_run", prepare_while_another_delivery_launches)

    assert run_agent_task(inputs) == {"launched": False, "run_id": str(run_id)}
    assert executor.launched == []
    assert _row(factory, run_id).output[EXECUTOR_KEY]["unit"] == "theirs"


def test_a_run_with_no_stored_record_fails_without_a_launch(factory, executor, no_loop) -> None:
    inputs = AgentObjective(objective="x", context={"workspace_id": str(WS)}).model_dump(
        mode="json"
    )

    returned = run_agent_task(inputs)

    assert returned["status"] == "failed" and "docker run executor" in returned["error"]
    assert executor.launched == []


# --------------------------------------------------------------------------- #
# Criterion 3: close-out                                                      #
# --------------------------------------------------------------------------- #


def test_close_out_performs_each_step_once_then_removes_the_unit(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    calls: list[str] = []
    racing: list[Any] = []
    record_outcome = agent_runner_module._record_run_outcome

    def deliver(_objective: Any, result: AgentRunResult, *, branch: str, **_kw: Any) -> None:
        # A retried task and the reconciler arrive while this close-out delivers.
        racing.append(close_out_task(str(run_id), str(WS)))
        racing.append(reconcile_launched_runs())
        calls.append(f"deliver {branch}")
        result.artifacts["pull_request"] = {"status": "opened", "number": 7, "url": "u"}

    def outcome(*args: Any, **kwargs: Any) -> None:
        calls.append("outcome")
        record_outcome(*args, **kwargs)

    monkeypatch.setattr(agent_runner_module, "_finalize", deliver)
    monkeypatch.setattr(agent_runner_module, "_record_run_outcome", outcome)
    monkeypatch.setattr(
        agent_runner_module, "_maybe_tracker_writeback", lambda *a, **kw: calls.append("write-back")
    )
    _hand_off(factory, run_id, artifacts={"branch": "scf/run"})
    executor.units["unit-0"] = UnitState("exited", 0)

    first = close_out_task(str(run_id), str(WS))
    second = close_out_task(str(run_id), str(WS))

    assert first == {"closed_out": True, "run_id": str(run_id), "status": "succeeded"}
    assert second == racing[0] == {"closed_out": False, "run_id": str(run_id)}
    assert racing[1] == {}
    assert calls == ["deliver scf/run", "outcome", "write-back"]
    assert _types(factory, run_id).count("thread.completed") == 1
    assert executor.removed == ["unit-0"]
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.SUCCEEDED and row.completed_at is not None
    assert row.output["artifacts"]["pull_request"]["number"] == 7
    assert HANDOFF_KEY not in row.output
    assert (
        row.output[EXECUTOR_KEY]["unit"] == "unit-0" and row.output[EXECUTOR_KEY]["closed_out_at"]
    )
    # A delivery after close-out launches nothing.
    assert run_agent_task(inputs)["launched"] is False and len(executor.launched) == 1


def test_close_out_without_a_handoff_leaves_the_run_to_the_reconciler(
    factory, executor, no_loop
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)

    assert close_out_task(str(run_id), str(WS)) == {"closed_out": False, "run_id": str(run_id)}

    row = _row(factory, run_id)
    assert row.completed_at is None and "closed_out_at" not in row.output[EXECUTOR_KEY]
    assert executor.removed == []


# --------------------------------------------------------------------------- #
# Criterion 4: reconciliation                                                 #
# --------------------------------------------------------------------------- #


def test_the_reconciler_closes_out_an_exited_unit_from_its_handoff(
    factory, executor, no_loop
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    _hand_off(factory, run_id)
    assert reconcile_launched_runs() == {}  # still running
    executor.units["unit-0"] = UnitState("exited", 0)

    assert reconcile_launched_runs() == {"handoff": 1}

    row = _row(factory, run_id)
    assert row.status is DbRunStatus.SUCCEEDED and row.confidence == 0.8
    assert executor.removed == ["unit-0"]


@pytest.mark.parametrize("state", [UnitState("exited", 137), None], ids=["exited", "missing"])
def test_a_unit_gone_without_a_handoff_fails_its_run_as_lost(
    state: UnitState | None, factory, executor, no_loop
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    if state is None:
        executor.units.clear()
    else:
        executor.units["unit-0"] = state

    assert reconcile_launched_runs() == {EXECUTOR_LOST: 1}
    assert reconcile_launched_runs() == {}

    row = _row(factory, run_id)
    assert (row.status, row.output["error"]) == (DbRunStatus.FAILED, EXECUTOR_LOST)
    assert _types(factory, run_id) == ["thread.failed"]


def test_a_unit_past_its_wall_clock_limit_and_grace_is_stopped_and_escalated(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_RUN_TIME_LIMIT", "600")
    monkeypatch.setenv(RECONCILE_GRACE_ENV, "60")
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    launched_at = _launched_at(factory, run_id)

    assert reconcile_launched_runs(now=launched_at + timedelta(seconds=660)) == {}
    assert executor.removed == [] and _row(factory, run_id).completed_at is None

    closed = reconcile_launched_runs(now=launched_at + timedelta(seconds=661))

    assert closed == {WALL_CLOCK_EXCEEDED: 1}
    assert executor.removed[0] == "unit-0" and "unit-0" not in executor.units
    row = _row(factory, run_id)
    assert (row.status, row.output["error"]) == (DbRunStatus.ESCALATED, WALL_CLOCK_EXCEEDED)


def test_an_unreachable_runtime_decides_nothing(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)

    def unreachable(_unit: str) -> UnitState:
        raise ExecutorUnavailable("daemon unreachable")

    monkeypatch.setattr(executor, "state", unreachable)

    assert reconcile_launched_runs() == {}
    row = _row(factory, run_id)
    assert row.completed_at is None and "closed_out_at" not in row.output[EXECUTOR_KEY]


# --------------------------------------------------------------------------- #
# Criterion 5: no inline fallback                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("refuse", ["preflight", "launch"])
def test_a_failed_preflight_or_launch_fails_the_run_naming_the_executor(
    refuse: str, factory, executor, no_loop
) -> None:
    executor.refuse = refuse
    run_id, inputs = _seed(factory)

    returned = run_agent_task(inputs)

    assert returned["status"] == "failed"
    assert returned["error"].startswith("the docker run executor could not launch the run")
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.FAILED and row.output["error"] == returned["error"]
    assert _types(factory, run_id) == ["thread.failed"]
    assert executor.launched == [] and reconcile_launched_runs() == {}


@pytest.mark.parametrize("name", ["docker", "kubernetes"])
def test_docker_and_kubernetes_fail_every_run_until_they_are_built(
    name: str, factory, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_EXECUTOR_ENV, name)
    run_id, inputs = _seed(factory)

    returned = run_agent_task(inputs)

    assert returned["status"] == "failed"
    assert f"the {name} run executor is not available" in returned["error"]
    assert _row(factory, run_id).status is DbRunStatus.FAILED


# --------------------------------------------------------------------------- #
# Criterion 6: combinations the worker refuses                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({RUN_EXECUTOR_ENV: "docker"}, "FORGE_SANDBOX_KIND is worktree"),  # the default tier
        ({RUN_EXECUTOR_ENV: "kubernetes", "FORGE_SANDBOX_KIND": "worktree"}, "worktree"),
        ({RUN_EXECUTOR_ENV: "kubernetes", "FORGE_SANDBOX_KIND": "container"}, "different"),
        (
            {
                RUN_EXECUTOR_ENV: "docker",
                "FORGE_SANDBOX_KIND": "gvisor",
                SANDBOX_BACKEND_ENV: "kubernetes",
            },
            "different platforms",
        ),
    ],
)
def test_the_worker_refuses_an_executor_its_sandbox_cannot_serve(
    env: dict[str, str], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (RUN_EXECUTOR_ENV, "FORGE_SANDBOX_KIND", SANDBOX_BACKEND_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit, match=message):
        _init_run_executor()


@pytest.mark.parametrize(
    "env",
    [
        {},
        {RUN_EXECUTOR_ENV: "inline", "FORGE_SANDBOX_KIND": "worktree"},
        {RUN_EXECUTOR_ENV: "docker", "FORGE_SANDBOX_KIND": "container"},
        {
            RUN_EXECUTOR_ENV: "docker",
            "FORGE_SANDBOX_KIND": "microvm",
            SANDBOX_BACKEND_ENV: "docker",
        },
        {
            RUN_EXECUTOR_ENV: "kubernetes",
            "FORGE_SANDBOX_KIND": "container",
            SANDBOX_BACKEND_ENV: "kubernetes",
        },
    ],
)
def test_an_executor_on_its_sandbox_platform_starts(env: dict[str, str]) -> None:
    assert check_run_executor(env) == env.get(RUN_EXECUTOR_ENV, "inline")


# --------------------------------------------------------------------------- #
# Criterion 7: audit                                                          #
# --------------------------------------------------------------------------- #


def test_launch_and_close_out_each_send_one_audit_event_without_environment_values(
    factory, executor, no_loop, audits, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_MODEL_API_KEY", MODEL_KEY)
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    _hand_off(factory, run_id)
    executor.units["unit-0"] = UnitState("exited", 0)
    close_out_task(str(run_id), str(WS))
    close_out_task(str(run_id), str(WS))

    assert [event["action"] for event in audits] == ["agent_run.launched", "agent_run.closed_out"]
    for event, outcome in zip(audits, ["launched", "succeeded"], strict=True):
        assert (event["workspace_id"], event["target_id"]) == (str(WS), str(run_id))
        assert event["details"] == {"executor": "docker", "unit": "unit-0", "outcome": outcome}
    assert executor.launched[0]["env"]["FORGE_MODEL_API_KEY"] == MODEL_KEY
    assert MODEL_KEY not in repr(audits) and "FORGE_" not in repr(audits)


def test_a_failed_launch_is_audited_as_an_error(factory, executor, no_loop, audits) -> None:
    executor.refuse = "launch"
    _, inputs = _seed(factory)
    run_agent_task(inputs)

    assert [(e["action"], e["result"], e["details"]["outcome"]) for e in audits] == [
        ("agent_run.launched", "error", "failed"),
        ("agent_run.closed_out", "error", "failed"),
    ]


def test_an_audit_failure_does_not_fail_the_run(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broker_down(*_a: Any, **_kw: Any) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(celery_app, "send_task", broker_down)
    run_id, inputs = _seed(factory)

    assert run_agent_task(inputs)["launched"] is True
    _hand_off(factory, run_id)
    executor.units["unit-0"] = UnitState("exited", 0)
    assert close_out_task(str(run_id), str(WS))["status"] == "succeeded"
    assert _row(factory, run_id).status is DbRunStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# Wiring                                                                      #
# --------------------------------------------------------------------------- #


def test_close_out_and_the_reconciler_are_registered_and_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert {CLOSE_OUT_TASK, RUN_RECONCILE_TASK} <= set(celery_app.tasks)
    assert BEAT_SCHEDULE["agent-reconcile-launched-runs"]["task"] == RUN_RECONCILE_TASK
    monkeypatch.setenv("FORGE_RUN_RECONCILE_INTERVAL_SECONDS", "15")
    assert run_reconcile_seconds() == 15.0
