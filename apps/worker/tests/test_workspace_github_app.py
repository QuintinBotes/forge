"""GitHub App credentials per workspace and per-run scoped tokens (#186).

Only GitHub's HTTP layer is faked (an ``httpx.MockTransport``); the vault is the
API's own ``SecretVault`` and the audit lands in a real ``audit_log`` table.
"""

from __future__ import annotations

import functools
import json
import logging
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.auth.crypto import FernetCipher
from scf_api.auth.vault import SecretVault
from scf_api.settings import get_settings
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_contracts.enums import APIKeyKind
from scf_db.base import Base
from scf_db.models import AuditLog, RepositoryConnection
from scf_integrations.errors import GitHubError
from scf_integrations.github_auth import WORKSPACE_SECRET_NAME, InstallationTokenProvider
from scf_worker import agent_runner as agent_runner_module
from scf_worker import dispatch as dispatch_module
from scf_worker import finalizer as finalizer_module
from scf_worker.dispatch import MIRROR_TOKEN_PERMISSIONS, RepositoryUnavailable
from scf_worker.finalizer import (
    RUN_TOKEN_PERMISSIONS,
    GitHubAppCredentials,
    push_and_open_pr,
    workspace_credentials,
)

WS_A = uuid.UUID("00000000-0000-0000-0000-00000000186a")
WS_B = uuid.UUID("00000000-0000-0000-0000-00000000186b")
REPO = "acme/app"


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def keys() -> dict[str, str]:
    return {"a": _pem(), "b": _pem(), "env": _pem()}


@dataclass
class Mint:
    installation: str
    app_id: str
    body: dict[str, Any]
    token: str
    app_jwt: str


class FakeGitHub:
    """GitHub's REST API: a fresh token per mint, pull requests and check runs."""

    def __init__(self) -> None:
        self.mints: list[Mint] = []
        self.ci_reads: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            app_jwt = request.headers["authorization"].removeprefix("Bearer ")
            claims = jwt.decode(app_jwt, options={"verify_signature": False})
            token = f"ghs_{len(self.mints):04d}" + "t" * 32
            self.mints.append(
                Mint(path.split("/")[3], claims["iss"], json.loads(request.content), token, app_jwt)
            )
            return httpx.Response(201, json={"token": token, "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"/repos/{REPO}/pulls" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "number": 1,
                    "html_url": f"https://github.com/{REPO}/pull/1",
                    "state": "open",
                    "head": {"ref": "b", "sha": "abc"},
                    "base": {"ref": "main"},
                    "title": "t",
                },
            )
        if path.startswith(f"/repos/{REPO}/commits/") and path.endswith("/check-runs"):
            self.ci_reads.append(request.headers["authorization"])
            run = {"name": "tests", "status": "completed", "conclusion": "success"}
            return httpx.Response(200, json={"total_count": 1, "check_runs": [run]})
        return httpx.Response(404)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _store(vault: SecretVault, ws: uuid.UUID, app_id: str, key: str, installation: str) -> None:
    payload = {"app_id": app_id, "installation_id": installation, "private_key_pem": key}
    vault.put_secret(
        workspace_id=ws,
        name=WORKSPACE_SECRET_NAME,
        secret=json.dumps(payload),
        kind=APIKeyKind.INTEGRATION_TOKEN,
        provider="github",
    )


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> SecretVault:
    vault = SecretVault(cipher=FernetCipher(b"v" * 32))
    monkeypatch.setattr(finalizer_module, "_workspace_vault", lambda: vault)
    return vault


@pytest.fixture
def github(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], vault: SecretVault
) -> FakeGitHub:
    """Every token request goes to the fake; lookups and audit use the test database."""
    fake = FakeGitHub()
    transport = httpx.MockTransport(fake.handler)

    def provider(**kwargs: Any) -> InstallationTokenProvider:
        return InstallationTokenProvider(**{**kwargs, "transport": transport})

    monkeypatch.setattr(finalizer_module, "InstallationTokenProvider", provider)
    monkeypatch.setattr(finalizer_module, "create_session_factory", lambda: factory)
    monkeypatch.setattr(
        agent_runner_module,
        "push_and_open_pr",
        functools.partial(push_and_open_pr, transport=transport, push=lambda *a: None),
    )
    return fake


