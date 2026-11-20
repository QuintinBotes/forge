"""The ``implementation_loop`` line's service bodies, and the line run end to end (#181).

Unit tests call each body as ``forge.run_service_station`` would. The end-to-end
runs use the worker's own composition root on the time-skipping Temporal server,
with scripted agents (the ``run_agent`` seam), a local bare "remote" that git
really pushes to, and only GitHub's HTTP layer faked. They skip, never fake, when
the test server cannot start.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import subprocess
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

import scf_worker.temporal_main as tm
from scf_contracts.lines import (
    AGENT_FORBIDDEN_TOOLS,
    SERVICE_CATALOG,
    AgentStation,
    station_tools,
)
from scf_db.base import Base
from scf_db.models import Project, RepositoryConnection, Task, WorkflowRun, Workspace
from scf_db.models.enums import PMConnectionStatus, PMProvider, PMSyncDirection
from scf_db.models.pm import PMTaskLink
from scf_worker.finalizer import RUN_TOKEN_PERMISSIONS, GitHubAppCredentials, git_push
from scf_worker.line_services import CiPolling, line_services, run_worktree
from scf_worker.run_workspace import run_branch
from scf_workflow.editor.store import bundled_line_text, start_bundled_loader
from scf_workflow.store import SqlAlchemyWorkflowStore
from scf_workflow.temporal.engine import TemporalWorkflowEngine
from scf_workflow.temporal.payloads import (
    AgentRunResultDTO,
    RunAgentInput,
    ServiceStationInput,
)
from scf_workflow.temporal.station_workflow import StationWorkflow
from scf_workflow.temporal.worker import build_temporal_worker

WS = uuid.UUID("00000000-0000-0000-0000-0000000001b1")
REPO = "acme/app"
TOKEN = "ghs_" + "t" * 36
TASK_QUEUE = "scf-181-line"
#: The declared test passes until ``tests/feature.t`` exists, then only with ``feature.txt``.
POLICY = 'repo_id: app\ncommands:\n  test: "test ! -e tests/feature.t || test -e feature.txt"\n'
RED = {"tests/feature.t": "expects feature.txt\n"}
GREEN = {"feature.txt": "done\n"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(worktree: Path, files: dict[str, str], message: str = "change") -> str:
    for path, text in files.items():
        (worktree / path).parent.mkdir(parents=True, exist_ok=True)
        (worktree / path).write_text(text)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "--allow-empty", "-m", message)
    return _git(worktree, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


@pytest.fixture
def make_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """A remote of ``REPO`` with ``files`` on main, and the workspace's bare mirror of it."""
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(tmp_path / "mirrors"))
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    monkeypatch.delenv("FORGE_ALLOW_FAKE_ACTIVITIES", raising=False)

    def make(policy: str = POLICY, files: dict[str, str] | None = None) -> Path:
        work = tmp_path / "work"
        work.mkdir()
        _git(work, "init", "-q", "-b", "main")
        _commit(work, {".forge/policy.yaml": policy, "README.md": "app\n", **(files or {})})
        remote = tmp_path / "remotes" / "acme" / "app.git"
        remote.parent.mkdir(parents=True)
        _git(tmp_path, "clone", "-q", "--bare", str(work), str(remote))
        mirror = tmp_path / "mirrors" / str(WS) / "acme" / "app"
        mirror.parent.mkdir(parents=True)
        _git(tmp_path, "clone", "-q", "--bare", str(remote), str(mirror))
        return mirror

    return make


