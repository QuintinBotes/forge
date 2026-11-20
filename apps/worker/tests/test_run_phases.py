"""An agent run's three phases, and the process that runs one run's loop (#238).

On a local bare mirror, the worktree sandbox and the offline scripted model. The
entry point always gets an explicit environment mapping: the root conftest sets
``FORGE_SECRET_KEY`` for the whole test process.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import scf_db.session
import scf_worker.agent_runner as agent_runner_module
import scf_worker.api_run_record as api_run_record
import scf_worker.multi_agent as multi_agent
import scf_worker.run_main as run_main
import scf_worker.tasks.pm_dispatch as pm_dispatch
from scf_agent.testing import ScriptedModelClient, finish_response, tool_response
from scf_api.auth.run_token import issue_run_token, revoke_run_token
from scf_api.deps import get_db
from scf_api.main import create_app
from scf_api.settings import Settings
from scf_contracts import (
    AgentObjective,
    AgentRunResult,
    RunEventDraft,
    RunEventType,
    RunStatus,
    Step,
)
from scf_db.base import Base
from scf_db.models import AgentRun, AgentRunEvent, AuditLog, Workspace
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_worker.agent_runner import close_out_run, execute_run, prepare_run, run_agent_task
from scf_worker.api_run_record import ATTEMPTS, ApiRunRecord
from scf_worker.finalizer import GitHubAppCredentials
from scf_worker.reliability import InMemoryDedupBackend, set_dedup_backend
from scf_worker.run_main import (
    RUN_API_URL_ENV,
    RUN_PROCESS_ALLOWED_ENV,
    RUN_PROCESS_DENIED_ENV,
    RUN_TIME_LIMIT_ENV,
    RUN_TOKEN_ENV,
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


# --------------------------------------------------------------------------- #
# #257: the record over SCF's API, with the run's token                       #
# --------------------------------------------------------------------------- #

API = "http://scf-api.test"
#: The default wall-clock limit plus the reconciler's default grace.
DEADLINE = 3_900.0


class Wire:
    """SCF's API reached in-process; it can refuse connections, throttle or lose an answer.

    The faults apply to every call, or only to the call ``only`` names (``"POST handoff"``).
    """

    def __init__(self, api: TestClient) -> None:
        self.api = api
        self.requests: list[httpx.Request] = []
        self.only: str | None = None
        self.unreachable = False
        self.lost_answers = 0
        self.throttled = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        faulty = self.only in (None, self.calls()[-1])
        if self.unreachable and faulty:
            raise httpx.ConnectError("connection refused", request=request)
        if self.throttled and faulty:  # the rate limiter answers; the route never runs
            self.throttled -= 1
            return httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "30"})
        answer = self.api.request(
            request.method, request.url.path, headers=request.headers, content=request.content
        )
        if self.lost_answers and faulty:  # the API handled the request; its answer never arrives
            self.lost_answers -= 1
            raise httpx.ReadError("connection reset by peer", request=request)
        kind = answer.headers.get("content-type", "application/json")
        return httpx.Response(
            answer.status_code, content=answer.content, headers={"content-type": kind}
        )

    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path.rsplit('/', 1)[1]}" for r in self.requests]


@pytest.fixture
def wire(factory: sessionmaker[Session]) -> Iterator[Wire]:
    """The API application on the test database, request replay off: the key alone dedupes."""
    app = create_app(Settings(ratelimit_enabled=False, idempotency_enabled=False))

    def database() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = database
    with TestClient(app) as api:
        yield Wire(api)


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The pauses the API record takes between attempts, recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr(api_run_record, "time", SimpleNamespace(sleep=slept.append))
    return slept


def _token(factory: sessionmaker[Session], run_id: uuid.UUID) -> str:
    """What the worker issues at launch."""
    with factory() as s:
        return issue_run_token(
            s, run_id=run_id, workspace_id=WS, run_deadline_seconds=DEADLINE
        ).token


def _api_record(wire: Wire, run_id: uuid.UUID, token: str) -> ApiRunRecord:
    return ApiRunRecord(run_id, WS, api_url=API, token=token, transport=httpx.MockTransport(wire))


def _through_api(
    factory: sessionmaker[Session], wire: Wire, run_id: uuid.UUID, *, token: str | None = None
) -> int:
    """The entry point as a unit runs it: its run token and the API's address, no database."""
    env = {RUN_TOKEN_ENV: token or _token(factory, run_id), RUN_API_URL_ENV: API}
    with pytest.MonkeyPatch.context() as mp:
        # Only the network is swapped: the entry point still picks the API record itself.
        mp.setattr(
            run_main, "ApiRunRecord", partial(ApiRunRecord, transport=httpx.MockTransport(wire))
        )
        for module in (agent_runner_module, multi_agent, scf_db.session):
            mp.setattr(module, "create_session_factory", _no_database)
        return main([str(run_id), str(WS)], env=env)


def _no_token(text: str, token: str) -> bool:
    secret = token.split("_", 4)[4]
    return token not in text and secret not in text