@pytest.fixture
def two_workspaces(
    vault: SecretVault, factory: sessionmaker[Session], keys: dict[str, str]
) -> None:
    """A's run repository names installation 222 (over its stored 111); B stores 333."""
    _store(vault, WS_A, "11", keys["a"], "111")
    _store(vault, WS_B, "22", keys["b"], "333")
    with factory() as s:
        s.add(
            RepositoryConnection(workspace_id=WS_A, repo_id=REPO, name=REPO, installation_id="222")
        )
        s.add(RepositoryConnection(workspace_id=WS_B, repo_id=REPO, name=REPO))
        s.commit()


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _set(name: str) -> None:
        monkeypatch.setenv("FORGE_ENVIRONMENT", name)
        monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "postgres_fsm")
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def env_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keys: dict[str, str]) -> None:
    """The process-wide FORGE_GITHUB_* variables name a third installation, 999."""
    key = tmp_path / "app.pem"
    key.write_text(keys["env"])
    monkeypatch.setenv("FORGE_GITHUB_APP_ID", "99")
    monkeypatch.setenv("FORGE_GITHUB_INSTALLATION_ID", "999")
    monkeypatch.setenv("FORGE_GITHUB_APP_PRIVATE_KEY_PATH", str(key))


@pytest.fixture
def mirrors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
    """``ensure_mirror`` records the remote and token instead of fetching."""
    monkeypatch.setenv("FORGE_REPO_MIRRORS_DIR", str(tmp_path / "mirrors"))
    fetched: list[tuple[str, str]] = []

    def ensure(full_name: str, *, root: Path, remote_url: str, token: str | None) -> Path:
        fetched.append((remote_url, str(token)))
        return root / full_name

    monkeypatch.setattr(dispatch_module, "ensure_mirror", ensure)
    return fetched


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone whose run branch is one commit ahead of main."""
    root = tmp_path / "clone"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "checkout", "-q", "-b", "scf/run-1")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    _git(root, "checkout", "-q", "main")
    return root


def _objective(ws: uuid.UUID, run: uuid.UUID, repo_path: Path | None = None) -> AgentObjective:
    context: dict[str, Any] = {
        "workspace_id": str(ws),
        "agent_run_id": str(run),
        "repo_full_name": REPO,
    }
    if repo_path is not None:
        context["repo_path"] = str(repo_path)
    return AgentObjective(objective="Add b", context=context)


def _fetch(ws: uuid.UUID, run: uuid.UUID) -> None:
    dispatch_module.resolve_run_repository(_objective(ws, run))


def _deliver(ws: uuid.UUID, run: uuid.UUID, repo: Path) -> AgentRunResult:
    result = AgentRunResult(status=RunStatus.SUCCEEDED, output="done")
    agent_runner_module._finalize(_objective(ws, run, repo), result, branch="scf/run-1", run_id=run)
    return result


def _issued(factory: sessionmaker[Session]) -> list[AuditLog]:
    with factory() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.action == "github.token_issued"))
        return list(rows)


# --------------------------------------------------------------------------- #
# Criterion 1: each workspace's own installation                              #
# --------------------------------------------------------------------------- #


def test_each_workspace_mints_only_from_its_own_installation(
    github: FakeGitHub,
    two_workspaces: None,
    env_app: None,
    mirrors: list[tuple[str, str]],
    repo: Path,
    environment: Any,
) -> None:
    """Mirror fetch and delivery for two workspaces: no token request names the
    other's installation, and the vault wins over the env even in development."""
    environment("development")
    for ws in (WS_A, WS_B):
        _fetch(ws, uuid.uuid4())
        assert _deliver(ws, uuid.uuid4(), repo).artifacts["pull_request"]["status"] == "opened"

    by_app = {(m.app_id, m.installation) for m in github.mints}
    assert by_app == {("11", "222"), ("22", "333")}
    assert len(github.mints) == 4


def test_the_installation_comes_from_the_run_repository_connection_when_present(
    two_workspaces: None, factory: sessionmaker[Session], vault: SecretVault
) -> None:
    resolved_a = workspace_credentials(WS_A, REPO, session_factory=factory)
    resolved_b = workspace_credentials(WS_B, REPO, session_factory=factory)
    assert resolved_a is not None and resolved_b is not None
    assert (resolved_a.app_id, resolved_a.installation_id) == ("11", "222")
    assert (resolved_b.app_id, resolved_b.installation_id) == ("22", "333")
    # Without a repository, the stored installation.
    stored_a = workspace_credentials(WS_A, session_factory=factory)
    assert stored_a is not None and stored_a.installation_id == "111"