class FakeGitHub:
    """GitHub's HTTP API for one repository: tokens, pull requests, GraphQL and CI."""

    def __init__(self) -> None:
        self.prs: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.tokens: list[dict[str, Any]] = []
        self.ci: Callable[[str], list[dict[str, Any]]] = lambda _sha: []

    def add_pr(self, head: str, *, draft: bool = True) -> dict[str, Any]:
        n = len(self.prs) + 1
        pr = {
            "number": n,
            "node_id": f"PR_{n}",
            "html_url": f"https://github.com/acme/app/pull/{n}",
            "state": "open",
            "draft": draft,
            "title": "t",
            "head": {"ref": head, "sha": "abc"},
            "base": {"ref": "main"},
        }
        self.prs.append(pr)
        return pr

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/access_tokens"):
            self.tokens.append(body)
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/repos/acme/app/pulls" and method == "GET":
            head = request.url.params["head"].partition(":")[2]
            found = [p for p in reversed(self.prs) if p["head"]["ref"] == head]
            return httpx.Response(200, json=found[:1])
        if path == "/repos/acme/app/pulls" and method == "POST":
            pr = self.add_pr(body["head"], draft=body["draft"])
            return httpx.Response(201, json={**pr, "title": body["title"]})
        if m := re.fullmatch(r"/repos/acme/app/pulls/(\d+)", path):
            return httpx.Response(200, json=self.prs[int(m[1]) - 1])
        if path == "/graphql":
            for pr in self.prs:
                if pr["node_id"] == body["variables"]["id"]:
                    pr["draft"] = False
            return httpx.Response(200, json={"data": {"markPullRequestReadyForReview": {}}})
        if m := re.fullmatch(r"/repos/acme/app/commits/(\w+)/check-runs", path):
            runs = self.ci(m[1])
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        return httpx.Response(404, json={"message": "not faked"})


class FakeTracker:
    """A PM connection service whose one adapter records what it is asked to do."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, Any]] = []
        self.connection = SimpleNamespace(
            workspace_id=WS,
            status=PMConnectionStatus.CONNECTED,
            sync_direction=PMSyncDirection.BIDIRECTIONAL,
            project_id=uuid.uuid4(),
            provider=PMProvider.LINEAR,
            config={"dispatch": {"status_on_pr_opened": "done"}},
        )

    def get_connection_any_workspace(self, _connection_id: uuid.UUID) -> Any:
        return self.connection

    def get_adapter_for_project(self, *_args: Any) -> FakeTracker:
        return self

    async def comment_external(self, external_id: str, body: str) -> str:
        self.comments.append((external_id, body))
        return f"c{len(self.comments)}"

    async def transition_external(self, external_id: str, category: Any) -> None:
        self.transitions.append((external_id, category))


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def tracker() -> FakeTracker:
    return FakeTracker()


@pytest.fixture
def credentials(tmp_path: Path, pem: str) -> GitHubAppCredentials:
    return GitHubAppCredentials(
        app_id="1",
        installation_id="2",
        private_key_pem=pem,
        git_base_url=(tmp_path / "remotes").as_uri(),
    )


@pytest.fixture
def bind(
    factory: sessionmaker[Session],
    credentials: GitHubAppCredentials,
    github: FakeGitHub,
    tracker: FakeTracker,
) -> Callable[..., dict[str, Any]]:
    """The bodies over the fakes; keyword arguments override ``line_services``'s own."""

    def _bind(**overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "credentials": lambda *_: credentials,
            "transport": httpx.MockTransport(github.handler),
            "connections": tracker,
            "ci": CiPolling(poll_seconds=0, heartbeat=lambda *_: None, cancelled=lambda: False),
            **overrides,
        }
        return line_services(factory, **kwargs)

    return _bind


def _seed_task(s: Session) -> Task:
    s.add(Workspace(id=WS, name="Acme", slug="acme"))
    s.add(RepositoryConnection(workspace_id=WS, repo_id=REPO, name=REPO))
    s.flush()
    project = Project(workspace_id=WS, name="Core", key="CORE")
    s.add(project)
    s.flush()
    task = Task(
        workspace_id=WS,
        project_id=project.id,
        key="CORE-81",
        title="Add the feature",
        repo_targets=[{"repo": REPO, "base_branch": "main"}],
    )
    s.add(task)
    s.flush()
    # The task's tracker issue: what a ``forge.tracker_comment`` ticket must name.
    s.add(
        PMTaskLink(
            workspace_id=WS,
            connection_id=uuid.uuid4(),
            forge_task_id=task.id,
            provider=PMProvider.LINEAR,
            external_id="issue-81",
            external_key="ENG-81",
            external_url="https://tracker.example/ENG-81",
        )
    )
    return task


