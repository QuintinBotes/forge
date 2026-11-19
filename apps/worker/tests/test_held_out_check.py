"""A governed run's approved held-out scenarios run on its change, hidden from the builder (#286).

Through ``build_activities`` with ``FORGE_ALLOW_FAKE_ACTIVITIES`` unset, on a local
bare mirror and the worktree sandbox (tests only). The builder's run is seeded as
``forge.run_agent`` records it; an independent reviewer of another family approves
and the red-team scan is turned off, so the held-out scenarios are the last check.
"""

from __future__ import annotations

import functools
import json
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.testing import ActivityEnvironment

import scf_worker.agent_runner as agent_runner_module
from scf_agent.testing import ScriptedModelClient, finish_response
from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_api.observability.audit import AuditLog as McpAuditLog
from scf_api.routers.board import BoardServiceRegistry, get_board_registry
from scf_api.routers.knowledge import get_knowledge_service
from scf_api.routers.mcp_server import get_mcp_server_audit_log
from scf_api.routers.spec import get_spec_engine
from scf_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from scf_api.services.approval_service import build_gate_registry, get_approval_service
from scf_api.services.attestation_service import AttestationService
from scf_approval import ApprovalAuthorizer, ApprovalService
from scf_approval.providers import InMemoryGrantStore
from scf_contracts import UserRole
from scf_contracts.attestation import decode_statement
from scf_contracts.orchestration_config import AgentRole, Effort
from scf_db.base import Base
from scf_db.models import (
    AgentRun,
    AgentRunEvent,
    Project,
    RepositoryConnection,
    Task,
    WorkflowRun,
    Workspace,
)
from scf_db.models.audit import AuditLog
from scf_db.models.enums import RunStatus
from scf_db.models.held_out import (
    SCENARIO_APPROVED,
    SCENARIO_DRAFT,
    SCENARIO_REJECTED,
    SCENARIO_WITHDRAWN,
    HeldOutScenario,
)
from scf_db.models.role_config import AgentRoleConfig
from scf_knowledge import KnowledgeService
from scf_mcp.transports.http import HttpMcpTransport
from scf_spec import FileSpecEngine
from scf_worker import temporal_main
from scf_worker.temporal_main import build_activities
from scf_worker.temporal_services import _failing_checks, run_checks_service
from scf_workflow.temporal.payloads import (
    ApprovalInput,
    ChecksResult,
    RunAgentInput,
    RunChecksInput,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000002e6")
REPO = "acme/app"
BRANCH = "scf/run-286286286286"
POLICY = 'repo_id: app\ncommands:\n  tests: "test -f health.py"\n'
#: Placed in each scenario's files and command: it must reach no record the builder sees.
MARKER = "HELD-OUT-MARKER-286-c4f1"
TITLE = "Health answers ok"
PASSING = [{"path": "held_out/check.sh", "content": f"# {MARKER}\ngrep -q ok health.py\n"}]
FAILING = [{"path": "held_out/check.sh", "content": f"# {MARKER}\ngrep -q ready health.py\n"}]
COMMAND = f"sh held_out/check.sh # {MARKER}"


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    # A builder run records itself through the worker's global factory.
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: session_factory)
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
    """A bare mirror whose main declares the checks; the run branch adds health.py."""
    root = tmp_path / "mirrors"
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(root))
    monkeypatch.setenv("FORGE_SANDBOX_KIND", "worktree")
    monkeypatch.setenv("FORGE_ALLOW_SCRIPTED_AGENT", "1")
    for name in (
        "FORGE_ALLOW_FAKE_ACTIVITIES",
        "FORGE_GITHUB_APP_ID",
        "FORGE_MODEL_PROVIDER",
        "FORGE_RECORD_RUNS",
    ):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    (work / ".forge").mkdir(parents=True)
    (work / ".forge" / "policy.yaml").write_text(POLICY)
    (work / "README.md").write_text("app\n")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", BRANCH)
    (work / "health.py").write_text("def health():\n    return 'ok'\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "agent change")
    _git(work, "checkout", "-q", "main")
    path = root / str(WS) / "acme" / "app"
    path.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(path))
    return path