@pytest.mark.parametrize("installation", ["../../users", "12/34", "abc", ""])
def test_an_installation_that_is_not_a_number_is_refused(
    vault: SecretVault, factory: sessionmaker[Session], keys: dict[str, str], installation: str
) -> None:
    """It becomes a URL path segment: anything but digits means no credentials."""
    _store(vault, WS_A, "11", keys["a"], installation)
    assert workspace_credentials(WS_A, REPO, session_factory=factory) is None


def test_the_api_route_stores_what_the_worker_resolves(
    monkeypatch: pytest.MonkeyPatch, keys: dict[str, str], factory: sessionmaker[Session]
) -> None:
    """The route's stored shape is the one the worker reads (the two never drift)."""
    from scf_api.auth.service import AuthService, get_auth_service
    from scf_api.deps import Principal, get_current_principal
    from scf_api.main import create_app
    from scf_contracts import UserRole

    service = AuthService(secret_key=b"r" * 32)
    monkeypatch.setattr(finalizer_module, "_workspace_vault", lambda: service.vault)
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=uuid.uuid4(), workspace_id=WS_A, role=UserRole.ADMIN, auth_method="test"
    )
    with TestClient(app) as client:
        resp = client.put(
            "/integration/github/app-credentials",
            json={"app_id": "11", "installation_id": "111", "private_key": keys["a"]},
        )
    assert resp.status_code == 200, resp.text

    resolved = workspace_credentials(WS_A, session_factory=factory)
    assert resolved == GitHubAppCredentials(
        app_id="11", installation_id="111", private_key_pem=keys["a"]
    )
    assert workspace_credentials(WS_B, session_factory=factory) is None


# --------------------------------------------------------------------------- #
# Criteria 2 and 3: no process-wide lookup outside development                #
# --------------------------------------------------------------------------- #


def _no_process_wide_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(cls: type, env: Any = None) -> None:
        raise AssertionError("a process-wide credential lookup outside development")

    monkeypatch.setattr(GitHubAppCredentials, "from_env", classmethod(refuse))


def test_in_production_the_mirror_step_uses_only_the_workspace_credentials(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeGitHub,
    two_workspaces: None,
    mirrors: list[tuple[str, str]],
    environment: Any,
) -> None:
    environment("production")
    _no_process_wide_lookup(monkeypatch)

    _fetch(WS_A, uuid.uuid4())

    [mint] = github.mints
    assert (mint.app_id, mint.installation) == ("11", "222")
    assert mirrors == [(f"https://github.com/{REPO}.git", mint.token)]


def test_in_production_a_workspace_without_credentials_gets_no_mirror_fetch_despite_the_env(
    github: FakeGitHub, env_app: None, mirrors: list[tuple[str, str]], environment: Any
) -> None:
    environment("production")

    with pytest.raises(RepositoryUnavailable, match="no GitHub App credentials"):
        _fetch(WS_A, uuid.uuid4())

    assert github.mints == [] and mirrors == []
    assert workspace_credentials(WS_A, REPO) is None


def test_in_development_the_env_is_an_explicit_fallback(
    github: FakeGitHub, env_app: None, mirrors: list[tuple[str, str]], environment: Any
) -> None:
    environment("development")

    _fetch(WS_A, uuid.uuid4())

    [mint] = github.mints
    assert (mint.app_id, mint.installation) == ("99", "999")


def test_in_development_without_vault_or_env_delivery_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, github: FakeGitHub, repo: Path, environment: Any
) -> None:
    environment("development")
    for name in ("FORGE_GITHUB_APP_ID", "FORGE_GITHUB_INSTALLATION_ID"):
        monkeypatch.delenv(name, raising=False)

    result = _deliver(WS_A, uuid.uuid4(), repo)

    assert result.artifacts["pull_request"] == {"status": "not_configured"}
    assert github.mints == []


# --------------------------------------------------------------------------- #
# Criterion 4: one repository, the step's permissions, never reused           #
# --------------------------------------------------------------------------- #


