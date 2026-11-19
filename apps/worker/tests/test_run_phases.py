"""An agent run's three phases, and the process that runs one run's loop (#238).

On a local bare mirror, the worktree sandbox and the offline scripted model. The
entry point always gets an explicit environment mapping: the root conftest sets
``FORGE_SECRET_KEY`` for the whole test process.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import scf_db.session
import scf_worker.agent_runner as agent_runner_module
import scf_worker.multi_agent as multi_agent
import scf_worker.tasks.pm_dispatch as pm_dispatch
from scf_agent.testing import ScriptedModelClient, finish_response, tool_response
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_db.base import Base
from scf_db.models import AgentRun, AgentRunEvent, Workspace
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_worker.agent_runner import close_out_run, execute_run, prepare_run, run_agent_task
from scf_worker.finalizer import GitHubAppCredentials
from scf_worker.reliability import InMemoryDedupBackend, set_dedup_backend
from scf_worker.run_main import (
    RUN_PROCESS_ALLOWED_ENV,
    RUN_PROCESS_DENIED_ENV,
    RUN_TIME_LIMIT_ENV,
    main,
)
from scf_worker.run_record import HANDOFF_KEY, InProcessRunRecord
from scf_worker.run_workspace import run_branch

WS = uuid.UUID("00000000-0000-0000-0000-000000000238")
REPO = "acme/app"
#: Secret-shaped, so the shared redactor must strip it from every stored event.
SECRET = "ghp_" + "s" * 36


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    # The worker's phases reach the database through its global factory.
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: session_factory)
    with session_factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
    yield session_factory
    engine.dispose()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    for name in (
        "FORGE_MODEL_PROVIDER",
        "FORGE_RECORD_RUNS",
        "FORGE_GITHUB_APP_ID",
        "FORGE_GITHUB_INSTALLATION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("app\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    path = root / str(WS) / "acme" / "app"
    path.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(path))
    return path


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same scripted objective for every run: write, read back, finish."""

    def client() -> ScriptedModelClient:
        return ScriptedModelClient(
            responses=[
                tool_response("write_file", {"path": "health.py", "content": "ok = True\n"}),
                tool_response("read_file", {"path": "health.py"}),
                finish_response(f"Added health.py; token {SECRET}", confidence=0.8),
            ],
            default=finish_response("Done", confidence=0.8),
        )

    monkeypatch.setattr(agent_runner_module, "_scripted_client", client)


def _seed(
    factory: sessionmaker[Session],
    *,
    repo: bool = True,
    run_id: uuid.UUID | None = None,
    **fields: Any,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """A pending run whose stored inputs are its objective, as the API writes them."""
    run_id = run_id or uuid.uuid4()
    context: dict[str, Any] = {"agent_run_id": str(run_id), "workspace_id": str(WS)}
    targets = [{"repo": REPO, "base_branch": "main", "branch_prefix": "scf"}] if repo else []
    if repo:
        context["repo_full_name"] = REPO
    context |= fields.pop("context", {})
    inputs = AgentObjective(
        objective="Add a health endpoint", repo_targets=targets, context=context, **fields
    ).model_dump(mode="json")
    with factory() as s:
        s.add(AgentRun(id=run_id, workspace_id=WS, status=DbRunStatus.PENDING, inputs=inputs))
        s.commit()
    return run_id, inputs


def _no_database(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("a run process opens no database session itself")


def _record(factory: sessionmaker[Session]) -> Callable[[uuid.UUID, uuid.UUID], Any]:
    def build(run_id: uuid.UUID, workspace_id: uuid.UUID) -> InProcessRunRecord:
        return InProcessRunRecord(
            run_id,
            workspace_id,
            session_factory=factory,
            redact=agent_runner_module._default_redactor(),
        )

    return build


def _run_process(
    factory: sessionmaker[Session],
    run_id: uuid.UUID,
    *,
    env: dict[str, str] | None = None,
    record: Callable[[uuid.UUID, uuid.UUID], Any] | None = None,
) -> int:
    """The entry point, with every session factory but its record port's unusable."""
    with pytest.MonkeyPatch.context() as mp:
        for module in (agent_runner_module, multi_agent, scf_db.session):
            mp.setattr(module, "create_session_factory", _no_database)
        return main([str(run_id), str(WS)], env=env or {}, record=record or _record(factory))


def _row(factory: sessionmaker[Session], run_id: uuid.UUID) -> AgentRun:
    with factory() as s:
        row = s.get(AgentRun, run_id)
        assert row is not None
        return row


def _handoff(factory: sessionmaker[Session], run_id: uuid.UUID) -> AgentRunResult:
    return AgentRunResult.model_validate(_row(factory, run_id).output[HANDOFF_KEY])


def _events(factory: sessionmaker[Session], run_id: uuid.UUID) -> list[AgentRunEvent]:
    with factory() as s:
        return list(
            s.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id)
                .order_by(AgentRunEvent.seq)
            )
        )