@pytest.fixture(autouse=True)
def reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An independent reviewer that approves, so the held-out scenarios are reached."""

    def approve(_model: str) -> ScriptedModelClient:
        return ScriptedModelClient([finish_response('{"verdict": "approve", "findings": []}')])

    monkeypatch.setattr(
        temporal_main, "run_checks_service", functools.partial(run_checks_service, reviewer=approve)
    )


def _seed(factory: sessionmaker[Session], mirror: Path) -> tuple[uuid.UUID, uuid.UUID, str]:
    """``(workflow run, task, head)``: the builder recorded the run branch's head."""
    head = _git(mirror, "rev-parse", BRANCH)
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(RepositoryConnection(workspace_id=WS, repo_id=REPO, name=REPO))
        for role, model in ((AgentRole.REVIEWER, "gpt-6-astra"), (AgentRole.ADVERSARY, "off")):
            s.add(
                AgentRoleConfig(workspace_id=WS, role=role, model_or_tier=model, effort=Effort.HIGH)
            )
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-286",
            title="Add a health endpoint",
            repo_targets=[{"repo": REPO, "base_branch": "main", "branch_prefix": "scf"}],
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        s.flush()
        s.add(
            AgentRun(
                workspace_id=WS,
                workflow_run_id=run.id,
                task_id=task.id,
                status=RunStatus.SUCCEEDED,
                model="claude-opus-5-5",
                inputs={"acceptance_criteria": [{"id": "AC-1", "text": "health() is ok"}]},
                output={
                    "status": "succeeded",
                    "artifacts": {"branch": BRANCH, "head_commit": head},
                },
            )
        )
        s.commit()
        return run.id, task.id, head


def _scenario(
    factory: sessionmaker[Session],
    task_id: uuid.UUID,
    *,
    files: list[dict[str, str]] = PASSING,
    command: str = COMMAND,
    state: str = SCENARIO_APPROVED,
    workflow_run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    with factory() as s:
        row = HeldOutScenario(
            workspace_id=WS,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            title=TITLE,
            files=files,
            command=command,
            drafted_by="user:someone",
            state=state,
        )
        s.add(row)
        s.commit()
        return row.id


async def _checks(factory: sessionmaker[Session], run_id: uuid.UUID) -> ChecksResult:
    inp = RunChecksInput(workflow_run_id=run_id, workspace_id=WS, attempt=0, idempotency_key="c0")
    result = await ActivityEnvironment().run(build_activities(factory).run_checks, inp)
    assert isinstance(result, ChecksResult)
    return result


def _results(factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    """Each scenario's audited outcome, oldest first."""
    with factory() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.action == "held_out.ran"))
        return [dict(row.details) for row in rows]


def _context(factory: sessionmaker[Session], run_id: uuid.UUID) -> dict[str, Any]:
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        return dict(run.context)


def _attempt(factory: sessionmaker[Session], run_id: uuid.UUID) -> dict[str, Any]:
    [attempt] = _context(factory, run_id)["checks"]
    return dict(attempt)


