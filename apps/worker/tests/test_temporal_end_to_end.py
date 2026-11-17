"""A governed run on the worker's real activities, from start to the pr gate (#253).

``build_activities`` over a database, a local bare mirror, the worktree sandbox and
a scripted model that writes and commits one file; ``FORGE_ALLOW_FAKE_ACTIVITIES``
unset. Only GitHub's HTTP layer is faked, and git really pushes to a local bare
"remote". Runs on the time-skipping Temporal test server and skips (never fakes)
when that server cannot start. One run starts from an issue delivery, as a labelled
issue starts it, and reports back to that issue (#278).
"""

from __future__ import annotations

import asyncio
import functools
import json
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import scf_api.db
import scf_worker.agent_runner as agent_runner_module
import scf_worker.temporal_main as tm
import scf_worker.temporal_services as temporal_services_module
from scf_agent.testing import ScriptedModelClient, finish_response, tool_response
from scf_api.auth.crypto import HmacAeadCipher
from scf_api.auth.vault import SecretVault
from scf_api.observability.audit import AuditLog
from scf_api.services import pm_service as pm_service_module
from scf_api.services import workflow_engine as workflow_engine_module
from scf_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from scf_api.services.approval_service import build_gate_registry
from scf_api.services.workflow_gate_hook import sync_gate_event
from scf_approval import ApprovalAuthorizer, ApprovalService
from scf_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Principal,
    Role,
)
from scf_approval.providers import InMemoryGrantStore
from scf_contracts.enums import Direction
from scf_contracts.orchestration_config import AgentRole, Effort
from scf_contracts.pm import ExternalTask, StatusCategory
from scf_contracts.pm import PMProvider as ContractsProvider
from scf_db.base import Base
from scf_db.models import AgentRun, Attestation, Project, RepositoryConnection, Task, Workspace
from scf_db.models.enums import PMConnectionStatus, PMDeliveryStatus
from scf_db.models.enums import PMProvider as DbPMProvider
from scf_db.models.pm import PMConnection, PMWebhookDelivery
from scf_db.models.role_config import AgentRoleConfig
from scf_worker.finalizer import GitHubAppCredentials
from scf_worker.tasks import pm_dispatch
from scf_worker.tasks.pm_sync import process_webhook
from scf_worker.temporal_services import open_pr_service, run_checks_service
from scf_workflow.store import SqlAlchemyWorkflowStore
from scf_workflow.temporal.engine import TemporalWorkflowEngine
from scf_workflow.temporal.ids import workflow_id
from scf_workflow.temporal.worker import build_temporal_worker
from scf_workflow.temporal.workflows import FeatureWorkflow

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a9")
REPO = "acme/app"
TASK_QUEUE = "scf-253-end-to-end"
TOKEN = "ghs_" + "t" * 36
#: The repository's one declared check passes only on the agent's commit.
POLICY = 'repo_id: app\ncommands:\n  tests: "test -f health.py"\n'


@pytest.fixture
async def time_skip_env() -> AsyncIterator[Any]:
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
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    # The agent body and the pr gate's attestation hook open their sessions from
    # the process factories; point both at this database.
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: session_factory)
    monkeypatch.setattr(scf_api.db, "get_session_factory", lambda: session_factory)
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
def remotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace mirror of ``REPO`` and a local bare "remote" standing in for GitHub."""
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(tmp_path / "mirrors"))
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    monkeypatch.setenv("FORGE_ALLOW_SCRIPTED_AGENT", "1")
    for name in (
        "FORGE_ALLOW_FAKE_ACTIVITIES",
        "FORGE_MODEL_PROVIDER",
        "FORGE_RECORD_RUNS",
        "FORGE_GITHUB_APP_ID",
        "FORGE_GITHUB_INSTALLATION_ID",
        "FORGE_ATTEST_SIGNING_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    (work / ".forge").mkdir(parents=True)
    (work / ".forge" / "policy.yaml").write_text(POLICY)
    (work / "README.md").write_text("app\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    remote = tmp_path / "remotes" / "acme" / "app.git"
    remote.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(remote))
    mirror = tmp_path / "mirrors" / str(WS) / "acme" / "app"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(remote), str(mirror))
    return tmp_path / "remotes"


@pytest.fixture
def committing_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The offline scripted model: write a file, commit it, finish."""

    def scripted() -> ScriptedModelClient:
        return ScriptedModelClient(
            responses=[
                tool_response(
                    "write_file",
                    {"path": "health.py", "content": "def health():\n    return 'ok'\n"},
                ),
                tool_response("git_commit", {"message": "Add a health endpoint"}),
                finish_response("Added health()", confidence=0.9),
            ],
            default=finish_response("Done", confidence=0.9),
        )

    monkeypatch.setattr(agent_runner_module, "_scripted_client", scripted)