def _types(factory: sessionmaker[Session], run_id: uuid.UUID) -> list[str]:
    return [e.type for e in _events(factory, run_id)]


# --------------------------------------------------------------------------- #
# Criterion 1: each phase called directly                                     #
# --------------------------------------------------------------------------- #


def test_prepare_stamps_the_run_and_resolves_its_mirror(factory, mirror) -> None:
    run_id, inputs = _seed(factory)

    run, refused = prepare_run(AgentObjective.model_validate(inputs))

    assert refused is None
    assert run.objective.context["repo_path"] == str(mirror)
    assert (run.run_id, run.workspace_id, run.persisted_row) == (run_id, WS, True)
    row = _row(factory, run_id)
    assert row.started_at is not None
    assert row.status is DbRunStatus.PENDING
    assert _events(factory, run_id) == []


def test_prepare_returns_a_refusal_that_close_out_records(factory, mirror) -> None:
    run_id, inputs = _seed(factory, context={"repo_full_name": "acme/missing"})

    run, refused = prepare_run(AgentObjective.model_validate(inputs))

    assert refused is not None and refused.status is RunStatus.FAILED
    assert "no mirror" in str(refused.error)
    assert _row(factory, run_id).completed_at is None
    close_out_run(run, refused)
    assert _row(factory, run_id).status is DbRunStatus.FAILED
    assert _types(factory, run_id) == ["thread.failed"]


def test_execute_runs_the_loop_and_leaves_the_run_open(factory, mirror, scripted) -> None:
    run_id, inputs = _seed(factory)
    run, _ = prepare_run(AgentObjective.model_validate(inputs))

    result = execute_run(run)

    assert result.status is RunStatus.SUCCEEDED
    assert result.artifacts["branch"] == run_branch("scf", run_id)
    assert result.artifacts["head_commit"] == result.artifacts["base_commit"]
    types = _types(factory, run_id)
    assert types[0] == "thread.started"
    assert "tool.completed" in types
    assert not {"thread.completed", "thread.failed"} & set(types)
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.PENDING and row.completed_at is None