def _seed(factory: sessionmaker[Session]) -> uuid.UUID:
    """A workflow run of the task; returns its id."""
    with factory() as s:
        task = _seed_task(s)
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.commit()
        return run.id


def _inp(name: str, run_id: uuid.UUID, visit: int = 1, **inputs: Any) -> ServiceStationInput:
    return ServiceStationInput(
        workflow_run_id=run_id,
        workspace_id=WS,
        name=name,
        idempotency_key=f"station:{run_id}:{name}:{visit}",
        params={"draft": True} if name == "forge.open_pr" else {},
        inputs=inputs,
    )


def _prepared(services: dict[str, Any], run_id: uuid.UUID, mirror: Path) -> Path:
    assert services["forge.prepare_worktree"](_inp("forge.prepare_worktree", run_id, repo=REPO))
    worktree = run_worktree(mirror, run_branch("forge", run_id))
    assert worktree is not None
    return worktree


# --------------------------------------------------------------------------- #
# Criterion 1: every catalog name is bound                                      #
# --------------------------------------------------------------------------- #


def test_the_worker_binds_a_body_for_every_catalog_service(
    factory: sessionmaker[Session],
) -> None:
    bound = tm.build_activities(factory)._service_bodies
    assert set(bound) == set(SERVICE_CATALOG)
    assert len(SERVICE_CATALOG) == 8


def test_prepare_makes_one_worktree_per_run_on_the_run_branch(
    factory: sessionmaker[Session], make_repo: Callable[..., Path], bind: Any
) -> None:
    mirror, run_id, services = make_repo(), _seed(factory), bind()
    worktree = _prepared(services, run_id, mirror)
    assert _git(worktree, "branch", "--show-current") == f"forge/run-{run_id.hex[:12]}"
    # Idempotent: a retry finds the same worktree.
    assert _prepared(services, run_id, mirror) == worktree
    other = services["forge.prepare_worktree"](_inp("forge.prepare_worktree", run_id, repo="x/y"))
    assert (other.outcome, other.error_code) == ("failed", "repo_mismatch")


# --------------------------------------------------------------------------- #
# Criterion 2: confirm-red                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("base", "branch", "outcome"),
    [
        ({}, RED, "succeeded"),  # the new test fails, the existing tests pass
        ({}, {**RED, **GREEN}, "not_red"),  # the new test already passes
        (RED, GREEN, "failed"),  # the base is not green, though the branch passes
    ],
    ids=["red", "new_tests_pass", "base_fails"],
)
def test_confirm_red_succeeds_only_when_new_tests_fail_on_a_green_base(
    factory: sessionmaker[Session],
    make_repo: Callable[..., Path],
    bind: Any,
    base: dict[str, str],
    branch: dict[str, str],
    outcome: str,
) -> None:
    mirror, run_id, services = make_repo(files=base), _seed(factory), bind()
    _commit(_prepared(services, run_id, mirror), branch)
    result = services["forge.confirm_red"](_inp("forge.confirm_red", run_id))
    assert result.outcome == outcome
    if outcome == "failed":
        assert result.error_code == "base_not_green"


# --------------------------------------------------------------------------- #
# Criterion 3: local checks                                                     #
# --------------------------------------------------------------------------- #


def test_local_checks_run_the_base_policy_on_a_clean_checkout(
    factory: sessionmaker[Session], make_repo: Callable[..., Path], bind: Any
) -> None:
    mirror, run_id, services = make_repo(), _seed(factory), bind()
    worktree = _prepared(services, run_id, mirror)
    # The agent loosens its own policy on the run branch, and leaves the fix uncommitted.
    _commit(worktree, {**RED, ".forge/policy.yaml": 'repo_id: app\ncommands:\n  test: "true"\n'})
    (worktree / "feature.txt").write_text("not committed\n")
    failed = services["forge.run_checks"](_inp("forge.run_checks", run_id))
    assert failed.outcome == "checks_failed"
    assert failed.outputs["failure_summary"].startswith("test (exit 1)")

    _commit(worktree, GREEN)
    passed = services["forge.run_checks"](_inp("forge.run_checks", run_id, visit=2))
    assert (passed.outcome, passed.outputs) == ("succeeded", {})


