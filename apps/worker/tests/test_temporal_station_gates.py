"""The worker's human-station gate activities (spec K §4.2, ADR-0020; #180).

``forge.open_station_gate`` opens one ``step`` approval request per visit, and a retry
finds it in any status. ``forge.read_station_gate`` takes an outcome only from a
decision Postgres holds for the same workspace, run, state and visit, made by a user
of the workspace; past the deadline it expires a pending gate with a conditional
update. End to end on the time-skipping server, the resolution hook's Signal wakes
the run and a cancel expires its open gate. Hermetic SQLite.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

import scf_api.db
import scf_worker.temporal_main as tm
import scf_worker.temporal_services as ts
from scf_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from scf_api.services.approval_service import build_gate_registry
from scf_approval import ApprovalAuthorizer, ApprovalService
from scf_approval.models import ApprovalAction, ApprovalDecisionRequest, Principal, Role
from scf_approval.providers import InMemoryGrantStore
from scf_contracts import RunStatus, UserRole
from scf_db.base import Base
from scf_db.models import (
    AgentRun,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    AuditLog,
    Project,
    RepositoryConnection,
    Task,
    User,
    Workspace,
)
from scf_db.models import WorkflowRun as DbWorkflowRun
from scf_db.models.enums import ApprovalGate
from scf_db.models.runs import WorkflowStationVisit
from scf_worker.approval_activity import build_approval_service
from scf_worker.temporal_services import open_station_gate_service, read_station_gate_service
from scf_workflow.store import SqlAlchemyWorkflowStore
from scf_workflow.temporal.engine import TemporalWorkflowEngine
from scf_workflow.temporal.ids import station_visit_id
from scf_workflow.temporal.payloads import StationGateInput, StationGateResult
from scf_workflow.temporal.worker import build_temporal_worker

WS = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
TASK_QUEUE = "station-human-gates"

LINE = """\
workflow: reviewed
dsl_version: "2"
initial: review
terminals: {done: succeeded}
stations:
  review: {kind: human, gate: plan, sla_seconds: 86400}
transitions:
  - {from: review, to: done, when: approved}
