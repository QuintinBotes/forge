"""``forge.open_pr_with_spec_traceability`` opens the run's pull request once (#252).

Only GitHub's HTTP layer is faked (an ``httpx.MockTransport``). git really pushes,
to a local bare "remote" the credentials' ``git_base_url`` points at.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scf_contracts.attestation import decode_statement
from scf_db.base import Base
from scf_db.models import (
    AgentRun,
    Attestation,
    Project,
    RepositoryConnection,
    Task,
    WorkflowRun,
    Workspace,
)
from scf_db.models.enums import RunStatus
from scf_worker.finalizer import GitHubAppCredentials
from scf_worker.temporal_main import build_activities
from scf_worker.temporal_services import open_pr_service
from scf_workflow.temporal.activities import WorkflowActivities
from scf_workflow.temporal.payloads import OpenPrInput, OpenPrResult

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a8")
REPO = "acme/app"
BRANCH = "scf/run-0123456789ab"
TOKEN = "ghs_" + "t" * 36


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
    """The workspace mirror holds the agent's run branch; ``remotes`` stands in for GitHub."""
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(tmp_path / "mirrors"))
    for name in ("FORGE_ALLOW_FAKE_ACTIVITIES", "FORGE_GITHUB_APP_ID"):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    work.mkdir()
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
    _git(work, "checkout", "-q", "-b", BRANCH)
    (work / "health.py").write_text("def health():\n    return 'ok'\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "agent change")
    _git(work, "push", "-q", str(mirror), BRANCH)  # only the mirror has the run branch
    return tmp_path / "remotes"


def _seed(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(RepositoryConnection(workspace_id=WS, repo_id=REPO, name=REPO))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-12",
            title="Add a health endpoint",
            repo_targets=[{"repo": REPO, "base_branch": "main"}],
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
                # A governed run's builder records the commit its checks ran on.
                output={
                    "status": "succeeded",
                    "artifacts": {"branch": BRANCH, "head_commit": HEAD},
                },
            )
        )
        s.commit()
        return run.id


class FakeGitHub:
    """GitHub's REST API for one repository: token minting, PR lookup and creation."""

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.lookups: list[httpx.QueryParams] = []

    def _pr(self, number: int) -> dict[str, object]:
        return {
            "number": number,
            "html_url": f"https://github.com/acme/app/pull/{number}",
            "state": "open",
            "head": {"ref": BRANCH, "sha": "abc"},
            "base": {"ref": "main"},
            "title": "t",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if request.url.path == "/repos/acme/app/pulls" and request.method == "GET":
            self.lookups.append(request.url.params)
            return httpx.Response(200, json=[self._pr(n) for n in range(len(self.created), 0, -1)])
        if request.url.path == "/repos/acme/app/pulls" and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json=self._pr(len(self.created)))
        return httpx.Response(404)


def _activities(
    factory: sessionmaker[Session], remotes: Path, pem: str, github: FakeGitHub
) -> WorkflowActivities:
    credentials = GitHubAppCredentials(
        app_id="1", installation_id="2", private_key_pem=pem, git_base_url=remotes.as_uri()
    )
    service = open_pr_service(
        factory, credentials=lambda: credentials, transport=httpx.MockTransport(github.handler)
    )
    return WorkflowActivities(open_pr_fn=service)


#: The commit the builder recorded and the checks ran on.
HEAD = "a1" * 20


def _attestations(factory: sessionmaker[Session], run_id: uuid.UUID) -> list[Attestation]:
    with factory() as s:
        return list(s.query(Attestation).filter(Attestation.workflow_run_id == run_id))


def _input(run_id: uuid.UUID) -> OpenPrInput:
    return OpenPrInput(
        workflow_run_id=run_id, workspace_id=WS, branch_name=BRANCH, idempotency_key="pr"
    )