def test_local_checks_fail_when_no_command_is_declared(
    factory: sessionmaker[Session], make_repo: Callable[..., Path], bind: Any
) -> None:
    mirror, run_id, services = make_repo(policy="repo_id: app\n"), _seed(factory), bind()
    _commit(_prepared(services, run_id, mirror), GREEN)
    result = services["forge.run_checks"](_inp("forge.run_checks", run_id))
    assert (result.outcome, result.error_code) == ("failed", "no_checks_declared")


# --------------------------------------------------------------------------- #
# Criterion 4: branch delivery, plus the draft pull request and mark-ready      #
# --------------------------------------------------------------------------- #


def test_the_draft_pull_request_is_opened_once_and_marked_ready(
    factory: sessionmaker[Session], make_repo: Callable[..., Path], bind: Any, github: FakeGitHub
) -> None:
    mirror, run_id, services = make_repo(), _seed(factory), bind()
    _commit(_prepared(services, run_id, mirror), RED)
    with factory() as s:
        task_id = str(s.get(WorkflowRun, run_id).task_id)  # type: ignore[union-attr]
    first = services["forge.open_pr"](_inp("forge.open_pr", run_id, task=task_id))
    again = services["forge.open_pr"](_inp("forge.open_pr", run_id, task=task_id))
    assert first.outputs == again.outputs == {"pr_ref": "acme/app#1"}
    [pr] = github.prs
    assert (pr["draft"], pr["head"]["ref"]) == (True, f"forge/run-{run_id.hex[:12]}")
    # The target comes from the task alone: another task id is refused.
    other = services["forge.open_pr"](_inp("forge.open_pr", run_id, task=str(uuid.uuid4())))
    assert (other.outcome, other.error_code) == ("failed", "task_mismatch")

    ready = services["forge.mark_pr_ready"](_inp("forge.mark_pr_ready", run_id, pr="acme/app#1"))
    assert ready.outcome == "succeeded"
    assert pr["draft"] is False
    assert github.requests.count(("POST", "/graphql")) == 1


def test_branch_delivery_pushes_only_the_run_branch_with_a_one_repository_token(
    factory: sessionmaker[Session],
    make_repo: Callable[..., Path],
    bind: Any,
    github: FakeGitHub,
    tmp_path: Path,
) -> None:
    pushed: list[tuple[str, str]] = []

    def push(path: Path, branch: str, url: str, token: str) -> None:
        pushed.append((branch, url))
        git_push(path, branch, url, token)

    mirror, run_id, services = make_repo(), _seed(factory), bind(push=push)
    head = _commit(_prepared(services, run_id, mirror), RED)
    branch = f"forge/run-{run_id.hex[:12]}"
    github.add_pr(branch)
    result = services["forge.push_branch"](_inp("forge.push_branch", run_id, pr="acme/app#1"))
    assert result.outcome == "succeeded"
    assert pushed == [(branch, f"{(tmp_path / 'remotes').as_uri()}/acme/app.git")]
    assert _git(tmp_path / "remotes" / "acme" / "app.git", "rev-parse", branch) == head
    assert github.tokens[-1] == {"repositories": ["app"], "permissions": RUN_TOKEN_PERMISSIONS}
    assert all(t["repositories"] == ["app"] for t in github.tokens)  # prepare's read token too

    # A pull request on another ref, or in another repository, is refused unpushed.
    github.add_pr("main", draft=False)
    for pr in ("acme/app#2", "other/app#1", "acme/app"):
        refused = services["forge.push_branch"](_inp("forge.push_branch", run_id, pr=pr))
        assert (refused.outcome, refused.error_code) == ("failed", "ref_refused")
    assert len(pushed) == 1


# --------------------------------------------------------------------------- #
# Criterion 5: the CI wait                                                      #
# --------------------------------------------------------------------------- #


def _run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "output": {"title": name}}