"""


@pytest.fixture
async def time_skip_env() -> AsyncIterator[Any]:
    """As ``packages/workflow-engine/tests/temporal/conftest.py``."""
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    from scf_workflow.temporal.testing import TEST_SERVER_VERSION

    try:
        env = await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            test_server_download_version=TEST_SERVER_VERSION,
        )
    except Exception as exc:  # pragma: no cover - offline sandbox
        pytest.skip(f"PARKED: Temporal test server unavailable: {exc}")
    try:
        yield env
    finally:
        await env.shutdown()


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    # A file, so the worker's activity threads and the approval service each get their
    # own connection: one shared in-memory connection interleaves their statements.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gates.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


@dataclass
class Seed:
    task_id: uuid.UUID
    run_id: uuid.UUID
    member: uuid.UUID
    agent: uuid.UUID
    outsider: uuid.UUID


def _user(s: Session, workspace_id: uuid.UUID, role: UserRole) -> uuid.UUID:
    user = User(workspace_id=workspace_id, email=f"{uuid.uuid4().hex}@example.test", role=role)
    s.add(user)
    s.flush()
    return user.id


def _seed(
    factory: sessionmaker[Session],
    *,
    with_run: bool = True,
    repo_targets: list[dict[str, str]] | None = None,
) -> Seed:
    with factory() as s:
        for ws, slug in ((WS, "acme"), (WS2, "rival")):
            if s.get(Workspace, ws) is None:
                s.add(Workspace(id=ws, name=slug, slug=slug))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
        s.add(project)
        s.flush()
        key = f"T-{uuid.uuid4().hex[:6]}"
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key=key,
            title="reviewed work",
            repo_targets=repo_targets or [],
        )
        s.add(task)
        s.flush()
        run_id = uuid.uuid4()
        if with_run:
            s.add(DbWorkflowRun(id=run_id, workspace_id=WS, task_id=task.id))
        seed = Seed(
            task_id=task.id,
            run_id=run_id,
            member=_user(s, WS, UserRole.MEMBER),
            agent=_user(s, WS, UserRole.AGENT_RUNNER),
            outsider=_user(s, WS2, UserRole.MEMBER),
        )
        s.commit()
    return seed


def _inp(seed: Seed, **fields: Any) -> StationGateInput:
    values: dict[str, Any] = {
        "workflow_run_id": seed.run_id,
        "workspace_id": WS,
        "state": "review",
        "visit": 1,
        "gate": "plan",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "required_approvals": 1,
        "summary": "Review it",
    }
    return StationGateInput(**(values | fields))


def _member(user_id: uuid.UUID) -> Principal:
    return Principal(kind="user", id=user_id, role=Role.MEMBER, workspace_id=WS)


async def _decide(
    factory: sessionmaker[Session], request_id: uuid.UUID, user_id: uuid.UUID, action: str
) -> None:
    decision = ApprovalDecisionRequest(decision=ApprovalAction(action))
    await build_approval_service(factory).resolve(
        request_id, decision, _member(user_id), workspace_id=WS
    )


def _requests(factory: sessionmaker[Session]) -> list[ApprovalRequest]:
    with factory() as s:
        return list(s.scalars(select(ApprovalRequest)))


def _refused(call: Any) -> ApplicationError:
    with pytest.raises(ApplicationError) as refused:
        call()
    assert refused.value.non_retryable
    return refused.value


# --------------------------------------------------------------------------- #
# Criterion 1: one request per visit, even when a retry follows a decision    #
# --------------------------------------------------------------------------- #


async def test_entering_a_human_station_opens_one_step_request_a_retry_reuses(
    factory: sessionmaker[Session],
) -> None:
    seed = _seed(factory)
    open_gate = open_station_gate_service(factory)
    inp = _inp(seed, required_approvals=2)

    opened = await open_gate(inp)

    assert opened.status == "pending"
    [row] = _requests(factory)
    assert row.id == opened.approval_request_id
    assert (row.subject_type, row.subject_id) == (
        "step",
        station_visit_id(seed.run_id, "review", 1),
    )
    assert (row.gate, row.workflow_run_id, row.task_id) == (
        ApprovalGate.PLAN,
        seed.run_id,
        seed.task_id,
    )
    assert row.expires_at is not None
    assert row.expires_at.replace(tzinfo=None) == inp.expires_at.replace(tzinfo=None)
    assert row.payload == {
        "run": str(seed.run_id),
        "state": "review",
        "visit": 1,
        "summary": "Review it",
    }
    assert row.requested_actor == f"agent:{seed.run_id}"
    assert row.required_approvals == 2

    for user in (seed.member, uuid.uuid4()):
        with factory() as s:  # a second, distinct person completes the quorum
            if s.get(User, user) is None:
                s.add(User(id=user, workspace_id=WS, email=f"{user.hex}@example.test"))
                s.commit()
        await _decide(factory, opened.approval_request_id, user, "approve")
    retried = await open_gate(inp)  # the activity retried after a quick decision

    assert (retried.approval_request_id, retried.status) == (opened.approval_request_id, "approved")
    assert len(_requests(factory)) == 1


# --------------------------------------------------------------------------- #
# Criterion 2 and 8: only a user's recorded decision on this visit's gate     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("action", "status"),
    [("approve", "approved"), ("reject", "rejected"), ("request_changes", "changes_requested")],
)
async def test_a_read_takes_a_users_decision_with_the_decider_as_actor(
    factory: sessionmaker[Session], action: str, status: str
) -> None:
    seed = _seed(factory)
    opened = await open_station_gate_service(factory)(_inp(seed))
    read = read_station_gate_service(factory)
    assert read(_inp(seed)).status == "pending"

    await _decide(factory, opened.approval_request_id, seed.member, action)
    result = read(_inp(seed))

    assert (result.approval_request_id, result.status, result.actor) == (
        opened.approval_request_id,
        status,
        f"user:{seed.member}",
    )


@pytest.mark.parametrize("forged", ["another_run", "another_visit", "caller_made"])
async def test_a_request_that_is_not_this_visits_gate_is_refused(
    factory: sessionmaker[Session], forged: str
) -> None:
    """A decided request on this visit's subject that the station did not open."""
    seed = _seed(factory)
    other = _seed(factory)
    run = other.run_id if forged == "another_run" else seed.run_id
    with factory() as s:
        row = ApprovalRequest(
            workspace_id=WS,
            subject_type="step",
            subject_id=station_visit_id(seed.run_id, "review", 1),
            gate=ApprovalGate.PLAN,
            status=ApprovalStatus.APPROVED,
            workflow_run_id=run,
            requested_actor="user:somebody" if forged == "caller_made" else f"agent:{run}",
            payload={
                "run": str(run),
                "state": "review",
                "visit": 2 if forged == "another_visit" else 1,
            },
            decided_by_id=seed.member,
        )
        s.add(row)
        s.flush()
        s.add(
            ApprovalDecision(
                workspace_id=WS,
                approval_request_id=row.id,
                approver_user_id=seed.member,
                decision="approve",
            )
        )
        s.commit()

    _refused(lambda: read_station_gate_service(factory)(_inp(seed)))
    with pytest.raises(ApplicationError):
        await open_station_gate_service(factory)(_inp(seed))
    assert len(_requests(factory)) == 1  # and no second gate was opened beside it


