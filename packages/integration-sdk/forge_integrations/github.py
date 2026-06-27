"""GitHub App client (plan Task 1.13): repo sync, PRs, reviews, CI webhooks.

Built against ``httpx`` with an injectable transport so tests drive it from
recorded fixtures (``httpx.MockTransport``) and never touch the network. The
public method surface matches the frozen ``forge_contracts.GitHubClient``
Protocol (plus ``health`` from ``IntegrationClient``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import httpx

from forge_contracts import (
    CIStatus,
    HealthResult,
    PRState,
    PullRequest,
    PullRequestRequest,
    RepositoryConnection,
    RepoSyncResult,
    WebhookEvent,
)

from .errors import GitHubError
from .webhooks import parse_github_webhook

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubClient:
    """A fixture-backed GitHub App client.

    Parameters
    ----------
    token:
        Installation / app token. Sent as ``Authorization: Bearer <token>``.
    base_url:
        API root (override for GitHub Enterprise).
    transport:
        Optional ``httpx`` transport — tests pass an ``httpx.MockTransport``.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def owner_repo(full_name: str) -> str:
        """Normalise any repo reference to GitHub's ``owner/repo`` form."""
        s = full_name.strip()
        for prefix in ("https://", "http://", "ssh://", "git@"):
            if s.startswith(prefix):
                s = s[len(prefix) :]
        for host in ("github.com/", "github.com:"):
            if s.startswith(host):
                s = s[len(host) :]
        if s.endswith(".git"):
            s = s[:-4]
        return s.strip("/")

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # pragma: no cover - defensive
            raise GitHubError(str(exc)) from exc

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            message = ""
            try:
                message = str(resp.json().get("message", ""))
            except (ValueError, AttributeError):
                message = resp.text
            raise GitHubError(message or "request failed", status_code=resp.status_code)

    @staticmethod
    def _split_reviewers(reviewers: list[str]) -> tuple[list[str], list[str]]:
        """Partition reviewer slugs into (users, teams).

        Team reviewers are identified by a ``team-`` prefix or an ``org/team``
        slug; everything else is treated as a user login.
        """
        users: list[str] = []
        teams: list[str] = []
        for r in reviewers:
            if r.startswith("team-") or "/" in r:
                teams.append(r)
            else:
                users.append(r)
        return users, teams

    # ------------------------------------------------------------------ #
    # contract surface                                                   #
    # ------------------------------------------------------------------ #

    def open_pr(self, request: PullRequestRequest) -> PullRequest:
        repo = self.owner_repo(request.repo)
        payload: dict[str, Any] = {
            "title": request.title,
            "head": request.head,
            "base": request.base,
            "draft": request.draft,
        }
        if request.body is not None:
            payload["body"] = request.body
        resp = self._request("POST", f"/repos/{repo}/pulls", json=payload)
        self._raise_for_status(resp)
        pr = self._parse_pr(request.repo, resp.json())

        if request.reviewers:
            self.request_reviews(pr, request.reviewers)
        if request.labels and pr.number is not None:
            self._add_labels(repo, pr.number, request.labels)
        return pr

    def request_reviews(self, pr: PullRequest, reviewers: list[str]) -> None:
        repo = self.owner_repo(pr.repo)
        users, teams = self._split_reviewers(reviewers)
        resp = self._request(
            "POST",
            f"/repos/{repo}/pulls/{pr.number}/requested_reviewers",
            json={"reviewers": users, "team_reviewers": teams},
        )
        self._raise_for_status(resp)

    def sync_repo(self, connection: RepositoryConnection) -> RepoSyncResult:
        repo = self.owner_repo(connection.full_name)
        branch = connection.default_branch or "main"
        head_resp = self._request("GET", f"/repos/{repo}/commits/{branch}")
        self._raise_for_status(head_resp)
        head_sha = str(head_resp.json().get("sha") or "")

        last_sha = connection.metadata.get("last_synced_sha")
        if last_sha and last_sha == head_sha:
            # Nothing new since the last sync.
            return RepoSyncResult(repo=connection.full_name, head_sha=head_sha)

        if last_sha:
            return self._incremental(connection.full_name, repo, last_sha, head_sha)
        return self._full(connection.full_name, repo, head_sha)

    def parse_webhook(self, event: WebhookEvent) -> CIStatus:
        return parse_github_webhook(event)

    def health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            resp = self._request("GET", "/rate_limit")
        except GitHubError as exc:
            return HealthResult(
                healthy=False, status="error", message=str(exc), checked_at=_now()
            )
        latency_ms = int((time.perf_counter() - start) * 1000)
        healthy = resp.status_code < 400
        return HealthResult(
            healthy=healthy,
            status="ok" if healthy else "error",
            latency_ms=latency_ms,
            message=None if healthy else f"status {resp.status_code}",
            checked_at=_now(),
        )

    # ------------------------------------------------------------------ #
    # internals                                                          #
    # ------------------------------------------------------------------ #

    def _full(self, display_repo: str, repo: str, head_sha: str) -> RepoSyncResult:
        resp = self._request(
            "GET", f"/repos/{repo}/git/trees/{head_sha}", params={"recursive": "1"}
        )
        self._raise_for_status(resp)
        tree = resp.json().get("tree") or []
        blobs = sum(1 for e in tree if e.get("type") == "blob")
        return RepoSyncResult(
            repo=display_repo, head_sha=head_sha, files_changed=blobs, indexed=blobs
        )

    def _incremental(
        self, display_repo: str, repo: str, base_sha: str, head_sha: str
    ) -> RepoSyncResult:
        resp = self._request("GET", f"/repos/{repo}/compare/{base_sha}...{head_sha}")
        self._raise_for_status(resp)
        files = resp.json().get("files") or []
        deleted = sum(1 for f in files if f.get("status") == "removed")
        indexed = len(files) - deleted
        return RepoSyncResult(
            repo=display_repo,
            head_sha=head_sha,
            files_changed=len(files),
            indexed=indexed,
            deleted=deleted,
        )

    def _add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        resp = self._request(
            "POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels}
        )
        self._raise_for_status(resp)

    @staticmethod
    def _parse_pr(display_repo: str, data: dict[str, Any]) -> PullRequest:
        head = data.get("head") or {}
        base = data.get("base") or {}
        if data.get("merged"):
            state = PRState.MERGED
        elif (data.get("state") or "open").lower() == "closed":
            state = PRState.CLOSED
        elif data.get("draft"):
            state = PRState.DRAFT
        else:
            state = PRState.OPEN
        return PullRequest(
            repo=display_repo,
            number=data.get("number"),
            url=data.get("html_url"),
            state=state,
            title=data.get("title"),
            head=head.get("ref"),
            base=base.get("ref") or "main",
            head_sha=head.get("sha"),
        )


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["GitHubClient"]