@pytest.mark.parametrize(
    ("polls", "outcome", "summary"),
    [
        ([[_run("tests", "queued", None)], [_run("tests"), _run("lint")]], "succeeded", None),
        (
            [[_run("tests", "in_progress", None), _run("lint", conclusion="failure")]],
            "checks_failed",
            "lint: failure",
        ),
        ([[]] * 4, "no_checks", None),
    ],
    ids=["green_after_a_pending_poll", "one_run_fails", "zero_check_runs"],
)
def test_the_ci_wait_heartbeats_every_poll_and_reads_the_run_branch_head(
    factory: sessionmaker[Session],
    make_repo: Callable[..., Path],
    bind: Any,
    github: FakeGitHub,
    polls: list[list[dict]],
    outcome: str,
    summary: str | None,
) -> None:
    now = [0.0]
    beats: list[Any] = []
    reads: list[str] = []

    def ci(sha: str) -> list[dict]:
        reads.append(sha)
        return polls[min(len(reads), len(polls)) - 1]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    github.ci = ci
    polling = CiPolling(
        poll_seconds=100,
        no_checks_after=300,
        sleep=sleep,
        clock=lambda: now[0],
        heartbeat=lambda *details: beats.append(details),
        cancelled=lambda: False,
    )
    mirror, run_id, services = make_repo(), _seed(factory), bind(ci=polling)
    head = _commit(_prepared(services, run_id, mirror), RED)
    github.add_pr(run_branch("forge", run_id))
    result = services["forge.await_ci"](_inp("forge.await_ci", run_id, pr="acme/app#1"))
    assert result.outcome == outcome
    assert result.outputs == ({"failure_summary": summary} if summary else {})
    assert set(reads) == {head}
    assert len(beats) == len(reads)  # one heartbeat per poll


def test_the_ci_wait_stops_when_cancelled(
    factory: sessionmaker[Session], make_repo: Callable[..., Path], bind: Any, github: FakeGitHub
) -> None:
    reads: list[str] = []
    github.ci = lambda sha: reads.append(sha) or [_run("tests", "queued", None)]
    polling = CiPolling(
        poll_seconds=0, heartbeat=lambda *_: None, cancelled=lambda: len(reads) >= 2
    )
    mirror, run_id, services = make_repo(), _seed(factory), bind(ci=polling)
    _commit(_prepared(services, run_id, mirror), RED)
    github.add_pr(run_branch("forge", run_id))
    result = services["forge.await_ci"](_inp("forge.await_ci", run_id, pr="acme/app#1"))
    assert (result.outcome, result.error_code) == ("failed", "cancelled")
    assert len(reads) == 2


# --------------------------------------------------------------------------- #
# Criterion 6: the tracker comment                                              #
# --------------------------------------------------------------------------- #


def test_the_tracker_comment_posts_once_and_never_moves_the_item(
    factory: sessionmaker[Session], bind: Any, tracker: FakeTracker
) -> None:
    run_id, services = _seed(factory), bind()
    inp = _inp("forge.tracker_comment", run_id, ticket="ENG-81", body="Already satisfied.")
    assert services["forge.tracker_comment"](inp).outcome == "succeeded"
    assert services["forge.tracker_comment"](inp).outcome == "succeeded"  # a retried visit
    [(external_id, body)] = tracker.comments
    assert external_id == "issue-81"
    assert body.startswith("Already satisfied.")
    assert tracker.transitions == []
    # Only the run's own task's issue: any other ticket is refused.
    other = _inp("forge.tracker_comment", run_id, visit=2, ticket="ENG-1", body="x")
    refused = services["forge.tracker_comment"](other)
    assert (refused.outcome, refused.error_code) == ("failed", "ticket_not_linked")
    assert len(tracker.comments) == 1


# --------------------------------------------------------------------------- #
# Criterion 11: no integration, no write tools                                  #
# --------------------------------------------------------------------------- #

#: What an agent station may hold: reads, and writes into its own worktree.
WORKTREE_TOOLS = frozenset(
    {"read_repo", "search_knowledge", "query_mcp", "write_code", "write_tests", "run_tests"}
)