def test_each_token_names_one_repository_and_the_steps_permissions_and_is_never_reused(
    github: FakeGitHub,
    two_workspaces: None,
    mirrors: list[tuple[str, str]],
    repo: Path,
    environment: Any,
) -> None:
    environment("development")
    _fetch(WS_A, uuid.uuid4())
    _fetch(WS_A, uuid.uuid4())  # a second run of the same workspace and repository
    _fetch(WS_B, uuid.uuid4())
    _deliver(WS_A, uuid.uuid4(), repo)

    bodies = [m.body for m in github.mints]
    assert bodies == [
        {"repositories": ["app"], "permissions": MIRROR_TOKEN_PERMISSIONS},
        {"repositories": ["app"], "permissions": MIRROR_TOKEN_PERMISSIONS},
        {"repositories": ["app"], "permissions": MIRROR_TOKEN_PERMISSIONS},
        {"repositories": ["app"], "permissions": RUN_TOKEN_PERMISSIONS},
    ]
    # Every run minted its own token and used that one only.
    assert [token for _, token in mirrors] == [m.token for m in github.mints[:3]]
    assert len({m.token for m in github.mints}) == 4


def test_the_merge_guard_reads_ci_with_the_workspaces_own_read_token(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeGitHub,
    two_workspaces: None,
    factory: sessionmaker[Session],
) -> None:
    from scf_integrations.github import GitHubClient
    from scf_worker import temporal_services

    transport = httpx.MockTransport(github.handler)
    monkeypatch.setattr(
        temporal_services, "GitHubClient", functools.partial(GitHubClient, transport=transport)
    )
    run = uuid.uuid4()

    runs = temporal_services.github_check_runs(
        REPO, "a1" * 20, workspace_id=WS_A, run_id=run, session_factory=factory
    )

    assert [r["name"] for r in runs] == ["tests"]
    [mint] = github.mints
    assert (mint.app_id, mint.installation) == ("11", "222")
    assert mint.body == {"repositories": ["app"], "permissions": {"checks": "read"}}
    assert github.ci_reads and all(mint.token in auth for auth in github.ci_reads)
    [issued] = _issued(factory)
    assert (issued.workspace_id, issued.details["run_id"]) == (WS_A, str(run))
    assert issued.details["permissions"] == {"checks": "read"}


# --------------------------------------------------------------------------- #
# Criterion 5: every issue audited; no secret anywhere                        #
# --------------------------------------------------------------------------- #


