"""The run executor setting, and a launched run's launch, close-out and reconciliation (#239).

Against a fake executor held in memory and SQLite: no Docker, cluster or broker.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import scf_worker.agent_runner as agent_runner_module
import scf_worker.launched_runs as launched_runs
import scf_worker.multi_agent as multi_agent
from scf_api.auth.run_token import RUN_TOKEN_KEY, require_run_token
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_db.base import Base
from scf_db.models import AgentRun, AgentRunEvent, AuditLog, Workspace
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_worker.agent_runner import run_agent_task
from scf_worker.beat import BEAT_SCHEDULE, RUN_RECONCILE_TASK, run_reconcile_seconds
from scf_worker.celery_app import _init_run_executor, celery_app
from scf_worker.launched_runs import (
    CLOSE_OUT_TASK,
    EXECUTOR_LOST,
    EXECUTOR_UNREACHABLE,
    RUN_RECORD_UNREADABLE,
    WALL_CLOCK_EXCEEDED,
    close_out_task,
    reconcile_launched_runs,
)
from scf_worker.run_executor import (
    RECONCILE_GRACE_ENV,
    RUN_EXECUTOR_ENV,
    SANDBOX_BACKEND_ENV,
    ExecutorUnavailable,
    UnitState,
    check_run_executor,
)
from scf_worker.run_main import RUN_API_URL_ENV, RUN_PROCESS_ALLOWED_ENV, RUN_TOKEN_ENV
from scf_worker.run_record import EXECUTOR_KEY, HANDOFF_KEY, InProcessRunRecord

WS = uuid.UUID("00000000-0000-0000-0000-000000000239")
#: A credential a unit may hold, and the one it must never get.
MODEL_KEY = "sk-" + "m" * 40
DATABASE_URL = "postgresql://scf:db-password-must-not-leak@db/scf"
API_URL = "http://scf-api:8000"


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


def _expire_claim(factory: sessionmaker[Session], run_id: uuid.UUID) -> None:
    """Age the run's close-out claim past its lease, as if its worker died long ago."""
    with factory() as s:
        row = s.get(AgentRun, run_id)
        assert row is not None and row.output[EXECUTOR_KEY]["closing_since"]
        record = {**row.output[EXECUTOR_KEY], "closing_since": "2000-01-01T00:00:00+00:00"}
        row.output = {**row.output, EXECUTOR_KEY: record}
        s.commit()


def _killed(*_a: Any, **_kw: Any) -> Any:
    raise SystemExit("the worker was killed")


def _audit_actions(factory: sessionmaker[Session]) -> list[str]:
    """The audit chain in the database (issuing a run token writes there, #256)."""
    with factory() as s:
        return [entry.action for entry in s.scalars(select(AuditLog).order_by(AuditLog.seq))]


def _unit_token(executor: FakeExecutor, unit: int = 0) -> str:
    return executor.launched[unit]["env"][RUN_TOKEN_ENV]


def _refused(factory: sessionmaker[Session], run_id: uuid.UUID, token: str) -> bool:
    """Whether the run-record routes refuse ``token`` on its run now."""
    with factory() as s:
        try:
            require_run_token(run_id, s, f"Bearer {token}")
        except HTTPException as exc:
            assert exc.status_code == 401
            return True
    return False


def _written(factory: sessionmaker[Session], run_id: uuid.UUID) -> str:
    """Everything stored for the run: its row, its events and the audit chain."""
    with factory() as s:
        row = s.get(AgentRun, run_id)
        assert row is not None
        values = {column.name: getattr(row, column.name) for column in AgentRun.__table__.columns}
        events = s.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run_id))
        audit = s.scalars(select(AuditLog))
        return json.dumps(
            [
                values,
                [(e.summary, e.payload) for e in events],
                [(a.details, a.reason, a.actor_label) for a in audit],
            ],
            default=str,
        )


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
    assert "run_token.issued" not in _audit_actions(factory)  # #257: inline holds no token
    _init_run_executor()  # starts


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FORGE_RUN_TIME_LIMIT", "soon"),
        ("FORGE_RUN_TIME_LIMIT", "0"),
        ("FORGE_RUN_TIME_LIMIT", "inf"),
        (RECONCILE_GRACE_ENV, "-1"),
        (RECONCILE_GRACE_ENV, "nan"),
    ],
)
def test_an_unusable_time_setting_stops_the_worker_naming_it(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit, match=name):
        _init_run_executor()


# --------------------------------------------------------------------------- #
# Criterion 2: launch                                                         #
# --------------------------------------------------------------------------- #