class FakeGitHub:
    """GitHub's REST API for one repository: token minting, PR lookup and creation."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        def pr(number: int) -> dict[str, Any]:
            return {
                "number": number,
                "html_url": f"https://github.com/acme/app/pull/{number}",
                "state": "open",
                "head": {"ref": self.created[number - 1]["head"], "sha": "abc"},
                "base": {"ref": "main"},
                "title": self.created[number - 1]["title"],
            }

        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if request.url.path == "/repos/acme/app/pulls" and request.method == "GET":
            return httpx.Response(200, json=[pr(n) for n in range(len(self.created), 0, -1)])
        if request.url.path == "/repos/acme/app/pulls" and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json=pr(len(self.created)))
        return httpx.Response(404)


@pytest.fixture
def github(remotes: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """GitHub App credentials whose git side is the local remote; the HTTP side is faked."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    credentials = GitHubAppCredentials(
        app_id="1", installation_id="2", private_key_pem=pem, git_base_url=remotes.as_uri()
    )
    fake = FakeGitHub()
    monkeypatch.setattr(
        tm,
        "open_pr_service",
        functools.partial(
            open_pr_service,
            credentials=lambda: credentials,
            transport=httpx.MockTransport(fake.handler),
        ),
    )
    return fake


@pytest.fixture
def independent_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The builder runs on Anthropic's model; the reviewer, of OpenAI's family, approves."""
    monkeypatch.setattr(temporal_services_module, "configured_model", lambda: "claude-opus-5-5")

    def reviewer(_model: str) -> ScriptedModelClient:
        return ScriptedModelClient(
            [finish_response('{"verdict": "approve", "findings": []}', confidence=0.9)]
        )

    monkeypatch.setattr(
        tm, "run_checks_service", functools.partial(run_checks_service, reviewer=reviewer)
    )


def _seed_workspace(s: Session) -> Project:
    """The workspace, its connected repository, its reviewer and one project."""
    s.add(Workspace(id=WS, name="Acme", slug="acme"))
    s.add(RepositoryConnection(workspace_id=WS, repo_id=REPO, name=REPO))
    s.add(
        AgentRoleConfig(
            workspace_id=WS,
            role=AgentRole.REVIEWER,
            model_or_tier="gpt-6-astra",
            effort=Effort.HIGH,
        )
    )
    s.flush()
    project = Project(workspace_id=WS, name="Core", key="CORE")
    s.add(project)
    s.flush()
    return project


def _seed(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as s:
        project = _seed_workspace(s)
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-21",
            title="Add a health endpoint",
            repo_targets=[{"repo": REPO, "base_branch": "main", "branch_prefix": "scf"}],
        )
        s.add(task)
        s.commit()
        return task.id


async def _wait_awaiting(handle: Any, event: str) -> None:
    last: list[str] = []
    for _ in range(1200):  # the agent, its checks and the pull request run for real
        last = await handle.query(FeatureWorkflow.awaiting)
        if event in last:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never awaited {event!r}; last={last}")


async def _pending(factory: sessionmaker[Session], gate: GateType) -> Any:
    rows = await SqlAlchemyApprovalRepository(factory).list(workspace_id=WS)
    return [r for r in rows if r.gate_type is gate and r.status is GateStatus.PENDING]


def _approvals(factory: sessionmaker[Session], engine: TemporalWorkflowEngine) -> ApprovalService:
    """The approval service, whose gate decisions reach the workflow through ``engine``."""

    @contextmanager
    def engine_for(workspace_id: uuid.UUID) -> Iterator[TemporalWorkflowEngine]:
        assert workspace_id == WS
        yield engine

    return ApprovalService(
        SqlAlchemyApprovalRepository(factory),
        build_gate_registry(InMemoryGrantStore(), engine_factory=engine_for),
        ApprovalAuthorizer(),
    )


def _attestations(factory: sessionmaker[Session], run_id: uuid.UUID) -> int:
    with factory() as s:
        return len(
            s.scalars(select(Attestation).where(Attestation.workflow_run_id == run_id)).all()
        )


def test_every_activity_but_resume_agent_is_bound(
    factory: sessionmaker[Session], remotes: Path
) -> None:
    """Criterion 1: with fakes off, only ``forge.resume_agent`` still refuses."""
    activities = tm.build_activities(factory)
    services = {
        "load_guard_inputs": activities._guard_inputs_fn,
        "run_agent": activities._run_agent_fn,
        "resume_agent": activities._resume_agent_fn,
        "run_checks": activities._run_checks_fn,
        "open_pr_with_spec_traceability": activities._open_pr_fn,
        "pause_and_notify": activities._notify_fn,
        "cleanup_worktree": activities._cleanup_fn,
        "create_approval": activities._approval_fn,
        "run_red_team_scan": activities._red_team_fn,
        "draft_held_out_scenarios": activities._draft_scenarios_fn,
    }
    refusing = {name for name, fn in services.items() if fn.__name__ == "_not_configured"}
    assert refusing == {"resume_agent"}


async def test_a_governed_run_reaches_the_pr_gate_and_is_synced_to_its_approval(
    time_skip_env: Any,
    factory: sessionmaker[Session],
    remotes: Path,
    github: FakeGitHub,
    committing_agent: None,
    independent_reviewer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criteria 2 and 3: start to ``awaiting_review`` with one pull request and one
    pending ``pr`` request; approving it attests at once, and the run is synced to
    that attestation later, with the request marked unsynced until then."""
    import scf_worker.temporal_services as services

    # SCF reads CI itself (#280): the head commit's check runs, pending at first.
    check_runs: list[dict[str, Any]] = [{"name": "tests", "status": "in_progress"}]
    ci_reads: list[str] = []

    def read_check_runs(_repo: str, ref: str) -> list[dict[str, Any]]:
        ci_reads.append(ref)
        return check_runs

    monkeypatch.setattr(services, "github_check_runs", read_check_runs)
    task_id = _seed(factory)
    activities = tm.build_activities(factory)
    approver = Principal(kind="user", id=uuid.uuid4(), role=Role.MEMBER, workspace_id=WS)
    decider = Principal(kind="user", id=uuid.uuid4(), role=Role.MEMBER, workspace_id=WS)
    approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)

    with factory() as session:
        engine = TemporalWorkflowEngine(
            workspace_id=WS,
            store=SqlAlchemyWorkflowStore(session, workspace_id=WS),
            client=time_skip_env.client,
            task_queue=TASK_QUEUE,
        )
        approvals = _approvals(factory, engine)
        async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
            run = await engine.astart(task_id)
            session.commit()  # as the API does after start
            assert run.id is not None
            await _wait_awaiting(engine.workflow_handle(run.id), "spec_approved_by_human")
            [spec] = await _pending(factory, GateType.SPEC)
            await approvals.resolve(spec.id, approve, approver, workspace_id=WS)
            await _wait_awaiting(engine.workflow_handle(run.id), "plan_approved_by_human")
            [plan] = await _pending(factory, GateType.PLAN)
            await approvals.resolve(plan.id, approve, approver, workspace_id=WS)

            await _wait_awaiting(engine.workflow_handle(run.id), "review_approved_by_human")

            # Criterion 2: one pull request, one pending pr request, the run at its gate.
            assert len(github.created) == 1
            assert github.created[0]["head"].startswith("scf/")
            # #173: an independent reviewer of another family approved it first.
            assert (
                "Independent review: `approve` by `gpt-6-astra`; the builder ran on "
                "`claude-opus-5-5`." in github.created[0]["body"]
            )
            # #175: the pull request was attested before anyone reviewed it.
            assert _attestations(factory, run.id) == 1
            assert "Attestation `" in github.created[0]["body"]
            [pr] = await _pending(factory, GateType.PR)
            assert pr.workflow_run_id == run.id
            with factory() as fresh:
                waiting = SqlAlchemyWorkflowStore(fresh, workspace_id=WS).get(run.id)
                assert waiting.current_state == "awaiting_review"

            # Criterion 3: approving attests at once. CI is still running on the head
            # commit, so the run stays at the gate and the request says so.
            await approvals.resolve(pr.id, approve, decider, workspace_id=WS)
            unsynced = await approvals.get(pr.id, workspace_id=WS)
            assert unsynced.gate_payload["workflow_event"]["status"] == "failed"
            # Approving records a further attestation naming the approver (#175 criterion 8).
            assert _attestations(factory, run.id) == 2

            # CI reports green on the head commit; sync reads it again and applies the
            # approval with the same attestation.
            check_runs[:] = [{"name": "tests", "status": "completed", "conclusion": "success"}]
            with factory() as sync_session:
                new_state = await sync_gate_event(
                    engine,
                    approvals,
                    sync_session,
                    unsynced,
                    run_state="awaiting_review",
                    fallback_user_id=decider.id,
                )
            assert new_state == "merged"
            synced = await approvals.get(pr.id, workspace_id=WS)
            assert synced.gate_payload["workflow_event"]["status"] == "submitted"
            assert synced.gate_payload["workflow_event"]["actor"] == f"user:{decider.id}"
            assert _attestations(factory, run.id) == 2, "the attestation is reused, not redone"
            assert len(ci_reads) == 2 and len(set(ci_reads)) == 1, "both read the head commit"
            result = await engine.workflow_handle(run.id).result()  # type: ignore[union-attr]

    assert result.final_state.value == "closed"
    assert _git(remotes / "acme" / "app.git", "ls-tree", "--name-only", github.created[0]["head"])


