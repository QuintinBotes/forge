"""Tests for the GitHub App client (plan Task 1.13).

Every request is served by an ``httpx.MockTransport`` from recorded fixtures —
no live GitHub calls. Asserts the *outbound payload* is correct (the plan's
"PR-open builds correct payload (fixture)") and that responses parse into the
frozen ``forge_contracts`` DTOs.
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import RequestRecorder, load_fixture, make_transport

from forge_contracts import (
    PRState,
    PullRequest,
    PullRequestRequest,
    RepositoryConnection,
    RepoSyncResult,
)
from forge_integrations import GitHubClient, GitHubError


def _client(handler, *, token: str = "ghs_testtoken") -> GitHubClient:
    return GitHubClient(token=token, transport=make_transport(handler))


# --------------------------------------------------------------------------- #
# open_pr                                                                      #
# --------------------------------------------------------------------------- #


def test_open_pr_builds_correct_payload_and_parses_response() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        if request.url.path.endswith("/pulls"):
            return httpx.Response(201, json=load_fixture("pr_created"))
        return httpx.Response(404, json={"message": "not found"})

    client = _client(handler)
    req = PullRequestRequest(
        repo="github.com/org/api",
        title="Add customer search endpoint",
        body="Implements SPEC-17",
        head="forge/TASK-123",
        base="main",
        draft=False,
    )
    pr = client.open_pr(req)

    # Outbound payload is the GitHub create-PR shape.
    sent = json.loads(rec.last.content)
    assert sent == {
        "title": "Add customer search endpoint",
        "head": "forge/TASK-123",
        "base": "main",
        "body": "Implements SPEC-17",
        "draft": False,
    }
    # Hits the owner/repo-normalised endpoint with auth + version headers.
    assert rec.last.url.path == "/repos/org/api/pulls"
    assert rec.last.method == "POST"
    assert rec.last.headers["authorization"] == "Bearer ghs_testtoken"
    assert rec.last.headers["x-github-api-version"]

    # Response parsed into the frozen DTO.
    assert isinstance(pr, PullRequest)
    assert pr.number == 42
    assert pr.state is PRState.OPEN
    assert pr.url == "https://github.com/org/api/pull/42"
    assert pr.head == "forge/TASK-123"
    assert pr.head_sha == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert pr.repo == "github.com/org/api"


def test_open_pr_requests_reviewers_when_provided() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        if request.url.path.endswith("/pulls"):
            return httpx.Response(201, json=load_fixture("pr_created"))
        if request.url.path.endswith("/requested_reviewers"):
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    client = _client(handler)
    req = PullRequestRequest(
        repo="org/api",
        title="t",
        head="feature",
        base="main",
        reviewers=["alice", "team-backend"],
    )
    client.open_pr(req)

    review_calls = rec.by_path("/requested_reviewers")
    assert len(review_calls) == 1
    body = json.loads(review_calls[0].content)
    # team reviewers (prefixed names) are routed to team_reviewers.
    assert body["reviewers"] == ["alice"]
    assert body["team_reviewers"] == ["team-backend"]


def test_open_pr_draft_payload() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return httpx.Response(201, json=load_fixture("pr_created"))

    client = _client(handler)
    client.open_pr(PullRequestRequest(repo="org/api", title="t", head="h", draft=True))
    assert json.loads(rec.last.content)["draft"] is True


def test_open_pr_raises_on_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Validation Failed"})

    client = _client(handler)
    with pytest.raises(GitHubError) as exc:
        client.open_pr(PullRequestRequest(repo="org/api", title="t", head="h"))
    assert exc.value.status_code == 422
    assert "422" in str(exc.value) or "Validation" in str(exc.value)


# --------------------------------------------------------------------------- #
# request_reviews                                                             #
# --------------------------------------------------------------------------- #


def test_request_reviews_posts_reviewers() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return httpx.Response(201, json={})

    client = _client(handler)
    pr = PullRequest(repo="github.com/org/api", number=42)
    result = client.request_reviews(pr, ["bob"])
    assert result is None
    assert rec.last.url.path == "/repos/org/api/pulls/42/requested_reviewers"
    assert json.loads(rec.last.content)["reviewers"] == ["bob"]


# --------------------------------------------------------------------------- #
# sync_repo                                                                    #
# --------------------------------------------------------------------------- #


def test_sync_repo_full_counts_tree_blobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/org/api/commits/main":
            return httpx.Response(200, json=load_fixture("commit_head"))
        if path.startswith("/repos/org/api/git/trees/"):
            return httpx.Response(200, json=load_fixture("git_tree"))
        return httpx.Response(404, json={})

    client = _client(handler)
    conn = RepositoryConnection(full_name="github.com/org/api", default_branch="main")
    result = client.sync_repo(conn)
    assert isinstance(result, RepoSyncResult)
    assert result.repo == "github.com/org/api"
    assert result.head_sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    # 3 blobs in the recorded tree (the two tree entries are excluded).
    assert result.files_changed == 3
    assert result.indexed == 3
    assert result.deleted == 0


def test_sync_repo_incremental_uses_compare() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/org/api/commits/main":
            return httpx.Response(200, json=load_fixture("commit_head"))
        if path.startswith("/repos/org/api/compare/"):
            return httpx.Response(200, json=load_fixture("compare"))
        return httpx.Response(404, json={})

    client = _client(handler)
    conn = RepositoryConnection(
        full_name="org/api",
        default_branch="main",
        metadata={"last_synced_sha": "0000000000000000000000000000000000000000"},
    )
    result = client.sync_repo(conn)
    assert result.head_sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert result.files_changed == 3
    # one file removed in the recorded compare fixture.
    assert result.deleted == 1


def test_sync_repo_incremental_noop_when_head_unchanged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/org/api/commits/main":
            return httpx.Response(200, json=load_fixture("commit_head"))
        raise AssertionError(f"unexpected call: {request.url.path}")

    client = _client(handler)
    conn = RepositoryConnection(
        full_name="org/api",
        metadata={"last_synced_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
    )
    result = client.sync_repo(conn)
    assert result.files_changed == 0
    assert result.head_sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# --------------------------------------------------------------------------- #
# health + repo normalisation                                                 #
# --------------------------------------------------------------------------- #


def test_health_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resources": {}})

    client = _client(handler)
    health = client.health()
    assert health.healthy is True
    assert health.latency_ms is not None


def test_health_unhealthy_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = _client(handler)
    health = client.health()
    assert health.healthy is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("github.com/org/api", "org/api"),
        ("https://github.com/org/api", "org/api"),
        ("https://github.com/org/api.git", "org/api"),
        ("git@github.com:org/api.git", "org/api"),
        ("org/api", "org/api"),
        ("org/api/", "org/api"),
    ],
)
def test_owner_repo_normalisation(raw: str, expected: str) -> None:
    assert GitHubClient.owner_repo(raw) == expected


def test_context_manager_closes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _client(handler) as client:
        assert client.health().healthy is True