async def test_an_approved_scenario_runs_on_a_fresh_checkout_of_the_head_commit(
    factory: sessionmaker[Session], mirror: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 1: after the checks and the review pass, the scenario's files are
    overlaid on a fresh worktree of the head commit and its command runs in a sandbox
    with no network; the outcome is recorded by scenario id, and the overlay never
    reaches the run branch."""
    import scf_worker.run_workspace as run_workspace_module

    networks: list[str] = []
    real = run_workspace_module.build_sandbox_provider

    def recording(settings: Any) -> Any:
        provider = real(settings)
        create = provider.create

        async def create_and_record(spec: Any) -> Any:
            networks.append(str(spec.network))
            return await create(spec)

        provider.create = create_and_record  # type: ignore[method-assign]
        return provider

    monkeypatch.setattr(run_workspace_module, "build_sandbox_provider", recording)
    run_id, task_id, head = _seed(factory, mirror)
    scenario_id = _scenario(factory, task_id)

    result = await _checks(factory, run_id)

    assert result.results == {"tests": True, "independent_review": True, "held_out_scenarios": True}
    assert result.all_passed and result.pause_reason is None
    [outcome] = _results(factory)
    assert outcome["scenario_id"] == str(scenario_id)
    assert outcome["workflow_run_id"] == str(run_id)
    assert (outcome["head_commit"], outcome["attempt"]) == (head, 0)
    assert (outcome["passed"], outcome["exit_code"], outcome["timed_out"]) == (True, 0, False)
    assert isinstance(outcome["duration_ms"], int) and outcome["reason"] is None
    assert networks == ["none", "none"], "the declared checks' sandbox, then the scenario's"
    assert len(_git(mirror, "worktree", "list").splitlines()) == 1, "both worktrees removed"
    assert _git(mirror, "rev-parse", BRANCH) == head
    assert "held_out/check.sh" not in _git(mirror, "ls-tree", "-r", "--name-only", BRANCH)
    assert _attempt(factory, run_id)["held_out"] == {
        "verdict": "passed",
        "defined": 1,
        "passed": 1,
        "failed": 0,
    }


async def test_only_the_runs_approved_scenarios_run(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    """Criterion 1: a person's approved scenario for the task and the verifier's
    approved draft for this run run; a draft, a rejected or withdrawn scenario, and
    one drafted for another run of the task do not."""
    run_id, task_id, _ = _seed(factory, mirror)
    with factory() as s:
        other = WorkflowRun(
            workspace_id=WS, task_id=task_id, engine_backend="temporal", status=RunStatus.FAILED
        )
        s.add(other)
        s.commit()
        other_id = other.id
    persons = _scenario(factory, task_id)
    verifiers = _scenario(factory, task_id, workflow_run_id=run_id)
    for state in (SCENARIO_DRAFT, SCENARIO_REJECTED, SCENARIO_WITHDRAWN):
        _scenario(factory, task_id, files=FAILING, state=state)
    _scenario(factory, task_id, files=FAILING, workflow_run_id=other_id)

    result = await _checks(factory, run_id)

    assert result.results["held_out_scenarios"] is True
    assert sorted(r["scenario_id"] for r in _results(factory)) == sorted(
        [str(persons), str(verifiers)]
    )
    # Defined: every scenario of the run not withdrawn, whatever its state.
    assert _attempt(factory, run_id)["held_out"] == {
        "verdict": "passed",
        "defined": 4,
        "passed": 2,
        "failed": 0,
    }


async def test_a_failing_scenario_stops_the_pull_request_and_the_builder_learns_only_the_count(
    factory: sessionmaker[Session], mirror: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 2: the check fails, so the run goes back to the builder, not to a pull
    request. The feedback path leaves the check out; the next objective carries the
    count of failing scenarios and nothing else about them."""
    run_id, task_id, _ = _seed(factory, mirror)
    _scenario(factory, task_id, files=FAILING)
    _scenario(factory, task_id)

    result = await _checks(factory, run_id)

    assert result.results["held_out_scenarios"] is False and not result.all_passed
    context = _context(factory, run_id)
    assert _failing_checks(context, 1) == []
    builder = ScriptedModelClient([], default=finish_response("Done", confidence=0.9))
    monkeypatch.setattr(agent_runner_module, "_scripted_client", lambda: builder)
    retry = RunAgentInput(run_id, task_id, WS, attempt=1, idempotency_key="k-1")
    again = await ActivityEnvironment().run(build_activities(factory).run_agent, retry)

    with factory() as s:
        row = s.get(AgentRun, again.agent_run_id)
        assert row is not None
        objective = row.inputs["context"]
    assert objective["held_out_failed"] == 1
    assert "failing_checks" not in objective
    [first, *_] = builder.requests
    assert first.system is not None
    assert "1 held-out acceptance scenario(s) failed" in first.system
    assert not any(MARKER in r.model_dump_json() for r in builder.requests)
    assert MARKER not in json.dumps(row.inputs) and TITLE not in json.dumps(row.inputs)


@pytest.mark.parametrize(
    ("command", "reason", "timed_out"),
    [("sleep 5", "timed_out", True), ("no-such-command-286", "could_not_start", False)],
)
async def test_a_scenario_that_times_out_or_cannot_start_fails_with_its_reason(
    factory: sessionmaker[Session],
    mirror: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    reason: str,
    timed_out: bool,
) -> None:
    """Criterion 3: it counts as failed, with the reason recorded."""
    monkeypatch.setenv("FORGE_SANDBOX_EXEC_TIMEOUT_SECONDS", "1")
    run_id, task_id, _ = _seed(factory, mirror)
    _scenario(factory, task_id, command=command)

    result = await _checks(factory, run_id)

    assert result.results["held_out_scenarios"] is False
    [outcome] = _results(factory)
    assert (outcome["passed"], outcome["reason"], outcome["timed_out"]) == (
        False,
        reason,
        timed_out,
    )
    assert _attempt(factory, run_id)["held_out"]["failed"] == 1


async def test_a_file_the_change_points_out_of_the_checkout_is_not_written(
    factory: sessionmaker[Session], mirror: Path, tmp_path: Path
) -> None:
    """The change may commit a symlink out of the checkout: the overlay refuses to
    follow it, and the scenario fails as one that could not start."""
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    _git(work, "checkout", "-q", BRANCH)
    (work / "held_out").symlink_to(outside)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "a link out")
    _git(work, "push", "-q", "-f", str(mirror), BRANCH)
    run_id, task_id, _ = _seed(factory, mirror)
    _scenario(factory, task_id)

    result = await _checks(factory, run_id)

    assert result.results["held_out_scenarios"] is False
    [outcome] = _results(factory)
    assert (outcome["reason"], outcome["exit_code"]) == ("could_not_start", None)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("states", "verdict"),
    [([], "none_defined"), ([SCENARIO_DRAFT, SCENARIO_DRAFT], "none_approved")],
)
async def test_with_no_approved_scenario_the_run_proceeds_and_nothing_reports_a_pass(
    factory: sessionmaker[Session], mirror: Path, states: list[str], verdict: str
) -> None:
    """Criterion 4: no scenario records ``held_out: none_defined``; only drafts record
    ``none_approved``. The run proceeds; no check, result or attestation says passed."""
    run_id, task_id, head = _seed(factory, mirror)
    for state in states:
        _scenario(factory, task_id, state=state)

    result = await _checks(factory, run_id)

    assert result.all_passed and "held_out_scenarios" not in result.results
    assert _context(factory, run_id)["held_out"] == verdict
    assert _attempt(factory, run_id)["held_out"] == {
        "verdict": verdict,
        "defined": len(states),
        "passed": 0,
        "failed": 0,
    }
    assert _results(factory) == []
    with factory() as s:
        attestation = AttestationService(s).attest_pull_request(
            run_id, repository=REPO, head_commit=head
        )
        s.commit()
    verification = decode_statement(attestation.envelope["payload"]).predicate["verification"]
    assert verification["held_out"]["verdict"] == verdict
    assert verification["held_out"]["passed"] == 0