def test_no_agent_station_of_the_line_holds_a_tool_that_writes_outside_its_worktree() -> None:
    line = start_bundled_loader("implementation_loop")
    agents = [s for s in line.stations.values() if isinstance(s, AgentStation)]  # type: ignore[union-attr]
    assert len(agents) == 3
    for station in agents:
        tools = set(station_tools(station.role))
        assert tools <= WORKTREE_TOOLS
        assert not tools & AGENT_FORBIDDEN_TOOLS
    assert not any(entry.merges for entry in SERVICE_CATALOG.values())


# --------------------------------------------------------------------------- #
# Criteria 8 to 10: the line end to end                                         #
# --------------------------------------------------------------------------- #


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


class Agents:
    """Scripted station agents: each commits its visit's files into the run's worktree."""

    def __init__(self, mirror: Path, *, decision: str = "needs_work", implement: Any) -> None:
        self.mirror, self.decision, self.implement = mirror, decision, implement
        self.entered: list[str] = []
        self.objectives: list[str] = []

    def __call__(self, inp: RunAgentInput) -> AgentRunResultDTO:
        state = inp.idempotency_key.split(":")[2]
        self.entered.append(state)
        self.objectives.append(inp.objective or "")
        result = AgentRunResultDTO(agent_run_id=uuid.uuid4(), status="succeeded")
        if state == "assess":
            result.decision, result.summary_ref = self.decision, "Already on main: see feature.txt."
            return result
        files = RED if state == "define_done" else self.implement(inp.attempt)
        worktree = run_worktree(self.mirror, run_branch("forge", inp.workflow_run_id))
        assert worktree is not None  # prepare made it
        _commit(worktree, files, f"{state} {inp.attempt}")
        return result