def test_close_out_delivers_then_records_the_outcome_then_writes_back(
    factory, mirror, scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, inputs = _seed(factory)
    run, _ = prepare_run(AgentObjective.model_validate(inputs))
    result = execute_run(run)
    calls: list[str] = []

    def deliver(objective: AgentObjective, res: AgentRunResult, **kw: Any) -> None:
        calls.append(f"deliver {kw['branch']}")
        res.artifacts["pull_request"] = {"status": "opened", "number": 7, "url": "u"}

    def write_back(*_a: Any, **_kw: Any) -> None:
        calls.append(f"write-back after {_row(factory, run_id).status.value}")

    monkeypatch.setattr(agent_runner_module, "_finalize", deliver)
    monkeypatch.setattr(agent_runner_module, "_maybe_tracker_writeback", write_back)

    close_out_run(run, result)

    assert calls == [f"deliver {run_branch('scf', run_id)}", "write-back after succeeded"]
    assert _types(factory, run_id)[-3:] == ["artifact.created", "cost.updated", "thread.completed"]
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.SUCCEEDED and row.completed_at is not None


# --------------------------------------------------------------------------- #
# Criteria 2 to 5: the entry point                                            #
# --------------------------------------------------------------------------- #


def test_the_entry_point_hands_off_through_the_port_without_closing_out(
    factory, mirror, scripted
) -> None:
    run_id, _ = _seed(factory, repo=False)

    assert _run_process(factory, run_id) == 0

    row = _row(factory, run_id)
    assert set(row.output) == {HANDOFF_KEY}
    assert _handoff(factory, run_id).status is RunStatus.SUCCEEDED
    assert row.status is DbRunStatus.PENDING
    assert row.started_at is None and row.completed_at is None
    assert not {"thread.completed", "thread.failed"} & set(_types(factory, run_id))


def test_a_failed_loop_is_still_handed_off(
    factory, mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any) -> Any:
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(agent_runner_module, "run_objective", boom)
    run_id, _ = _seed(factory)

    assert _run_process(factory, run_id) == 0

    handoff = _handoff(factory, run_id)
    assert (handoff.status, handoff.error) == (RunStatus.FAILED, "RuntimeError")
    assert _row(factory, run_id).status is DbRunStatus.PENDING


def test_with_no_handoff_recorded_the_exit_is_non_zero(factory, mirror, capsys) -> None:
    run_id, _ = _seed(factory, repo=False)

    other = uuid.uuid4()
    code = main([str(run_id), str(other)], env={}, record=_record(factory))
    assert code != 0
    assert main([str(run_id), str(WS)], env={}) != 0  # no record port on the command line
    assert "API-backed record" in capsys.readouterr().err
    assert _row(factory, run_id).output is None


def test_step_events_are_stored_numbered_and_redacted_before_the_handoff(
    factory, mirror, scripted
) -> None:
    run_id, _ = _seed(factory)
    at_handoff: list[AgentRunEvent] = []
    build = _record(factory)

    def record(rid: uuid.UUID, wid: uuid.UUID) -> InProcessRunRecord:
        port = build(rid, wid)
        real = port.record_handoff

        def record_handoff(result: AgentRunResult) -> bool:
            at_handoff.extend(_events(factory, rid))
            return real(result)

        port.record_handoff = record_handoff
        return port

    assert _run_process(factory, run_id, record=record) == 0

    assert [e.seq for e in at_handoff] == list(range(1, len(at_handoff) + 1))
    assert at_handoff[0].type == "thread.started"
    assert {"tool.requested", "tool.completed"} <= {e.type for e in at_handoff}
    stored = repr([(e.summary, e.payload) for e in at_handoff])
    assert SECRET not in stored and "[REDACTED]" in stored
    assert _types(factory, run_id) == [e.type for e in at_handoff]


def test_the_entry_point_never_delivers_writes_back_or_reads_application_settings(
    factory, mirror, scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("a run process must not reach this")

    monkeypatch.setattr(GitHubAppCredentials, "from_env", forbidden)
    monkeypatch.setattr(agent_runner_module, "resolve_run_repository", forbidden)
    monkeypatch.setattr(multi_agent, "resolve_run_repository", forbidden)
    monkeypatch.setattr(agent_runner_module, "_finalize", forbidden)
    monkeypatch.setattr(agent_runner_module, "push_and_open_pr", forbidden)
    monkeypatch.setattr(agent_runner_module, "_maybe_tracker_writeback", forbidden)
    monkeypatch.setattr(pm_dispatch, "post_run_writeback", forbidden)
    tracker = {"connection_id": str(uuid.uuid4()), "external_id": "7"}
    run_id, _ = _seed(factory, context={"tracker": tracker})

    assert _run_process(factory, run_id) == 0

    handoff = _handoff(factory, run_id)
    assert handoff.status is RunStatus.SUCCEEDED, handoff.error
    assert handoff.artifacts["branch"] == run_branch("scf", run_id)
    assert "pull_request" not in handoff.artifacts


def test_a_missing_mirror_is_handed_off_as_failed(factory, mirror) -> None:
    run_id, _ = _seed(factory, context={"repo_full_name": "acme/missing"})

    assert _run_process(factory, run_id) == 0

    handoff = _handoff(factory, run_id)
    assert handoff.status is RunStatus.FAILED and "no mirror" in str(handoff.error)


# --------------------------------------------------------------------------- #
# Criterion 3: the same run inline and through the entry point                #
# --------------------------------------------------------------------------- #


def _steps(row: AgentRun) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in s.items() if k not in {"timestamp", "duration_ms"}} for s in row.steps
    ]