async def test_the_per_scenario_results_stay_off_the_run_and_the_attestation_counts_them(
    factory: sessionmaker[Session], mirror: Path
) -> None:
    """Criteria 7 and 8: the run's context, which readers get, holds counts and no
    scenario id, title or content; the attestation records ``defined``, ``passed``
    and ``failed`` and no scenario id or title."""
    run_id, task_id, head = _seed(factory, mirror)
    failing = _scenario(factory, task_id, files=FAILING)
    passing = _scenario(factory, task_id)

    await _checks(factory, run_id)

    context = json.dumps(_context(factory, run_id))
    for hidden in (str(failing), str(passing), TITLE, MARKER):
        assert hidden not in context
    with factory() as s:
        attestation = AttestationService(s).attest_pull_request(
            run_id, repository=REPO, head_commit=head
        )
        s.commit()
    statement = decode_statement(attestation.envelope["payload"])
    assert statement.predicate["verification"]["held_out"] == {
        "verdict": "failed",
        "defined": 2,
        "passed": 1,
        "failed": 1,
    }
    payload = json.dumps(statement.model_dump(mode="json"))
    for hidden in (str(failing), str(passing), TITLE, MARKER):
        assert hidden not in payload


def _surfaces(
    factory: sessionmaker[Session], tmp_path: Path, run_id: uuid.UUID, project_id: uuid.UUID
) -> dict[str, str]:
    """What ``GET /agent/runs/{id}`` and each MCP read tool return for the run, by call,
    over the real database-backed stores, to an admin (who reads all a builder may)."""
    app = create_app()
    principal = Principal(
        user_id=uuid.uuid4(), workspace_id=WS, role=UserRole.ADMIN, auth_method="test", scopes=["*"]
    )

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    approvals = ApprovalService(
        SqlAlchemyApprovalRepository(factory),
        build_gate_registry(InMemoryGrantStore()),
        ApprovalAuthorizer(),
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_board_registry] = lambda: BoardServiceRegistry(
        backend="db", session_factory=factory
    )
    app.dependency_overrides[get_approval_service] = lambda: approvals
    app.dependency_overrides[get_spec_engine] = lambda: FileSpecEngine(root=tmp_path / "specs")
    app.dependency_overrides[get_knowledge_service] = lambda: KnowledgeService.from_session_factory(
        factory, None, None
    )
    app.dependency_overrides[get_mcp_server_audit_log] = lambda: McpAuditLog()
    client = TestClient(app)
    with factory() as s:
        agent_runs = list(s.scalars(select(AgentRun.id).where(AgentRun.workflow_run_id == run_id)))
    seen: dict[str, str] = {}
    for agent_run_id in agent_runs:
        response = client.get(f"/agent/runs/{agent_run_id}")
        assert response.status_code == 200, response.text
        seen[f"GET /agent/runs/{agent_run_id}"] = response.text
    with HttpMcpTransport("http://testserver/mcp", client=client) as mcp:
        calls: list[tuple[str, dict[str, Any]]] = [
            ("run_status", {"run_id": str(agent_run_id)}) for agent_run_id in agent_runs
        ]
        calls += [
            ("board_items", {}),
            ("approvals_pending", {}),
            ("assemble_context", {"task": f"{TITLE}: add a health endpoint"}),
            ("spec_list", {"project_id": str(project_id)}),
        ]
        for name, args in calls:
            result = mcp.call_tool(name, args)
            assert result.get("isError") is not True, (name, result)
            seen[f"{name} {json.dumps(args)}"] = json.dumps(result)
    return seen