@pytest.mark.parametrize("decider", ["agent", "system", "outsider"])
async def test_a_decision_not_made_by_a_user_of_the_workspace_is_never_an_outcome(
    factory: sessionmaker[Session], decider: str
) -> None:
    """Written straight to the database: the approval service refuses these deciders."""
    seed = _seed(factory)
    opened = await open_station_gate_service(factory)(_inp(seed))
    user = {"agent": seed.agent, "system": None, "outsider": seed.outsider}[decider]
    with factory() as s:
        row = s.get(ApprovalRequest, opened.approval_request_id)
        assert row is not None
        row.status, row.decided_by_id = ApprovalStatus.APPROVED, user
        if user is not None:
            s.add(
                ApprovalDecision(
                    workspace_id=WS,
                    approval_request_id=row.id,
                    approver_user_id=user,
                    decision="approve",
                )
            )
        s.commit()

    refused = _refused(lambda: read_station_gate_service(factory)(_inp(seed)))
    assert refused.type == "StationGateRefused"


# --------------------------------------------------------------------------- #
# Criterion 5: past the deadline, a pending gate expires conditionally        #
# --------------------------------------------------------------------------- #


async def test_past_its_deadline_a_pending_gate_expires_and_a_decided_one_stands(
    factory: sessionmaker[Session],
) -> None:
    seed = _seed(factory)
    open_gate, read = open_station_gate_service(factory), read_station_gate_service(factory)
    pending = await open_gate(_inp(seed))

    expired = read(_inp(seed, expire=True))

    assert (expired.status, expired.actor) == ("expired", None)
    with factory() as s:
        row = s.get(ApprovalRequest, pending.approval_request_id)
        assert row is not None and row.status is ApprovalStatus.EXPIRED
        audit = s.scalars(select(AuditLog).where(AuditLog.target_id == row.id)).all()
        assert "approval.expired" in [a.action for a in audit]

    decided = await open_gate(_inp(seed, visit=2))
    await _decide(factory, decided.approval_request_id, seed.member, "approve")
    assert read(_inp(seed, visit=2, expire=True)).status == "approved"


# --------------------------------------------------------------------------- #
# End to end: the hook's Signal wakes the run; a cancel expires the gate      #
# --------------------------------------------------------------------------- #


async def _start_line(
    env: Any, factory: sessionmaker[Session], seed: Seed
) -> tuple[TemporalWorkflowEngine, uuid.UUID]:
    session = factory()  # the API's request session; the run row is committed on start
    engine = TemporalWorkflowEngine(
        workspace_id=WS,
        store=SqlAlchemyWorkflowStore(session, workspace_id=WS),
        client=env.client,
        task_queue=TASK_QUEUE,
        commit=session.commit,
    )
    run = await engine.astart(
        seed.task_id, "reviewed", definition_revision_id=uuid.uuid4(), dsl_yaml=LINE
    )
    assert run.id is not None
    return engine, run.id


def _run(factory: sessionmaker[Session], run_id: uuid.UUID) -> Any:
    """The projection as the worker last wrote it."""
    return tm.SessionPerCallStore(factory).get(run_id)


async def _until(check: Any) -> Any:
    for _ in range(500):
        if found := check():
            return found
        await asyncio.sleep(0.02)
    raise AssertionError("condition never held")


def _visit_status(factory: sessionmaker[Session], run_id: uuid.UUID) -> str | None:
    with factory() as s:
        return s.scalar(
            select(WorkflowStationVisit.status).where(
                WorkflowStationVisit.workflow_run_id == run_id
            )
        )


def _pending_gate(factory: sessionmaker[Session]) -> ApprovalRequest | None:
    rows = [r for r in _requests(factory) if r.status is ApprovalStatus.PENDING]
    return rows[0] if rows else None


