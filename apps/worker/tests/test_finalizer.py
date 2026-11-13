"""Run finalizer (spec G §4): scoped token, no token in argv, honest outcomes."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from forge_contracts import AgentObjective, AgentRunResult, PullRequest, RunStatus
from forge_worker import agent_runner as agent_runner_module
from forge_worker import finalizer as finalizer_module
from forge_worker.finalizer import (
    RUN_TOKEN_PERMISSIONS,
    GitHubAppCredentials,
    git_push,
    push_and_open_pr,
)

TOKEN = "ghs_" + "t" * 36


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "clone"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "checkout", "-q", "-b", "forge/run-1")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    _git(root, "checkout", "-q", "main")
    return root


def test_push_and_open_pr_mints_a_one_repo_token_and_opens_the_pr(repo: Path, pem: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if request.url.path == "/repos/acme/widgets/pulls":
            return httpx.Response(
                201,
                json={
                    "number": 7,
                    "html_url": "https://github.com/acme/widgets/pull/7",
                    "state": "open",
                    "head": {"ref": "forge/run-1", "sha": "abc"},
                    "base": {"ref": "main"},
                    "title": "t",
                },
            )
        return httpx.Response(404)

    pushed: list[tuple[Path, str, str, str]] = []
    pr = push_and_open_pr(
        repo_path=repo,
        branch="forge/run-1",
        repo_full_name="acme/widgets",
        base_branch="main",
        title="Add b",
        body="body",
        credentials=GitHubAppCredentials(app_id="1", installation_id="2", private_key_pem=pem),
        transport=httpx.MockTransport(handler),
        push=lambda *args: pushed.append(args),
    )

    mint = json.loads(seen[0].content)
    assert mint == {"repositories": ["widgets"], "permissions": RUN_TOKEN_PERMISSIONS}
    assert pushed == [(repo, "forge/run-1", "https://github.com/acme/widgets.git", TOKEN)]
    opened = json.loads(seen[1].content)
    assert (opened["head"], opened["base"]) == ("forge/run-1", "main")
    assert seen[1].headers["authorization"] == f"Bearer {TOKEN}"
    assert pr.number == 7


def test_git_push_keeps_the_token_out_of_argv(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        finalizer_module.subprocess,
        "run",
        lambda argv, **kw: calls.append((argv, kw["env"])),
    )
    git_push(repo, "forge/run-1", "https://github.com/acme/widgets.git", TOKEN)
    ((argv, env),) = calls
    assert TOKEN not in " ".join(argv)
    assert "core.hooksPath=/dev/null" in argv and "--no-verify" in argv
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_push_really_pushes_the_branch(repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git_push(repo, "forge/run-1", str(remote), TOKEN)
    assert _git(remote, "rev-parse", "forge/run-1") == _git(repo, "rev-parse", "forge/run-1")


def test_credentials_repr_never_shows_the_key(pem: str) -> None:
    creds = GitHubAppCredentials(app_id="1", installation_id="2", private_key_pem=pem)
    assert "PRIVATE" not in repr(creds)


def test_credentials_from_env_require_all_three(tmp_path: Path, pem: str) -> None:
    key = tmp_path / "app.pem"
    key.write_text(pem)
    env = {
        "FORGE_GITHUB_APP_ID": "1",
        "FORGE_GITHUB_INSTALLATION_ID": "2",
        "FORGE_GITHUB_APP_PRIVATE_KEY_PATH": str(key),
    }
    assert GitHubAppCredentials.from_env(env) is not None
    assert GitHubAppCredentials.from_env({**env, "FORGE_GITHUB_APP_ID": ""}) is None
    missing_key = {**env, "FORGE_GITHUB_APP_PRIVATE_KEY_PATH": str(tmp_path / "x")}
    assert GitHubAppCredentials.from_env(missing_key) is None


def _objective(repo: Path) -> AgentObjective:
    return AgentObjective(
        objective="Add b",
        key="TASK-9",
        context={"repo_path": str(repo), "repo_full_name": "acme/widgets"},
    )


def _succeeded() -> AgentRunResult:
    return AgentRunResult(status=RunStatus.SUCCEEDED, output="done")


def test_finalize_without_credentials_reports_not_configured(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(GitHubAppCredentials, "from_env", classmethod(lambda cls: None))
    result = _succeeded()
    agent_runner_module._finalize(
        _objective(repo), result, branch="forge/run-1", run_id=uuid.uuid4()
    )
    assert result.artifacts["pull_request"] == {"status": "not_configured"}
    assert result.status == RunStatus.SUCCEEDED


def test_finalize_without_commits_opens_nothing(repo: Path) -> None:
    _git(repo, "branch", "forge/empty", "main")
    result = _succeeded()
    agent_runner_module._finalize(
        _objective(repo), result, branch="forge/empty", run_id=uuid.uuid4()
    )
    assert result.artifacts["pull_request"] == {"status": "no_changes"}


def test_finalize_opens_the_pr(repo: Path, monkeypatch: pytest.MonkeyPatch, pem: str) -> None:
    creds = GitHubAppCredentials(app_id="1", installation_id="2", private_key_pem=pem)
    monkeypatch.setattr(GitHubAppCredentials, "from_env", classmethod(lambda cls: creds))
    captured: dict[str, Any] = {}

    def fake_open(**kwargs: Any) -> PullRequest:
        captured.update(kwargs)
        return PullRequest(
            repo="acme/widgets", number=7, url="https://github.com/acme/widgets/pull/7"
        )

    monkeypatch.setattr(agent_runner_module, "push_and_open_pr", fake_open)
    run_id = uuid.uuid4()
    result = _succeeded()
    agent_runner_module._finalize(_objective(repo), result, branch="forge/run-1", run_id=run_id)
    assert result.artifacts["pull_request"] == {
        "status": "opened",
        "number": 7,
        "url": "https://github.com/acme/widgets/pull/7",
    }
    assert captured["title"] == "TASK-9: Add b"
    assert str(run_id) in captured["body"] and "human approval" in captured["body"]


def test_finalize_failure_fails_the_run_and_redacts(
    repo: Path, monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:
    creds = GitHubAppCredentials(app_id="1", installation_id="2", private_key_pem=pem)
    monkeypatch.setattr(GitHubAppCredentials, "from_env", classmethod(lambda cls: creds))

    def boom(**_: Any) -> PullRequest:
        raise RuntimeError(f"push rejected for {TOKEN}")

    monkeypatch.setattr(agent_runner_module, "push_and_open_pr", boom)
    result = _succeeded()
    agent_runner_module._finalize(
        _objective(repo), result, branch="forge/run-1", run_id=uuid.uuid4()
    )
    assert result.status == RunStatus.FAILED
    assert result.artifacts["pull_request"]["status"] == "failed"
    assert TOKEN not in (result.error or "") and TOKEN not in str(result.artifacts)