async def test_a_repeated_call_returns_the_same_pull_request_and_creates_one(
    factory: sessionmaker[Session], remotes: Path, pem: str
) -> None:
    run_id = _seed(factory)
    github = FakeGitHub()
    activities = _activities(factory, remotes, pem, github)

    first = await ActivityEnvironment().run(
        activities.open_pr_with_spec_traceability, _input(run_id)
    )
    again = await ActivityEnvironment().run(
        activities.open_pr_with_spec_traceability, _input(run_id)
    )

    assert first == again == OpenPrResult(pr_number=1, pr_url="https://github.com/acme/app/pull/1")
    assert len(github.created) == 1, "a retry must not create a second pull request"
    created = github.created[0]
    assert (created["head"], created["base"]) == (BRANCH, "main")
    assert "CORE-12" in str(created["title"])
    assert str(run_id) in str(created["body"])
    # The run branch reached the remote.
    assert _git(remotes / "acme" / "app.git", "rev-parse", f"refs/heads/{BRANCH}")
    # #175 criterion 4: the retry found the attestation; it did not store a second.
    assert len(_attestations(factory, run_id)) == 1


async def test_the_lookup_asks_for_the_run_branch_before_creating(
    factory: sessionmaker[Session], remotes: Path, pem: str
) -> None:
    run_id = _seed(factory)
    github = FakeGitHub()

    await ActivityEnvironment().run(
        _activities(factory, remotes, pem, github).open_pr_with_spec_traceability, _input(run_id)
    )

    (params,) = github.lookups
    assert params["head"] == f"acme:{BRANCH}"
    assert params["state"] == "all"


async def test_a_run_branch_with_no_commits_is_not_delivered(
    factory: sessionmaker[Session], remotes: Path, pem: str, tmp_path: Path
) -> None:
    """Like the Celery finalizer's ``no_changes``: nothing to push, no pull request."""
    mirror = tmp_path / "mirrors" / str(WS) / "acme" / "app"
    _git(mirror, "branch", "-f", BRANCH, "main")  # the agent committed nothing
    run_id = _seed(factory)
    github = FakeGitHub()

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(
            _activities(factory, remotes, pem, github).open_pr_with_spec_traceability,
            _input(run_id),
        )

    assert caught.value.type == "NoChangesToDeliver"
    assert caught.value.non_retryable is True
    assert github.created == [] and github.lookups == []


async def test_without_github_app_credentials_the_activity_refuses(
    factory: sessionmaker[Session], remotes: Path
) -> None:
    run_id = _seed(factory)

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(
            build_activities(factory).open_pr_with_spec_traceability, _input(run_id)
        )

    assert caught.value.type == "GitHubAppNotConfigured"
    assert caught.value.non_retryable is True


async def test_a_run_outside_the_workspace_is_refused(
    factory: sessionmaker[Session], remotes: Path, pem: str
) -> None:
    _seed(factory)
    activities = _activities(factory, remotes, pem, FakeGitHub())

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(
            activities.open_pr_with_spec_traceability, _input(uuid.uuid4())
        )

    assert caught.value.type == "WorkflowRunNotFound"


async def test_the_pull_request_names_its_signed_attestation(
    factory: sessionmaker[Session], remotes: Path, pem: str
) -> None:
    """#175 criteria 1 to 3: before the pull request opens, one attestation names the
    repository and head commit and records what was verified; the description points
    to it."""
    run_id = _seed(factory)
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        check = {"id": "tests", "passed": True, "exit_code": 0}
        review = {"id": "independent_review", "passed": True, "exit_code": 0}
        run.context = {
            **run.context,
            "checks": [{"attempt": 0, "head_commit": HEAD, "checks": [check, review]}],
            "reviews": [{"attempt": 0, "verdict": "approve", "reviewer_model": "gpt-6-astra"}],
        }
        s.commit()
    github = FakeGitHub()

    await ActivityEnvironment().run(
        _activities(factory, remotes, pem, github).open_pr_with_spec_traceability, _input(run_id)
    )

    [attestation] = _attestations(factory, run_id)
    body = str(github.created[0]["body"])
    assert str(attestation.id) in body
    assert attestation.payload_hash in body and attestation.keyid in body
    statement = decode_statement(attestation.envelope["payload"])
    assert [(s.name, (s.digest.model_extra or {}).get("gitCommit")) for s in statement.subject][
        1:
    ] == [(REPO, HEAD)]
    verification = statement.predicate["verification"]
    assert verification["checks_verdict"] == "passed"
    assert verification["checks"] == [{"id": "tests", "outcome": "passed"}]
    assert verification["reviewer"] == {"verdict": "approve", "model": "gpt-6-astra"}
    # Stages that did not run are named, never omitted and never a pass.
    assert verification["ci"] == "not_run"
    assert verification["red_team"] == {"verdict": "not_run", "model": None}