async def test_the_resolution_hooks_signal_moves_the_run_as_the_decider(
    time_skip_env: Any, factory: sessionmaker[Session]
) -> None:
    seed = _seed(factory, with_run=False)
    activities = tm.build_activities(factory)
    async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
        engine, run_id = await _start_line(time_skip_env, factory, seed)

        @contextmanager
        def engine_for(workspace_id: uuid.UUID) -> Iterator[TemporalWorkflowEngine]:
            assert workspace_id == WS
            yield engine

        approvals = ApprovalService(
            SqlAlchemyApprovalRepository(factory),
            build_gate_registry(InMemoryGrantStore(), engine_factory=engine_for),
            ApprovalAuthorizer(),
        )
        request = await _until(lambda: _pending_gate(factory))
        await approvals.resolve(
            request.id,
            ApprovalDecisionRequest(decision=ApprovalAction.APPROVE),
            _member(seed.member),
            workspace_id=WS,
        )
        # Polling skips no time, so the 15-minute wake cannot be what moved it.
        run = await _until(lambda: (r := _run(factory, run_id)).status is RunStatus.SUCCEEDED and r)

    last = run.context["transitions"][-1]
    assert (last["from_state"], last["to_state"], last["event"]) == ("review", "done", "approved")
    assert last["actor"] == f"user:{seed.member}"


async def test_cancelling_a_run_at_a_human_station_expires_its_gate_as_run_cancelled(
    time_skip_env: Any, factory: sessionmaker[Session]
) -> None:
    seed = _seed(factory, with_run=False)
    activities = tm.build_activities(factory)
    async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
        engine, run_id = await _start_line(time_skip_env, factory, seed)
        request = await _until(lambda: _pending_gate(factory))
        await _until(lambda: _visit_status(factory, run_id) == "waiting")
        await engine.atransition(run_id, "cancel", actor=f"user:{seed.member}", reason="stop")
        await _until(lambda: _run(factory, run_id).status is RunStatus.CANCELLED)

    stored = await build_approval_service(factory).get(request.id, workspace_id=WS)
    assert stored.status.value == "expired"
    assert stored.gate_payload["expired"] == {
        "reason": "run_cancelled",
        "actor": f"user:{seed.member}",
    }
    assert _visit_status(factory, run_id) == "cancelled"


# --------------------------------------------------------------------------- #
# Review round: attestation, a cancel during the open, the expiry, mirrors    #
# --------------------------------------------------------------------------- #


class _Hints:
    """An engine factory that records the gate hook's ``station_gate_changed`` hints."""

    def __init__(self) -> None:
        self.sent: list[uuid.UUID] = []

    @contextmanager
    def __call__(self, workspace_id: uuid.UUID) -> Iterator[_Hints]:
        yield self

    async def astation_gate_changed(self, run_id: uuid.UUID, request_id: uuid.UUID) -> None:
        self.sent.append(request_id)


def _approve_in_db(factory: sessionmaker[Session], request_id: uuid.UUID, user: uuid.UUID) -> None:
    """The decision recorded, as ``ApprovalService.resolve`` writes it before its hook runs."""
    with factory() as s:
        row = s.get(ApprovalRequest, request_id)
        assert row is not None
        row.status, row.decided_by_id = ApprovalStatus.APPROVED, user
        s.add(
            ApprovalDecision(
                workspace_id=WS,
                approval_request_id=row.id,
                approver_user_id=user,
                decision="approve",
            )
        )
        s.commit()


@pytest.mark.parametrize("attestable", [True, False], ids=["attested", "attestation_raises"])
async def test_an_approved_pr_station_gate_counts_only_once_its_attestation_is_recorded(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, attestable: bool
) -> None:
    # The pr attestation hook opens its own session from the API's factory.
    monkeypatch.setattr(scf_api.db, "get_session_factory", lambda: factory)
    monkeypatch.delenv("FORGE_ATTEST_SIGNING_KEY", raising=False)
    seed = _seed(factory)
    if attestable:  # without an agent run to attest (no line has one yet), attesting raises
        with factory() as s:
            s.add(
                AgentRun(
                    workspace_id=WS,
                    workflow_run_id=seed.run_id,
                    task_id=seed.task_id,
                    role="implementer",
                    model="claude-sonnet-4-5",
                    sandbox_kind="gvisor",
                )
            )
            s.commit()
    opened = await open_station_gate_service(factory)(_inp(seed, gate="pr"))
    hints = _Hints()
    approvals = ApprovalService(
        SqlAlchemyApprovalRepository(factory),
        build_gate_registry(InMemoryGrantStore(), engine_factory=hints),
        ApprovalAuthorizer(),
    )

    approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)
    await approvals.resolve(
        opened.approval_request_id, approve, _member(seed.member), workspace_id=WS
    )
    result = read_station_gate_service(factory)(_inp(seed, gate="pr"))

    assert result.status == ("approved" if attestable else "attestation_missing")
    assert hints.sent == [opened.approval_request_id]  # either way the run hears of it now