#: The labelled issue, as the tracker returns it when SCF re-fetches it.
ISSUE = ExternalTask(
    provider=ContractsProvider.github_issues,
    external_id="42",
    external_key="acme/app#42",
    url="https://github.com/acme/app/issues/42",
    title="Add a health endpoint",
    description_md="Serve a health check.\n\n## Acceptance criteria\n- health() returns ok\n",
    status_name="open",
    status_category=StatusCategory.unstarted,
    labels=["forge:run"],
    external_updated_at=datetime(2026, 11, 17, 9, 0, tzinfo=UTC),
)


class FakeTracker:
    """GitHub Issues behind the adapter registry: the issue, and what SCF posts back."""

    provider = ContractsProvider.github_issues

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, StatusCategory]] = []

    def map_status(self, value: str, direction: Direction) -> str:
        return StatusCategory.unstarted.value

    def map_priority(self, value: str, direction: Direction) -> str:
        return value

    def map_fields(self, fields: dict[str, Any], direction: Direction) -> dict[str, Any]:
        return dict(fields)

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return ISSUE

    async def comment_external(self, external_id: str, body: str) -> str:
        self.comments.append((external_id, body))
        return str(len(self.comments))

    async def transition_external(self, external_id: str, category: StatusCategory) -> ExternalTask:
        self.transitions.append((external_id, category))
        return ISSUE


