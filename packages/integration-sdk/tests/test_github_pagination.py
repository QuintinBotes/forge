"""HARD-01 unit tests: Link-header pagination (offline)."""

from __future__ import annotations

import httpx
from conftest import RequestRecorder, load_fixture, make_transport

from forge_contracts import PullRequest
from forge_integrations import GitHubClient


def _client(handler) -> GitHubClient:
    return GitHubClient(token="ghs_x", transport=make_transport(handler))


def test_paginate_follows_link_next() -> None:
    rec = RequestRecorder()
    base = "https://api.github.com"

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=load_fixture("pulls_page2"))
        # First page advertises the next page via Link.
        next_url = f"{base}/repos/org/api/pulls/7/reviews?page=2"
        return httpx.Response(
            200,
            json=load_fixture("pulls_page1"),
            headers={"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'},
        )

    client = _client(handler)
    pr = PullRequest(repo="org/api", number=7)
    reviews = client.list_reviews(pr)

    # Two pages concatenated: 2 + 1 = 3 reviews.
    assert [r.id for r in reviews] == [1, 2, 3]
    assert [r.state for r in reviews] == ["COMMENTED", "APPROVED", "CHANGES_REQUESTED"]
    assert len(rec.requests) == 2


def test_single_page_no_extra_request() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        # No Link header -> exactly one page.
        return httpx.Response(200, json=load_fixture("pulls_page1"))

    client = _client(handler)
    pr = PullRequest(repo="org/api", number=7)
    comments = client.list_review_comments(pr)
    assert len(comments) == 2
    assert len(rec.requests) == 1


def test_paginate_envelope_items_key() -> None:
    """``items_key`` extracts a list from an envelope object (tree/compare)."""
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        if request.url.path == "/repos/org/api/commits/main":
            return httpx.Response(200, json={"sha": "headsha"})
        if request.url.path.startswith("/repos/org/api/git/trees/"):
            return httpx.Response(200, json=load_fixture("git_tree"))
        return httpx.Response(404, json={})

    from forge_contracts import RepositoryConnection

    client = _client(handler)
    result = client.sync_repo(RepositoryConnection(full_name="org/api", default_branch="main"))
    # git_tree fixture has 3 blobs (excluding the 2 tree entries).
    assert result.files_changed == 3