async def test_a_pr_approval_whose_attestation_is_not_yet_recorded_waits_then_parks(
    factory: sessionmaker[Session],
) -> None:
    """Decided, but the hook has not recorded the attestation (it is still running, or
    its process died): the station waits, and parks at the deadline. An attestation id
    that names none of this run's attestations proves nothing."""
    seed = _seed(factory)
    opened = await open_station_gate_service(factory)(_inp(seed, gate="pr"))
    _approve_in_db(factory, opened.approval_request_id, seed.member)
    read = read_station_gate_service(factory)

    assert read(_inp(seed, gate="pr")).status == "pending"
    assert read(_inp(seed, gate="pr", expire=True)).status == "attestation_missing"
    with factory() as s:
        row = s.get(ApprovalRequest, opened.approval_request_id)
        assert row is not None
        row.payload = {**row.payload, "attestation_id": str(uuid.uuid4())}
        s.commit()
    assert read(_inp(seed, gate="pr")).status == "attestation_missing"


async def test_a_cancel_while_the_gate_opens_leaves_no_pending_request(
    time_skip_env: Any, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel's cleanup runs while the open is still on its way to the database."""
    real = open_station_gate_service(factory)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    async def slow_open(inp: StationGateInput) -> StationGateResult:
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        try:
            return await real(inp)
        finally:
            done.set()

    monkeypatch.setattr(tm, "open_station_gate_service", lambda _factory: slow_open)
    seed = _seed(factory, with_run=False)
    activities = tm.build_activities(factory)
    async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
        engine, run_id = await _start_line(time_skip_env, factory, seed)
        await _until(entered.is_set)
        await engine.atransition(run_id, "cancel", actor=f"user:{seed.member}", reason="stop")
        await _until(lambda: _run(factory, run_id).status is RunStatus.CANCELLED)
        release.set()
        await _until(done.is_set)

    assert _pending_gate(factory) is None
    [request] = _requests(factory)
    assert request.status is ApprovalStatus.EXPIRED
    assert request.payload["expired"]["reason"] == "run_cancelled"


async def test_an_expiry_by_the_station_is_announced_once(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []
    monkeypatch.setattr(ts, "publish_sync", lambda _url, event: events.append(event))
    seed = _seed(factory)
    opened = await open_station_gate_service(factory)(_inp(seed))

    read_station_gate_service(factory)(_inp(seed, expire=True))

    [event] = events
    assert (event.type.value, event.approval_id) == ("approval.decided", opened.approval_request_id)
    assert (event.payload["status"], event.payload["decided_by"]) == ("expired", "system")


async def test_a_pr_station_on_a_repository_with_no_mirror_fails_naming_it(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No mirror and no GitHub App credentials to fetch one: refused, not retried forever."""
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(tmp_path / "mirrors"))
    for name in ("FORGE_GITHUB_APP_ID", "FORGE_GITHUB_INSTALLATION_ID"):
        monkeypatch.delenv(name, raising=False)
    seed = _seed(factory, repo_targets=[{"repo": "acme/app", "base_branch": "main"}])
    _connect(factory, "acme/app")

    with pytest.raises(ApplicationError) as refused:
        await open_station_gate_service(factory)(_inp(seed, gate="pr"))

    assert refused.value.non_retryable and "acme/app" in str(refused.value)
    assert _requests(factory) == []


def _connect(factory: sessionmaker[Session], repo: str) -> None:
    with factory() as s:
        s.add(RepositoryConnection(workspace_id=WS, repo_id=repo, name=repo))
        s.commit()


async def test_a_pr_station_on_a_repository_not_connected_is_refused_unfetched(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like every other run path, a line reads only a repository its workspace connected."""
    fetched: list[str] = []
    monkeypatch.setattr(ts, "_mirror", lambda _ws, repo, _run: fetched.append(repo))
    seed = _seed(factory, repo_targets=[{"repo": "acme/unconnected", "base_branch": "main"}])

    with pytest.raises(ApplicationError) as refused:
        await open_station_gate_service(factory)(_inp(seed, gate="pr"))

    assert refused.value.non_retryable and "acme/unconnected" in str(refused.value)
    assert (fetched, _requests(factory)) == ([], [])
