"""HARD-01 unit tests: Link-header pagination (offline)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from conftest import RequestRecorder, load_fixture, make_transport

from forge_contracts import PullRequest
from forge_integrations import GitHubClient
from forge_integrations.errors import GitHubError


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


def _endless_issues(rec: RequestRecorder) -> Callable[[httpx.Request], httpx.Response]:
    """Page ``n`` holds issue ``n`` and always links to page ``n + 1``."""

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        page = int(request.url.params.get("page", "1"))
        assert page <= 10, "pagination never stopped"
        next_url = f"https://api.github.com/repos/acme/widgets/issues?state=all&page={page + 1}"
        return httpx.Response(
            200, json=[{"number": page}], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    return handler


def test_iter_pages_follows_link_with_the_first_pages_params() -> None:
    rec = RequestRecorder()
    client = _client(_endless_issues(rec))

    pages = client.iter_pages(
        "https://github.com/acme/widgets", "issues", {"state": "all", "per_page": "100"}
    )
    assert [next(pages)["number"] for _ in range(3)] == [1, 2, 3]

    assert rec.requests[0].url.path == "/repos/acme/widgets/issues"
    assert dict(rec.requests[0].url.params) == {"state": "all", "per_page": "100"}
    assert rec.requests[1].url.params["page"] == "2"
    assert rec.requests[0].headers["Authorization"] == "Bearer ghs_x"


def test_iter_pages_stops_at_its_page_bound() -> None:
    rec = RequestRecorder()
    client = _client(_endless_issues(rec))
    seen: list[int] = []

    with pytest.raises(GitHubError, match="more than 3 pages"):
        pages = client.iter_pages("acme/widgets", "issues", max_pages=3)
        seen.extend(item["number"] for item in pages)

    assert seen == [1, 2, 3]
    assert len(rec.requests) == 3


def test_iter_pages_raises_the_clients_error_on_a_failed_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubError, match="Not Found"):
        list(_client(handler).iter_pages("acme/widgets", "issues"))


@pytest.mark.parametrize(
    "next_url",
    [
        "https://collector.example.net/steal?page=2",
        "http://api.github.com/repos/acme/widgets/issues?page=2",
    ],
)
def test_iter_pages_refuses_a_link_to_another_host_or_scheme(next_url: str) -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return httpx.Response(
            200, json=[{"number": 1}], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    seen: list[int] = []
    with pytest.raises(GitHubError, match=r"Link next leaves api\.github\.com"):
        seen.extend(
            item["number"] for item in _client(handler).iter_pages("acme/widgets", "issues")
        )

    assert seen == [1]
    assert [r.url.host for r in rec.requests] == ["api.github.com"]