# Criterion 1: the port over the API --------------------------------------- #


def test_the_api_record_reads_appends_and_hands_off_through_the_run_record_routes(
    factory, wire
) -> None:
    run_id, inputs = _seed(factory, repo=False)
    token = _token(factory, run_id)
    record = _api_record(wire, run_id, token)

    assert record.inputs() == inputs
    assert record.emit(RunEventDraft(type=RunEventType.THREAD_STARTED, summary="started"))
    record.on_step(Step(output=f"thinking about {SECRET}"))
    assert record.record_handoff(
        AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED, summary="done")
    )

    assert wire.calls() == ["GET inputs", "POST events", "POST events", "POST handoff"]
    assert {r.url.path.rsplit("/", 1)[0] for r in wire.requests} == {f"/agent/runs/{run_id}/record"}
    assert all(r.headers["authorization"] == f"Bearer {token}" for r in wire.requests)
    events = _events(factory, run_id)
    assert [(e.seq, e.type) for e in events] == [(1, "thread.started"), (2, "agent.message")]
    assert SECRET not in repr([e.summary for e in events])
    assert _handoff(factory, run_id).summary == "done"
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.PENDING and row.completed_at is None


# Criterion 2: the entry point picks it, and opens no database connection --- #


def test_with_its_token_and_the_api_address_the_entry_point_records_through_the_api(
    factory, mirror, scripted, wire
) -> None:
    run_id, _ = _seed(factory)

    assert _through_api(factory, wire, run_id) == 0

    handoff = _handoff(factory, run_id)
    assert handoff.status is RunStatus.SUCCEEDED, handoff.error
    assert handoff.artifacts["branch"] == run_branch("scf", run_id)
    assert wire.calls()[0] == "GET inputs" and wire.calls()[-1] == "POST handoff"
    types = _types(factory, run_id)
    assert types[0] == "thread.started" and "tool.completed" in types
    assert len(types) == wire.calls().count("POST events")
    row = _row(factory, run_id)
    assert row.status is DbRunStatus.PENDING and row.completed_at is None


# Criterion 3: the two names ------------------------------------------------ #