def _artifacts(row: AgentRun) -> dict[str, Any]:
    artifacts = dict(row.output["artifacts"])
    assert artifacts.pop("branch") == run_branch("scf", row.id)  # names its own run
    return artifacts


def test_inline_and_through_the_entry_point_leave_the_same_run(factory, mirror, scripted) -> None:
    inline_id, inline_inputs = _seed(factory)
    run_agent_task(inline_inputs)

    process_id, process_inputs = _seed(factory)
    run, refused = prepare_run(AgentObjective.model_validate(process_inputs))
    assert refused is None
    assert _run_process(factory, process_id) == 0
    close_out_run(run, _handoff(factory, process_id))

    inline, process = _row(factory, inline_id), _row(factory, process_id)
    assert process.status is inline.status is DbRunStatus.SUCCEEDED
    assert _steps(process) == _steps(inline) != []
    assert _artifacts(process) == _artifacts(inline)
    assert _artifacts(process)["pull_request"] == {"status": "no_changes"}
    assert process.confidence == inline.confidence == 0.8
    assert _types(factory, process_id) == _types(factory, inline_id)
    assert HANDOFF_KEY not in process.output
    assert process.completed_at is not None


# --------------------------------------------------------------------------- #
# Criterion 6: what a run process may and may not hold                        #
# --------------------------------------------------------------------------- #


def test_the_allowed_and_denied_names_never_overlap() -> None:
    assert not RUN_PROCESS_ALLOWED_ENV & RUN_PROCESS_DENIED_ENV
    assert {
        "FORGE_GITHUB_APP_ID",
        "FORGE_GITHUB_INSTALLATION_ID",
        "FORGE_GITHUB_APP_PRIVATE_KEY_PATH",
        "FORGE_SECRET_KEY",
        "FORGE_GITHUB_WEBHOOK_SECRET",
        "FORGE_DATABASE_URL",
    } <= RUN_PROCESS_DENIED_ENV
    assert RUN_TIME_LIMIT_ENV in RUN_PROCESS_ALLOWED_ENV


@pytest.mark.parametrize("name", sorted(RUN_PROCESS_DENIED_ENV))
def test_a_denied_name_stops_the_process_naming_it_not_its_value(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[object] = []

    code = main(
        [str(uuid.uuid4()), str(WS)],
        env={"PATH": "/usr/bin", name: "value-that-must-not-print"},
        record=lambda *a: built.append(a),
    )

    err = capsys.readouterr().err
    assert code != 0 and built == []
    assert name in err and "value-that-must-not-print" not in err


@pytest.mark.parametrize(
    "name",
    # The chart's names for the same secrets, and a retained versioned master key.
    ["GITHUB_APP_PRIVATE_KEY", "FORGE_VAULT_KEYS", "TEMPORAL_CODEC_KEY", "FORGE_SECRET_KEY_V2"],
)
def test_the_charts_secret_names_and_versioned_keys_stop_the_process(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(uuid.uuid4()), str(WS)], env={name: "value-that-must-not-print"})

    err = capsys.readouterr().err
    assert code == 2 and name in err and "value-that-must-not-print" not in err