def test_a_run_is_prepared_launched_recorded_and_not_awaited(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_MODEL_API_KEY", MODEL_KEY)
    monkeypatch.setenv("FORGE_DATABASE_URL", DATABASE_URL)
    # Neither allowed nor denied by name: the allowlist alone keeps it out.
    monkeypatch.setenv("FORGE_REDIS_URL", "redis://:broker-password@redis:6379/0")
    run_id, inputs = _seed(factory)

    returned = run_agent_task(inputs)

    assert returned["launched"] is True and returned["unit"] == "unit-0"
    [launch] = executor.launched
    assert (launch["run_id"], launch["workspace_id"]) == (run_id, WS)
    assert launch["env"]["FORGE_MODEL_API_KEY"] == MODEL_KEY
    assert set(launch["env"]) <= RUN_PROCESS_ALLOWED_ENV
    assert not {"FORGE_DATABASE_URL", "FORGE_SECRET_KEY", "FORGE_REDIS_URL"} & set(launch["env"])
    assert DATABASE_URL not in repr(launch["env"])
    assert "broker-password" not in repr(launch["env"])
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

    def deliver(_objective: Any, result: AgentRunResult, *, branch: str, **kw: Any) -> None:
        # A retried task and the reconciler arrive while this close-out delivers.
        racing.append(close_out_task(str(run_id), str(WS)))
        racing.append(reconcile_launched_runs())
        # A close-out may be a retry, so it reuses a pull request already opened.
        calls.append(f"deliver {branch} reusing={kw['reuse_existing']}")
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

    assert first == {
        "closed_out": True,
        "run_id": str(run_id),
        "status": "succeeded",
        "reason": "handoff",
    }
    assert second == racing[0] == {"closed_out": False, "run_id": str(run_id)}
    assert racing[1] == {}
    assert calls == ["deliver scf/run reusing=True", "outcome", "write-back"]
    assert _types(factory, run_id).count("thread.completed") == 1
    assert executor.removed == ["unit-0"]
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.SUCCEEDED and row.completed_at is not None
    assert row.output["artifacts"]["pull_request"]["number"] == 7
    assert HANDOFF_KEY not in row.output
    assert (
        row.output[EXECUTOR_KEY]["unit"] == "unit-0" and row.output[EXECUTOR_KEY]["closing_since"]
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
    assert row.completed_at is None and "closing_since" not in row.output[EXECUTOR_KEY]
    assert executor.removed == []


@pytest.mark.parametrize("stage", ["_finalize", "_record_run_outcome"])
def test_a_close_out_killed_after_its_claim_is_retried_once_the_claim_expires(
    stage: str, factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed worker, or the task's hard time limit, stops close-out part way."""
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    delivered: list[bool] = []

    def deliver(_objective: Any, result: AgentRunResult, **kw: Any) -> None:
        delivered.append(kw["reuse_existing"])
        result.artifacts["pull_request"] = {"status": "opened", "number": 7, "url": "u"}

    monkeypatch.setattr(agent_runner_module, "_finalize", deliver)
    _hand_off(factory, run_id, artifacts={"branch": "scf/run"})
    executor.units["unit-0"] = UnitState("exited", 0)
    real = getattr(agent_runner_module, stage)
    monkeypatch.setattr(agent_runner_module, stage, _killed)

    with pytest.raises(SystemExit):
        close_out_task(str(run_id), str(WS))
    monkeypatch.setattr(agent_runner_module, stage, real)

    # While the claim's lease holds, nothing else closes it out.
    assert close_out_task(str(run_id), str(WS))["closed_out"] is False
    assert reconcile_launched_runs() == {}
    assert executor.removed == [] and _row(factory, run_id).completed_at is None
    _expire_claim(factory, run_id)

    assert reconcile_launched_runs() == {"handoff": 1}

    row = _row(factory, run_id)
    assert row.status is DbRunStatus.SUCCEEDED
    assert row.output["artifacts"]["pull_request"]["number"] == 7
    assert executor.removed == ["unit-0"] and delivered and all(delivered)


def test_an_outcome_that_was_not_recorded_fails_the_close_out_until_a_retry_records_it(
    factory, executor, no_loop, audits, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    _hand_off(factory, run_id)
    executor.units["unit-0"] = UnitState("exited", 0)
    real = agent_runner_module._record_run_outcome

    def database_blip(rid: uuid.UUID, wid: uuid.UUID, result: AgentRunResult, **_kw: Any) -> None:
        def unreachable() -> Session:
            raise ConnectionError("database unreachable")

        real(rid, wid, result, session_factory=unreachable)  # logs and swallows it

    monkeypatch.setattr(agent_runner_module, "_record_run_outcome", database_blip)

    assert close_out_task(str(run_id), str(WS)) == {"closed_out": False, "run_id": str(run_id)}

    assert _row(factory, run_id).completed_at is None
    assert executor.removed == []
    assert [event["action"] for event in audits] == ["agent_run.launched"]
    monkeypatch.setattr(agent_runner_module, "_record_run_outcome", real)
    _expire_claim(factory, run_id)
    assert reconcile_launched_runs() == {"handoff": 1}
    assert _row(factory, run_id).status is DbRunStatus.SUCCEEDED
    assert executor.removed == ["unit-0"]


@pytest.mark.parametrize("unreadable", ["handoff", "inputs"])
def test_an_unreadable_run_record_fails_the_run_instead_of_stranding_it(
    unreadable: str, factory, executor, no_loop
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    _hand_off(factory, run_id)
    with factory() as s:
        row = s.get(AgentRun, run_id)
        if unreadable == "handoff":
            row.output = {**row.output, HANDOFF_KEY: {"status": "half-written"}}
        else:
            row.inputs = {"objective": ["not", "text"]}
        s.commit()
    executor.units["unit-0"] = UnitState("exited", 0)

    assert reconcile_launched_runs() == {RUN_RECORD_UNREADABLE: 1}

    row = _row(factory, run_id)
    assert (row.status, row.output["error"]) == (DbRunStatus.FAILED, RUN_RECORD_UNREADABLE)
    assert executor.removed == ["unit-0"]


def test_a_failed_launch_whose_close_out_dies_is_failed_with_its_launch_error(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor.refuse = "launch"
    run_id, inputs = _seed(factory)
    real = agent_runner_module._record_run_outcome
    monkeypatch.setattr(agent_runner_module, "_record_run_outcome", _killed)

    with pytest.raises(SystemExit):
        run_agent_task(inputs)
    monkeypatch.setattr(agent_runner_module, "_record_run_outcome", real)
    assert reconcile_launched_runs() == {}
    _expire_claim(factory, run_id)

    assert list(reconcile_launched_runs().values()) == [1]

    row = _row(factory, run_id)
    assert row.status is DbRunStatus.FAILED
    assert row.output["error"].startswith("the docker run executor could not launch the run")


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


def test_a_handoff_recorded_after_the_reconcilers_scan_is_not_discarded_as_lost(
    factory, executor, no_loop
) -> None:
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)

    def hands_off_and_exits(_unit: str) -> UnitState:
        # The unit records its handoff and exits after the pass read the run.
        _hand_off(factory, run_id)
        return UnitState("exited", 0)

    executor.state = hands_off_and_exits  # type: ignore[method-assign]

    assert reconcile_launched_runs() == {"handoff": 1}

    row = _row(factory, run_id)
    assert row.status is DbRunStatus.SUCCEEDED and row.confidence == 0.8


def test_a_unit_past_its_wall_clock_limit_and_grace_is_stopped_and_escalated(
    factory, executor, no_loop, audits, monkeypatch: pytest.MonkeyPatch
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
    assert (audits[-1]["details"]["outcome"], audits[-1]["result"]) == ("escalated", "error")


def test_an_unreachable_runtime_decides_nothing_until_the_deadline_then_escalates(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_RUN_TIME_LIMIT", "600")
    monkeypatch.setenv(RECONCILE_GRACE_ENV, "60")
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    launched_at = _launched_at(factory, run_id)

    def unreachable(_unit: str) -> UnitState:
        raise ExecutorUnavailable("daemon unreachable")

    monkeypatch.setattr(executor, "state", unreachable)

    assert reconcile_launched_runs(now=launched_at + timedelta(seconds=660)) == {}
    row = _row(factory, run_id)
    assert row.completed_at is None and "closing_since" not in row.output[EXECUTOR_KEY]

    closed = reconcile_launched_runs(now=launched_at + timedelta(seconds=661))

    assert closed == {EXECUTOR_UNREACHABLE: 1}
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.ESCALATED and "may still exist" in row.output["error"]


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
        {RUN_EXECUTOR_ENV: "docker", "FORGE_SANDBOX_KIND": "container", RUN_API_URL_ENV: API_URL},
        {
            RUN_EXECUTOR_ENV: "docker",
            "FORGE_SANDBOX_KIND": "microvm",
            SANDBOX_BACKEND_ENV: "docker",
            RUN_API_URL_ENV: API_URL,
        },
        {
            RUN_EXECUTOR_ENV: "kubernetes",
            "FORGE_SANDBOX_KIND": "container",
            SANDBOX_BACKEND_ENV: "kubernetes",
            RUN_API_URL_ENV: API_URL,
        },
    ],
)
def test_an_executor_on_its_sandbox_platform_starts(env: dict[str, str]) -> None:
    assert check_run_executor(env) == env.get(RUN_EXECUTOR_ENV, "inline")


@pytest.mark.parametrize("value", [None, ""])
def test_a_non_inline_executor_without_the_api_address_stops_the_worker_naming_it(
    value: str | None,
) -> None:
    """Its units reach their run record only through SCF's API (#257)."""
    env = {RUN_EXECUTOR_ENV: "docker", "FORGE_SANDBOX_KIND": "container"}
    if value is not None:
        env[RUN_API_URL_ENV] = value

    with pytest.raises(ValueError, match=RUN_API_URL_ENV):
        check_run_executor(env)


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
# #257: the run token, issued at launch and revoked at close-out              #
# --------------------------------------------------------------------------- #


def test_launch_issues_one_token_per_run_and_hands_it_to_that_unit_alone(
    factory, executor, no_loop, audits, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    run_a, inputs_a = _seed(factory)
    run_b, inputs_b = _seed(factory)

    run_agent_task(inputs_a)
    run_agent_task(inputs_b)
    run_agent_task(inputs_a)  # a duplicate delivery issues nothing

    token_a, token_b = _unit_token(executor, 0), _unit_token(executor, 1)
    assert len(executor.launched) == 2 and token_a != token_b
    assert not _refused(factory, run_a, token_a) and not _refused(factory, run_b, token_b)
    assert _refused(factory, run_b, token_a)
    assert token_a not in repr(executor.launched[1]) and token_b not in repr(executor.launched[0])
    assert _audit_actions(factory) == ["run_token.issued", "run_token.issued"]
    # Run A hands off and is closed out; run B's unit is lost.
    _hand_off(factory, run_a)
    executor.units["unit-0"] = UnitState("exited", 0)
    executor.units["unit-1"] = UnitState("exited", 137)
    assert reconcile_launched_runs() == {"handoff": 1, EXECUTOR_LOST: 1}
    written = [
        repr(audits),
        caplog.text,
        _written(factory, run_a),
        _written(factory, run_b),
    ]
    assert [text for text in written if token_a in text or token_b in text] == []


def test_a_launch_error_that_quotes_the_units_environment_stores_no_token(
    factory, executor, no_loop, audits, caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    launch = executor.launch

    def leaky(run_id: uuid.UUID, workspace_id: uuid.UUID, *, env: Mapping[str, str]) -> str:
        launch(run_id, workspace_id, env=env)
        raise RuntimeError(f"container create failed: {dict(env)}")

    monkeypatch.setattr(executor, "launch", leaky)
    run_id, inputs = _seed(factory)

    returned = run_agent_task(inputs)

    token = _unit_token(executor)
    assert returned["status"] == "failed" and "container create failed" in returned["error"]
    written = [json.dumps(returned), repr(audits), caplog.text, _written(factory, run_id)]
    assert [text for text in written if token in text] == []
    assert _refused(factory, run_id, token)  # the failed launch's close-out revoked it


@pytest.mark.parametrize(
    "end", ["close-out", "outcome not recorded", EXECUTOR_LOST, WALL_CLOCK_EXCEEDED]
)
def test_close_out_and_the_reconciler_revoke_the_runs_token(
    end: str, factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_RUN_TIME_LIMIT", "600")
    monkeypatch.setenv(RECONCILE_GRACE_ENV, "60")
    run_id, inputs = _seed(factory)
    run_agent_task(inputs)
    token = _unit_token(executor)
    assert not _refused(factory, run_id, token)

    if end in ("close-out", "outcome not recorded"):
        _hand_off(factory, run_id)
        executor.units["unit-0"] = UnitState("exited", 0)
        if end == "outcome not recorded":  # close-out claimed, but its outcome write failed
            monkeypatch.setattr(agent_runner_module, "_record_run_outcome", lambda *a, **kw: None)
        closed = close_out_task(str(run_id), str(WS))
        assert closed["closed_out"] is (end == "close-out")
    elif end == EXECUTOR_LOST:
        executor.units.clear()
        assert reconcile_launched_runs() == {EXECUTOR_LOST: 1}
    else:
        later = _launched_at(factory, run_id) + timedelta(seconds=661)
        assert reconcile_launched_runs(now=later) == {WALL_CLOCK_EXCEEDED: 1}
        assert _row(factory, run_id).status is DbRunStatus.ESCALATED

    assert _refused(factory, run_id, token)


def test_a_database_failure_while_issuing_the_token_fails_the_run_naming_the_launch(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = launched_runs.issue_run_token

    def database_fails(session: Session, **kwargs: Any) -> Any:
        session.add(Workspace(id=WS, name="Acme", slug="acme"))  # a duplicate: the flush fails
        return issue(session, **kwargs)

    monkeypatch.setattr(launched_runs, "issue_run_token", database_fails)
    run_id, inputs = _seed(factory)

    returned = run_agent_task(inputs)

    assert returned["status"] == "failed"
    assert returned["error"].startswith("the docker run executor could not launch the run")
    assert "IntegrityError" in returned["error"]
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.FAILED and row.output["error"] == returned["error"]
    assert RUN_TOKEN_KEY not in row.output and executor.launched == []
    assert "run_token.issued" not in _audit_actions(factory)


# The launch window: the unit is starting and the worker has not recorded it yet.


def test_a_handoff_recorded_while_the_unit_starts_is_kept_with_the_unit(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = executor.launch

    def hands_off_at_once(run_id: uuid.UUID, workspace_id: uuid.UUID, **kw: Any) -> str:
        unit = launch(run_id, workspace_id, **kw)
        _hand_off(factory, run_id)  # before the worker records the unit
        return unit

    monkeypatch.setattr(executor, "launch", hands_off_at_once)
    run_id, inputs = _seed(factory)

    assert run_agent_task(inputs)["launched"] is True

    output = _row(factory, run_id).output
    assert HANDOFF_KEY in output
    assert output[EXECUTOR_KEY]["unit"] == "unit-0" and "closing_since" not in output[EXECUTOR_KEY]


def test_while_the_unit_starts_neither_the_reconciler_nor_a_duplicate_acts_on_the_run(
    factory, executor, no_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, inputs = _seed(factory)
    during: list[Any] = []
    launch = executor.launch

    def launch_while_others_look(*args: Any, **kw: Any) -> str:
        during.append(reconcile_launched_runs())
        during.append(run_agent_task(inputs))
        return launch(*args, **kw)

    monkeypatch.setattr(executor, "launch", launch_while_others_look)

    assert run_agent_task(inputs)["launched"] is True

    assert during == [{}, {"launched": False, "run_id": str(run_id)}]
    assert len(executor.launched) == 1
    row = _row(factory, run_id)
    assert row.completed_at is None and row.output[EXECUTOR_KEY]["unit"] == "unit-0"


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


# --------------------------------------------------------------------------- #
# The row lock, on Postgres (skips without FORGE_TEST_DATABASE_URL)            #
# --------------------------------------------------------------------------- #


@pytest.mark.postgres
def test_two_close_outs_racing_on_postgres_claim_exactly_one(
    pg_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both claimers read the run before either commits; the row lock admits one."""
    Base.metadata.create_all(pg_engine)
    try:
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
        monkeypatch.setattr(launched_runs, "create_session_factory", lambda: factory)
        workspace_id, run_id = uuid.uuid4(), uuid.uuid4()
        handoff = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED).model_dump(mode="json")
        record = {"name": "docker", "unit": "unit-0", "launched_at": datetime.now(UTC).isoformat()}
        with factory() as s:
            s.add(Workspace(id=workspace_id, name="Race", slug=f"race-{workspace_id.hex[:8]}"))
            s.flush()
            s.add(
                AgentRun(
                    id=run_id,
                    workspace_id=workspace_id,
                    status=DbRunStatus.PENDING,
                    inputs={"objective": "race"},
                    output={EXECUTOR_KEY: record, HANDOFF_KEY: handoff},
                )
            )
            s.commit()
        stamp = launched_runs._now

        def slow_stamp() -> str:
            time.sleep(0.3)  # between the claimer's read and its commit
            return stamp()

        monkeypatch.setattr(launched_runs, "_now", slow_stamp)
        barrier = threading.Barrier(2)
        claimed: list[bool] = []

        def claim() -> None:
            barrier.wait()
            claimed.append(launched_runs._claim_close_out(run_id, workspace_id) is not None)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert sorted(claimed) == [False, True]
    finally:
        Base.metadata.drop_all(pg_engine)