@pytest.mark.parametrize("held", [RUN_TOKEN_ENV, RUN_API_URL_ENV])
def test_with_only_one_of_the_token_and_the_api_address_the_process_refuses_to_start(
    held: str, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[object] = []

    code = main(
        [str(uuid.uuid4()), str(WS)],
        env={held: "value-that-must-not-print"},
        record=lambda *a: built.append(a),
    )

    [missing] = {RUN_TOKEN_ENV, RUN_API_URL_ENV} - {held}
    err = capsys.readouterr().err
    assert code == 2 and built == []
    assert missing in err and "value-that-must-not-print" not in err
    assert {RUN_TOKEN_ENV, RUN_API_URL_ENV} <= RUN_PROCESS_ALLOWED_ENV


# Criterion 4: the same run in process and through the API ----------------- #


def test_in_process_and_through_the_api_the_entry_point_leaves_the_same_run(
    factory, mirror, scripted, wire
) -> None:
    rows = []
    for through_api in (False, True):
        run_id, inputs = _seed(factory)
        run, refused = prepare_run(AgentObjective.model_validate(inputs))
        assert refused is None
        code = _through_api(factory, wire, run_id) if through_api else _run_process(factory, run_id)
        assert code == 0
        close_out_run(run, _handoff(factory, run_id))
        rows.append(_row(factory, run_id))

    local, remote = rows
    assert remote.status is local.status is DbRunStatus.SUCCEEDED
    assert _steps(remote) == _steps(local) != []
    assert _artifacts(remote) == _artifacts(local)
    assert remote.confidence == local.confidence == 0.8
    assert _types(factory, remote.id) == _types(factory, local.id)
    assert wire.requests and all(f"/runs/{remote.id}/" in r.url.path for r in wire.requests)


# Criterion 5: a lost answer ------------------------------------------------ #


def test_an_append_whose_answer_is_lost_is_retried_with_its_key_and_stored_once(
    factory, wire
) -> None:
    run_id, _ = _seed(factory, repo=False)
    record = _api_record(wire, run_id, _token(factory, run_id))
    wire.lost_answers = 1

    assert record.emit(RunEventDraft(type=RunEventType.AGENT_MESSAGE, summary="first"))
    assert record.emit(RunEventDraft(type=RunEventType.AGENT_MESSAGE, summary="second"))

    keys = [r.headers["idempotency-key"] for r in wire.requests]
    assert len(keys) == 3 and keys[0] == keys[1] != keys[2]
    assert [(e.seq, e.summary) for e in _events(factory, run_id)] == [(1, "first"), (2, "second")]


# Criterion 6: refused or unreachable --------------------------------------- #


@pytest.mark.parametrize("token", ["revoked", "another run's"])
def test_a_refused_token_exits_non_zero_without_a_handoff_naming_the_status(
    token: str, factory, mirror, wire, capsys, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    run_id, _ = _seed(factory)
    if token == "revoked":
        value = _token(factory, run_id)
        with factory() as s:
            assert revoke_run_token(s, run_id=run_id, workspace_id=WS)
    else:
        value = _token(factory, _seed(factory)[0])

    assert _through_api(factory, wire, run_id, token=value) != 0

    err = capsys.readouterr().err
    assert "401" in err
    assert _no_token(err, value) and _no_token(caplog.text, value)
    assert wire.calls() == ["GET inputs", "POST handoff"]  # a refusal is never retried
    assert HANDOFF_KEY not in (_row(factory, run_id).output or {})


def test_an_unreachable_api_exits_non_zero_after_bounded_attempts_naming_the_error(
    factory, mirror, wire, capsys, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    run_id, _ = _seed(factory)
    token = _token(factory, run_id)
    wire.unreachable = True

    assert _through_api(factory, wire, run_id, token=token) != 0

    err = capsys.readouterr().err
    assert "ConnectError" in err
    assert _no_token(err, token) and _no_token(caplog.text, token)
    assert wire.calls() == ["GET inputs"] * ATTEMPTS + ["POST handoff"] * ATTEMPTS
    assert HANDOFF_KEY not in (_row(factory, run_id).output or {})


# Review fixes: a large handoff, a lost handoff answer, throttling ----------- #

MIB = 1_048_576


def test_a_handoff_over_the_body_limit_is_stored_and_one_over_its_own_cap_is_refused(
    factory, wire
) -> None:
    """Tool output is kept in the steps; the in-process record has no size limit."""
    run_id, _ = _seed(factory, repo=False)
    token = _token(factory, run_id)
    output = "x" * (3 * MIB // 2)
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED, steps=[Step(output=output)])

    assert _api_record(wire, run_id, token).record_handoff(result)

    assert _handoff(factory, run_id).steps[0].output == output
    cap = Settings().run_handoff_max_body_bytes
    record, auth = f"/agent/runs/{run_id}/record", {"Authorization": f"Bearer {token}"}
    over_cap = wire.api.post(f"{record}/handoff", headers=auth, content=b"x" * (cap + 1))
    other_route = wire.api.post(f"{record}/events", headers=auth, content=b"x" * (MIB + 1))
    assert (over_cap.status_code, other_route.status_code) == (413, 413)


def test_a_handoff_whose_answer_is_lost_is_recorded_once_and_the_process_exits_zero(
    factory, mirror, scripted, wire
) -> None:
    run_id, _ = _seed(factory)
    token = _token(factory, run_id)
    wire.only, wire.lost_answers = "POST handoff", 1

    assert _through_api(factory, wire, run_id, token=token) == 0

    assert wire.calls()[-2:] == ["POST handoff", "POST handoff"]
    assert _handoff(factory, run_id).status is RunStatus.SUCCEEDED
    with factory() as s:
        actions = list(s.scalars(select(AuditLog.action)))
    assert actions.count("agent_run.handoff_recorded") == 1
    # A 409 with no earlier attempt in doubt is still a refusal.
    with pytest.raises(api_run_record.RunRecordError, match="409"):
        _api_record(wire, run_id, token).record_handoff(AgentRunResult(run_id=run_id))


def test_a_throttled_append_waits_and_is_retried_with_its_key_and_stored_once(
    factory, wire, waits
) -> None:
    run_id, _ = _seed(factory, repo=False)
    record = _api_record(wire, run_id, _token(factory, run_id))
    wire.throttled = 1

    assert record.emit(RunEventDraft(type=RunEventType.AGENT_MESSAGE, summary="first"))

    keys = [r.headers["idempotency-key"] for r in wire.requests]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert waits == [api_run_record.MAX_RETRY_AFTER_SECONDS]  # Retry-After: 30, capped
    assert [(e.seq, e.summary) for e in _events(factory, run_id)] == [(1, "first")]
    # A refusal other than 429 is still never retried.
    assert not record.emit(RunEventDraft(type=RunEventType.THREAD_STATE, summary="mine"))
    assert len(wire.requests) == 3


def test_a_handoff_throttled_on_every_attempt_exits_non_zero_naming_429(
    factory, mirror, wire, capsys
) -> None:
    run_id, _ = _seed(factory)
    wire.throttled = 2 * ATTEMPTS

    assert _through_api(factory, wire, run_id) != 0

    assert "429" in capsys.readouterr().err
    assert wire.calls() == ["GET inputs"] * ATTEMPTS + ["POST handoff"] * ATTEMPTS
    assert HANDOFF_KEY not in (_row(factory, run_id).output or {})


def test_a_handoff_without_its_runs_token_is_refused_before_its_body_is_read(factory, wire) -> None:
    """The handoff's larger limit must not let any caller make the API parse a large body."""
    run_id, _ = _seed(factory, repo=False)
    junk = {"Authorization": "Bearer junk", "Content-Type": "application/json"}

    resp = wire.api.post(
        f"/agent/runs/{run_id}/record/handoff", headers=junk, content=b"{" * (2 * MIB)
    )

    assert resp.status_code == 401  # not 422: the body was never parsed