def test_off_the_main_thread_the_process_refuses_to_start() -> None:
    # The wall-clock limit is a SIGALRM, which only the main thread can set.
    codes: list[int] = []
    worker = threading.Thread(
        target=lambda: codes.append(main([str(uuid.uuid4()), str(WS)], env={}))
    )
    worker.start()
    worker.join()
    assert codes == [2]


# --------------------------------------------------------------------------- #
# Criterion 7: the wall-clock limit                                           #
# --------------------------------------------------------------------------- #


def test_the_wall_clock_limit_hands_off_an_escalated_run(
    factory, mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    def runaway(*_a: Any) -> Any:
        try:
            time.sleep(30)
        except Exception:  # the loop's own handlers must not swallow the limit
            time.sleep(30)

    monkeypatch.setattr(agent_runner_module, "run_objective", runaway)
    run_id, _ = _seed(factory)
    started = time.monotonic()

    assert _run_process(factory, run_id, env={RUN_TIME_LIMIT_ENV: "0.3"}) == 0

    assert time.monotonic() - started < 10
    handoff = _handoff(factory, run_id)
    assert (handoff.status, handoff.error) == (RunStatus.ESCALATED, "soft_time_limit_exceeded")
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.PENDING and row.completed_at is None


@pytest.mark.parametrize("value", ["0", "-5", "soon", "inf", "nan", "1e12"])
def test_an_unusable_time_limit_stops_the_process(value: str) -> None:
    assert main([str(uuid.uuid4()), str(WS)], env={RUN_TIME_LIMIT_ENV: value}) == 2


# --------------------------------------------------------------------------- #
# Criterion 8: a supervised objective                                         #
# --------------------------------------------------------------------------- #


def _supervised(run_id: uuid.UUID) -> dict[str, Any]:
    return {
        "context": {
            "parent_agent_run_id": str(run_id),
            "subagent_rules": {
                "allow_subagents": True,
                "allowed_roles": ["implementer", "reviewer"],
                "max_parallel": 2,
            },
        },
        "execution_mode": "supervised_multi_agent",
        "subagent_policy": {"allowed": True, "max_parallel": 2},
    }


def _outcome(result: AgentRunResult) -> dict[str, Any]:
    """The result without the ids each run mints for itself."""
    outcome = result.model_dump(mode="json", exclude={"run_id", "task_id", "steps"})
    for subagent in outcome["artifacts"]["subagents"]:
        assert subagent.pop("agent_run_id")
    return outcome


def test_a_supervised_objective_hands_off_what_the_celery_task_returns(
    factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_MODEL_PROVIDER", raising=False)
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "1")
    monkeypatch.setattr(multi_agent, "create_session_factory", lambda: factory)
    set_dedup_backend(InMemoryDedupBackend())
    try:
        celery_id = uuid.uuid4()
        payload = _supervised(celery_id)
        payload["context"]["workspace_id"] = str(WS)
        celery = AgentRunResult.model_validate(
            run_agent_task({"objective": "Add a health endpoint", **payload})
        )
    finally:
        set_dedup_backend(None)
    process_id = uuid.uuid4()  # the API names the run itself as the supervisor
    _seed(factory, repo=False, run_id=process_id, **_supervised(process_id))

    assert _run_process(factory, process_id) == 0

    handoff = _handoff(factory, process_id)
    assert celery.status is RunStatus.SUCCEEDED
    assert _outcome(handoff) == _outcome(celery)
    assert handoff.run_id == process_id and celery.run_id == celery_id
    assert [s.kind for s in handoff.steps] == [s.kind for s in celery.steps]
    assert _row(factory, process_id).status is DbRunStatus.PENDING