@pytest.fixture
def tracker(
    time_skip_env: Any, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeTracker, pm_service_module.PMConnectionService]:
    """The tracker the issue lives on, and the durable engine its dispatch starts runs on."""
    fake = FakeTracker()

    def build_adapter(*_args: Any, **_kw: Any) -> FakeTracker:
        return fake

    monkeypatch.setattr("scf_integrations.pm.registry.build_adapter", build_adapter)
    monkeypatch.setattr(pm_service_module, "build_adapter", build_adapter)
    connections = pm_service_module.PMConnectionService(
        session_factory=factory,
        vault=SecretVault(cipher=HmacAeadCipher(b"0" * 32)),
        audit=AuditLog(),
        transport_factory=lambda _connection: object(),  # type: ignore[arg-type,return-value]
    )
    # The worker's write-back reaches the same tracker.
    monkeypatch.setattr(pm_dispatch, "_connection_service", lambda _factory: connections)

    async def client(_settings: object = None) -> Any:
        return time_skip_env.client

    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", TASK_QUEUE)
    monkeypatch.setattr(workflow_engine_module, "get_temporal_client", client)
    return fake, connections


def _seed_issue_delivery(factory: sessionmaker[Session]) -> uuid.UUID:
    """A connected tracker whose ``forge:run`` label dispatches to ``REPO``, and one
    verified ``issues.labeled`` delivery for issue 42. No board task exists yet."""
    with factory() as s:
        project = _seed_workspace(s)
        connection = PMConnection(
            workspace_id=WS,
            provider=DbPMProvider.GITHUB_ISSUES,
            name="GitHub Issues",
            project_id=project.id,
            external_project_key=REPO,
            external_project_id="10",
            config={
                "dispatch": {
                    "enabled": True,
                    "label": "forge:run",
                    "repository": REPO,
                    "allowed_senders": ["octocat"],
                    "status_on_pr_opened": "started",
                }
            },
            status=PMConnectionStatus.CONNECTED,
        )
        s.add(connection)
        s.flush()
        delivery = PMWebhookDelivery(
            provider=DbPMProvider.GITHUB_ISSUES,
            connection_id=connection.id,
            delivery_id="gh-278",
            event_type="issue.updated",
            external_id="42",
            payload_hash="cafe278",
            signature_valid=True,
            received_at=datetime.now(UTC),
            status=PMDeliveryStatus.RECEIVED,
        )
        s.add(delivery)
        s.commit()
        return delivery.id


