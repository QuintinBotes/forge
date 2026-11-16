"""A governed run on the worker's real activities, from start to the pr gate (#253).

``build_activities`` over a database, a local bare mirror, the worktree sandbox and
a scripted model that writes and commits one file; ``FORGE_ALLOW_FAKE_ACTIVITIES``
unset. Only GitHub's HTTP layer is faked, and git really pushes to a local bare
"remote". Runs on the time-skipping Temporal test server and skips (never fakes)
when that server cannot start.
"""

from __future__ import annotations

import asyncio
import functools
import json
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
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
from scf_contracts.orchestration_config import AgentRole, Effort
from scf_db.base import Base
from scf_db.models import Attestation, Project, RepositoryConnection, Task, Workspace
from scf_db.models.role_config import AgentRoleConfig
from scf_worker.finalizer import GitHubAppCredentials
from scf_worker.temporal_services import open_pr_service, run_checks_service
from scf_workflow.store import SqlAlchemyWorkflowStore
from scf_workflow.temporal.engine import TemporalWorkflowEngine
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

    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
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


def _seed(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as s:
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


async def _wait_awaiting(engine: TemporalWorkflowEngine, run_id: uuid.UUID, event: str) -> None:
    handle: Any = engine.workflow_handle(run_id)
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
) -> None:
    """Criteria 2 and 3: start to ``awaiting_review`` with one pull request and one
    pending ``pr`` request; approving it attests at once, and the run is synced to
    that attestation later, with the request marked unsynced until then."""
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

        @contextmanager
        def engine_for(workspace_id: uuid.UUID) -> Iterator[TemporalWorkflowEngine]:
            assert workspace_id == WS
            yield engine

        approvals = ApprovalService(
            SqlAlchemyApprovalRepository(factory),
            build_gate_registry(InMemoryGrantStore(), engine_factory=engine_for),
            ApprovalAuthorizer(),
        )
        async with build_temporal_worker(time_skip_env.client, activities, task_queue=TASK_QUEUE):
            run = await engine.astart(task_id)
            session.commit()  # as the API does after start
            assert run.id is not None
            await _wait_awaiting(engine, run.id, "spec_approved_by_human")
            [spec] = await _pending(factory, GateType.SPEC)
            await approvals.resolve(spec.id, approve, approver, workspace_id=WS)
            await _wait_awaiting(engine, run.id, "plan_approved_by_human")
            [plan] = await _pending(factory, GateType.PLAN)
            await approvals.resolve(plan.id, approve, approver, workspace_id=WS)

            await _wait_awaiting(engine, run.id, "review_approved_by_human")

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

            # Criterion 3: approving attests at once. The merge guard has no CI
            # signals yet, so the run stays at the gate and the request says so.
            await approvals.resolve(pr.id, approve, decider, workspace_id=WS)
            unsynced = await approvals.get(pr.id, workspace_id=WS)
            assert unsynced.gate_payload["workflow_event"]["status"] == "failed"
            # Approving records a further attestation naming the approver (#175 criterion 8).
            assert _attestations(factory, run.id) == 2

            # CI reports green; sync applies the approval with the same attestation.
            unsynced.gate_payload = {
                **unsynced.gate_payload,
                "ci_status_green": True,
                "spec_validated": True,
            }
            await approvals.save_gate_payload(unsynced)
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
            result = await engine.workflow_handle(run.id).result()  # type: ignore[union-attr]

    assert result.final_state.value == "closed"
    assert _git(remotes / "acme" / "app.git", "ls-tree", "--name-only", github.created[0]["head"])