def test_each_token_issue_is_audited_without_any_secret(
    github: FakeGitHub,
    two_workspaces: None,
    mirrors: list[tuple[str, str]],
    repo: Path,
    factory: sessionmaker[Session],
    keys: dict[str, str],
    environment: Any,
) -> None:
    environment("development")
    fetch_run, deliver_run = uuid.uuid4(), uuid.uuid4()
    _fetch(WS_A, fetch_run)
    _deliver(WS_B, deliver_run, repo)

    rows = _issued(factory)
    assert [(r.workspace_id, r.details) for r in rows] == [
        (
            WS_A,
            {
                "run_id": str(fetch_run),
                "repository": REPO,
                "installation_id": "222",
                "permissions": MIRROR_TOKEN_PERMISSIONS,
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        ),
        (
            WS_B,
            {
                "run_id": str(deliver_run),
                "repository": REPO,
                "installation_id": "333",
                "permissions": RUN_TOKEN_PERMISSIONS,
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        ),
    ]
    stored = " ".join(
        json.dumps({c.name: getattr(r, c.name) for c in AuditLog.__table__.columns}, default=str)
        for r in rows
    )
    for secret in _secrets(github, keys):
        assert secret not in stored


def _secrets(github: FakeGitHub, keys: dict[str, str]) -> list[str]:
    """Each token, each App JWT and every base64 line of each private key.

    Lines, not the whole key: JSON and ``repr`` escape the newlines between them.
    """
    lines = [line for k in keys.values() for line in k.splitlines()[1:-1]]
    return [*(m.token for m in github.mints), *(m.app_jwt for m in github.mints), *lines]


def test_no_token_jwt_or_key_reaches_the_logs(
    caplog: pytest.LogCaptureFixture,
    github: FakeGitHub,
    two_workspaces: None,
    mirrors: list[tuple[str, str]],
    repo: Path,
    keys: dict[str, str],
    environment: Any,
) -> None:
    caplog.set_level(logging.DEBUG)
    environment("development")
    _fetch(WS_A, uuid.uuid4())
    _deliver(WS_B, uuid.uuid4(), repo)

    assert github.mints
    for secret in _secrets(github, keys):
        assert secret not in caplog.text


def test_a_failed_delivery_records_no_token_jwt_or_key_on_the_run(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeGitHub,
    two_workspaces: None,
    repo: Path,
    keys: dict[str, str],
    environment: Any,
) -> None:
    """The run's error and artifacts become its run events: nothing secret in them."""
    environment("development")

    def leaky_push(_path: Path, _branch: str, _url: str, token: str) -> None:
        mint = github.mints[-1]
        raise RuntimeError(f"push failed: {token} {mint.app_jwt} {keys['a']}")

    monkeypatch.setattr(
        agent_runner_module,
        "push_and_open_pr",
        functools.partial(
            push_and_open_pr, transport=httpx.MockTransport(github.handler), push=leaky_push
        ),
    )

    result = _deliver(WS_A, uuid.uuid4(), repo)

    assert result.status == RunStatus.FAILED
    recorded = f"{result.error} {result.artifacts}"
    for secret in _secrets(github, keys):
        assert secret not in recorded


def test_a_rejected_token_request_raises_no_secret(
    two_workspaces: None, factory: sessionmaker[Session], keys: dict[str, str]
) -> None:
    seen: list[str] = []

    def reject(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"].removeprefix("Bearer "))
        return httpx.Response(401, json={"message": "Bad credentials"})

    creds = workspace_credentials(WS_A, REPO, session_factory=factory)
    assert creds is not None
    with pytest.raises(GitHubError) as caught:
        finalizer_module.mint_token(
            creds,
            REPO,
            RUN_TOKEN_PERMISSIONS,
            workspace_id=WS_A,
            transport=httpx.MockTransport(reject),
            session_factory=factory,
        )

    message = f"{caught.value} {caught.value!r}"
    assert seen and seen[0] not in message
    assert keys["a"].splitlines()[1] not in message
    assert _issued(factory) == [], "a token that was never issued is not audited as issued"


def test_resolved_credentials_never_show_the_key_in_repr(
    two_workspaces: None, factory: sessionmaker[Session], keys: dict[str, str]
) -> None:
    creds = workspace_credentials(WS_A, REPO, session_factory=factory)
    assert creds is not None
    shown = f"{creds!r} {creds}"
    assert keys["a"].splitlines()[1] not in shown and "PRIVATE" not in shown


def test_unreadable_stored_credentials_are_none_and_raise_nothing(
    vault: SecretVault, factory: sessionmaker[Session], keys: dict[str, str]
) -> None:
    vault.put_secret(
        workspace_id=WS_A,
        name=WORKSPACE_SECRET_NAME,
        secret=f"not json {keys['a']}",
        kind=APIKeyKind.INTEGRATION_TOKEN,
    )
    assert workspace_credentials(WS_A, REPO, session_factory=factory) is None


# --------------------------------------------------------------------------- #
# Criterion 6 (worker side): minting never puts a token in the worker's env    #
# --------------------------------------------------------------------------- #


def test_minting_and_pushing_leave_no_token_in_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, github: FakeGitHub, two_workspaces: None, repo: Path
) -> None:
    """The sandbox's worktree tier starts from the worker's environment: the token and
    its auth header live only in the push subprocess's own environment."""
    import os

    seen_env: list[dict[str, str]] = []
    monkeypatch.setattr(
        finalizer_module.subprocess, "run", lambda *a, **kw: seen_env.append(kw["env"])
    )
    creds = workspace_credentials(WS_A, REPO)
    assert creds is not None
    push_and_open_pr(
        repo_path=repo,
        branch="scf/run-1",
        repo_full_name=REPO,
        base_branch="main",
        title="t",
        body="b",
        credentials=creds,
        workspace_id=WS_A,
        transport=httpx.MockTransport(github.handler),
    )

    [mint] = github.mints
    assert any("GIT_CONFIG_VALUE_0" in env for env in seen_env), "the push got its header"
    process = json.dumps(dict(os.environ))
    assert mint.token not in process and "GIT_CONFIG_VALUE_0" not in os.environ