def _worker(
    env: Any,
    factory: sessionmaker[Session],
    agents: Agents,
    services: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """A worker on the composition root's activities, its agents scripted."""
    monkeypatch.setattr(tm, "agent_run_service", lambda _factory: agents)
    monkeypatch.setattr(tm, "line_services", services)
    return build_temporal_worker(env.client, tm.build_activities(factory), task_queue=TASK_QUEUE)


async def _start_line(env: Any, factory: sessionmaker[Session]) -> tuple[Any, uuid.UUID]:
    """Start the bundled ``implementation_loop`` for a freshly seeded task."""
    with factory() as s:
        task = _seed_task(s)
        s.commit()
        snapshot = {"key": task.key, "title": task.title, "description": "", "repo": REPO}
    session = factory()
    engine = TemporalWorkflowEngine(
        workspace_id=WS,
        store=SqlAlchemyWorkflowStore(session, workspace_id=WS),
        client=env.client,
        task_queue=TASK_QUEUE,
        commit=session.commit,
    )
    line = bundled_line_text("implementation_loop")
    run = await engine.astart(
        task.id,
        "implementation_loop",
        definition_revision_id=uuid.uuid4(),
        dsl_yaml=line,
        task=snapshot,
        trigger={"external_ref": "ENG-81"},
    )
    assert run.id is not None
    return engine.workflow_handle(run.id), run.id


def _transitions(factory: sessionmaker[Session], run_id: uuid.UUID) -> tuple[Any, list]:
    with factory() as s:
        row = s.get(WorkflowRun, run_id)
        assert row is not None
        return row, [(t["from_state"], t["event"]) for t in row.context["transitions"]]


@pytest.fixture
def e2e(
    make_repo: Callable[..., Path],
    credentials: GitHubAppCredentials,
    github: FakeGitHub,
    tracker: FakeTracker,
) -> tuple[Path, Callable[..., Any]]:
    mirror = make_repo()
    services = functools.partial(
        line_services,
        credentials=lambda *_: credentials,
        transport=httpx.MockTransport(github.handler),
        connections=tracker,
        ci=CiPolling(poll_seconds=0),
    )
    return mirror, services


async def test_the_line_retries_failed_checks_and_ci_then_ends_ready_for_review(
    time_skip_env: Any,
    factory: sessionmaker[Session],
    e2e: tuple[Path, Any],
    github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criteria 8 and 11: local checks fail once and CI fails once before the run is ready."""
    mirror, services = e2e
    reads: list[str] = []

    def ci(sha: str) -> list[dict]:
        reads.append(sha)
        return [_run("tests", conclusion="failure" if len(reads) == 1 else "success")]

    github.ci = ci
    agents = Agents(
        mirror, implement=lambda visit: {} if visit == 1 else {"feature.txt": f"{visit}\n"}
    )
    remote = tmp_path / "remotes" / "acme" / "app.git"
    main = _git(remote, "rev-parse", "main")
    async with _worker(time_skip_env, factory, agents, services, monkeypatch):
        handle, run_id = await _start_line(time_skip_env, factory)
        assert await handle.result() == "ready_for_review"

    row, path = _transitions(factory, run_id)
    assert path == [
        ("prepare", "succeeded"),
        ("assess", "needs_work"),
        ("define_done", "succeeded"),
        ("confirm_red", "succeeded"),
        ("open_draft_pr", "succeeded"),
        ("implement", "succeeded"),
        ("local_checks", "checks_failed"),
        ("implement", "succeeded"),
        ("local_checks", "succeeded"),
        ("push", "succeeded"),
        ("await_ci", "checks_failed"),
        ("implement", "succeeded"),
        ("local_checks", "succeeded"),
        ("push", "succeeded"),
        ("await_ci", "succeeded"),
        ("mark_ready", "succeeded"),
    ]
    assert (row.current_state, row.status.value) == ("ready_for_review", "succeeded")
    assert agents.entered.count("implement") == 3
    assert "tests: failure" in agents.objectives[-1]  # the last CI failure reached the agent
    [pr] = github.prs
    assert (pr["draft"], pr["head"]["ref"]) == (False, f"forge/run-{run_id.hex[:12]}")
    # Criterion 11: main never moved, and nothing asked GitHub to merge.
    assert _git(remote, "rev-parse", "main") == main
    assert not [r for r in github.requests if "merge" in r[1]]


async def test_ci_failing_every_time_parks_the_run_at_the_implementer_visit_limit(
    time_skip_env: Any,
    factory: sessionmaker[Session],
    e2e: tuple[Path, Any],
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 9: six implementer visits, then ``visit_limit`` parks; the PR stays a draft."""
    mirror, services = e2e
    github.ci = lambda _sha: [_run("tests", conclusion="failure")]
    agents = Agents(mirror, implement=lambda visit: {"feature.txt": f"{visit}\n"})
    async with _worker(time_skip_env, factory, agents, services, monkeypatch):
        handle, run_id = await _start_line(time_skip_env, factory)
        for _ in range(3000):
            if await handle.query(StationWorkflow.awaiting) == ["resume"]:
                break
            await asyncio.sleep(0.05)

    row, path = _transitions(factory, run_id)
    assert agents.entered.count("implement") == 6
    assert path[-1] == ("implement", "visit_limit")
    assert (row.current_state, row.status.value) == ("implement", "escalated")
    assert row.context["parked"] == {"state": "implement", "visit": 7, "reason": "visit_limit"}
    [pr] = github.prs
    assert pr["draft"] is True
    assert ("POST", "/graphql") not in github.requests


async def test_already_done_posts_one_tracker_comment_and_opens_no_pull_request(
    time_skip_env: Any,
    factory: sessionmaker[Session],
    e2e: tuple[Path, Any],
    github: FakeGitHub,
    tracker: FakeTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 10: ``already_done`` ends in ``done_proposed`` with one comment and no PR."""
    mirror, services = e2e
    agents = Agents(mirror, decision="already_done", implement=lambda _visit: GREEN)
    async with _worker(time_skip_env, factory, agents, services, monkeypatch):
        handle, run_id = await _start_line(time_skip_env, factory)
        assert await handle.result() == "done_proposed"

    row, path = _transitions(factory, run_id)
    assert path == [
        ("prepare", "succeeded"),
        ("assess", "already_done"),
        ("propose_done", "succeeded"),
    ]
    assert row.status.value == "succeeded"
    [(external_id, body)] = tracker.comments
    assert (external_id, body.split("\n")[0]) == ("issue-81", "Already on main: see feature.txt.")
    assert tracker.transitions == []
    assert github.prs == []
    assert agents.entered == ["assess"]
