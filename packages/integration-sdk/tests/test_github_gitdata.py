"""HARD-01 unit tests: Git Data API branch push primitives (offline)."""

from __future__ import annotations

import json

import httpx
from conftest import RequestRecorder, load_fixture, make_transport

from forge_contracts import PRState, PullRequest
from forge_integrations import GitHubClient


def _client(handler) -> GitHubClient:
    return GitHubClient(token="ghs_x", transport=make_transport(handler))


def test_create_branch_resolves_base_sha() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        if request.method == "GET" and "/git/ref/heads/main" in request.url.path:
            return httpx.Response(200, json=load_fixture("git_ref"))
        if request.method == "POST" and request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    client = _client(handler)
    base_sha = client.create_branch("org/api", new_branch="forge/smoke-1", from_ref="main")
    assert base_sha == "1111111111111111111111111111111111111111"

    create = rec.by_path("/git/refs")[-1]
    body = json.loads(create.content)
    assert body == {
        "ref": "refs/heads/forge/smoke-1",
        "sha": "1111111111111111111111111111111111111111",
    }


def test_push_files_blob_tree_commit_ref_sequence() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        path = request.url.path
        method = request.method
        if method == "GET" and path.endswith("/git/ref/heads/forge/smoke-1"):
            return httpx.Response(200, json=load_fixture("git_ref"))
        if method == "GET" and "/git/commits/" in path:
            return httpx.Response(200, json=load_fixture("git_commit"))
        if method == "POST" and path.endswith("/git/blobs"):
            return httpx.Response(201, json=load_fixture("git_blob"))
        if method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json=load_fixture("git_tree_created"))
        if method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json=load_fixture("git_commit_created"))
        if method == "PATCH" and "/git/refs/heads/forge/smoke-1" in path:
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})

    client = _client(handler)
    sha = client.push_files(
        "org/api",
        branch="forge/smoke-1",
        files={"docs/NOTE.md": "hello\n"},
        message="chore: smoke",
        base_ref="main",
    )
    assert sha == "5555555555555555555555555555555555555555"

    # The Git Data API sequence: blob -> tree -> commit -> update-ref.
    ordered = [f"{r.method} {r.url.path.split('/repos/org/api')[-1]}" for r in rec.requests]
    blob_i = ordered.index("POST /git/blobs")
    tree_i = ordered.index("POST /git/trees")
    commit_i = ordered.index("POST /git/commits")
    patch_i = next(i for i, s in enumerate(ordered) if s.startswith("PATCH /git/refs/heads"))
    assert blob_i < tree_i < commit_i < patch_i

    # Blob payload carries the file content + encoding.
    blob_body = json.loads(rec.by_path("/git/blobs")[0].content)
    assert blob_body == {"content": "hello\n", "encoding": "utf-8"}

    # Tree references the base tree from the parent commit + the new blob.
    tree_body = json.loads(rec.by_path("/git/trees")[0].content)
    assert tree_body["base_tree"] == "2222222222222222222222222222222222222222"
    assert tree_body["tree"] == [
        {
            "path": "docs/NOTE.md",
            "mode": "100644",
            "type": "blob",
            "sha": "3333333333333333333333333333333333333333",
        }
    ]

    # Commit references the new tree + the parent commit. (Select the POST that
    # *creates* the commit — ``by_path`` would also match the GET base-commit.)
    create_commit = next(
        r for r in rec.requests if r.method == "POST" and r.url.path.endswith("/git/commits")
    )
    commit_body = json.loads(create_commit.content)
    assert commit_body["tree"] == "4444444444444444444444444444444444444444"
    assert commit_body["parents"] == ["1111111111111111111111111111111111111111"]
    assert commit_body["message"] == "chore: smoke"


def test_close_pr_sets_state_closed() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return httpx.Response(
            200,
            json={
                "number": 42,
                "state": "closed",
                "html_url": "https://github.com/org/api/pull/42",
                "head": {"ref": "forge/smoke-1", "sha": "deadbeef"},
                "base": {"ref": "main"},
            },
        )

    client = _client(handler)
    result = client.close_pr(PullRequest(repo="org/api", number=42))
    assert result.state is PRState.CLOSED
    body = json.loads(rec.last.content)
    assert body == {"state": "closed"}
    assert rec.last.method == "PATCH"
    assert rec.last.url.path == "/repos/org/api/pulls/42"


def test_delete_branch_is_idempotent_on_404() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        # A branch already gone returns 422/404 — cleanup must not raise.
        return httpx.Response(422, json={"message": "Reference does not exist"})

    client = _client(handler)
    # Should not raise despite the 422.
    client.delete_branch("org/api", "forge/smoke-1")
    assert rec.last.method == "DELETE"
    assert rec.last.url.path == "/repos/org/api/git/refs/heads/forge/smoke-1"