#: The adapter's parsed hints for the delivery: who added which label where.
LABELED = {
    "event": "issues",
    "action": "labeled",
    "label": "forge:run",
    "sender_login": "octocat",
    "repository_full_name": REPO,
    "is_pull_request": False,
}


async def test_an_issue_delivery_reaches_the_pr_gate_and_the_issue_hears_of_it(
    time_skip_env: Any,
    factory: sessionmaker[Session],
    remotes: Path,
    github: FakeGitHub,
    committing_agent: None,
    independent_reviewer: None,
    tracker: tuple[FakeTracker, pm_service_module.PMConnectionService],
) -> None:
    """#278 criterion 2: an issue delivery starts a run that waits at the spec gate with
    one pending ``spec`` request and no agent run; once a person approves each gate before
    execution, it reaches ``awaiting_review`` with a delivered branch, one pull request
    and one pending ``pr`` request naming the run. Criterion 1: the issue is told the
    state and the pull request, once."""
    fake_tracker, connections = tracker
    delivery_id = _seed_issue_delivery(factory)
    activities = tm.build_activities(factory)
    approver = Principal(kind="user", id=uuid.uuid4(), role=Role.MEMBER, workspace_id=WS)
    approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)

    with factory() as session:
        engine = TemporalWorkflowEngine(
            workspace_id=WS,
            store=SqlAlchemyWorkflowStore(session, workspace_id=WS),
            client=time_skip_env.client,
            task_queue=TASK_QUEUE,
        )
        approvals = _approvals(factory, engine)
        async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
            # The worker's webhook task: sync the issue to the board, then dispatch it.
            outcome = await asyncio.to_thread(
                process_webhook, factory, delivery_id, connections=connections, payload=LABELED
            )
            assert outcome["dispatch_action"] == "dispatched", outcome
            run_id = uuid.UUID(str(outcome["run_id"]))
            handle = time_skip_env.client.get_workflow_handle(workflow_id(run_id))

            await _wait_awaiting(handle, "spec_approved_by_human")
            [spec] = await _pending(factory, GateType.SPEC)
            assert spec.workflow_run_id == run_id
            with factory() as fresh:
                assert fresh.scalars(select(AgentRun)).all() == [], "no agent before the gate"
            assert fake_tracker.comments == [], "a run waiting at its spec gate has not finished"
            await approvals.resolve(spec.id, approve, approver, workspace_id=WS)
            await _wait_awaiting(handle, "plan_approved_by_human")
            [plan] = await _pending(factory, GateType.PLAN)
            await approvals.resolve(plan.id, approve, approver, workspace_id=WS)

            await _wait_awaiting(handle, "review_approved_by_human")

            with factory() as fresh:
                waiting = SqlAlchemyWorkflowStore(fresh, workspace_id=WS).get(run_id)
            assert waiting.current_state == "awaiting_review"
            assert waiting.context["trigger"]["started_by"] == "github:octocat"
            [created] = github.created
            # The branch reached the remote with the agent's commit on it.
            delivered = _git(
                remotes / "acme" / "app.git", "ls-tree", "--name-only", created["head"]
            )
            assert "health.py" in delivered.split()
            [pr] = await _pending(factory, GateType.PR)
            assert pr.workflow_run_id == run_id

    [(issue, comment)] = fake_tracker.comments
    assert issue == "42"
    assert f"`{run_id}`" in comment and "**awaiting_review**" in comment
    assert "https://github.com/acme/app/pull/1" in comment
    assert fake_tracker.transitions == [("42", StatusCategory.started)]