async def test_no_agent_facing_surface_returns_scenario_content(
    factory: sessionmaker[Session], mirror: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6: after a scenario ran and failed and the builder ran again, ``GET
    /agent/runs/{id}``, ``assemble_context`` and the factory tools return nothing of
    its files or command, read over the stores they really read."""
    run_id, task_id, _ = _seed(factory, mirror)
    _scenario(factory, task_id, files=FAILING, workflow_run_id=run_id)
    # The run's spec gate, opened as #288 opens it (its payload names the scenario).
    approval = ApprovalInput(workflow_run_id=run_id, workspace_id=WS, task_id=task_id, gate="spec")
    await ActivityEnvironment().run(build_activities(factory).create_approval, approval)
    await _checks(factory, run_id)
    builder = ScriptedModelClient([], default=finish_response("Done", confidence=0.9))
    monkeypatch.setattr(agent_runner_module, "_scripted_client", lambda: builder)
    retry = RunAgentInput(run_id, task_id, WS, attempt=1, idempotency_key="k-1")
    await ActivityEnvironment().run(build_activities(factory).run_agent, retry)
    with factory() as s:
        project_id = s.scalars(select(Project.id)).one()
        events = [json.dumps(e.payload) for e in s.scalars(select(AgentRunEvent))]

    seen = _surfaces(factory, tmp_path, run_id, project_id)

    # Each surface read something real: the task, the gate, both builder runs.
    assert "Add a health endpoint" in seen["board_items {}"]
    assert '"spec"' in seen["approvals_pending {}"]
    assert sum('"succeeded"' in text for call, text in seen.items() if call.startswith("GET")) >= 2
    assert events, "the builder's runs recorded events"
    for text in [*seen.values(), *events]:
        assert MARKER not in text
        assert "grep -q ready" not in text
