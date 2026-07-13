"""GitHub App client (Task 1.13 + HARD-01): repo sync, PRs, reviews, CI webhooks.

Built against ``httpx`` with an injectable transport so tests drive it from
recorded fixtures (``httpx.MockTransport``) and never touch the network. The
public method surface matches the frozen ``forge_contracts.GitHubClient``
Protocol (plus ``health`` from ``IntegrationClient``).

HARD-01 hardens the client for the *real* api.github.com without changing that
frozen surface:

- :meth:`GitHubClient.from_app` wires a :class:`InstallationTokenProvider` so
  every data-plane call carries a freshly-minted (cached) installation token
  instead of a static ``token``.
- ``_request`` retries with exponential backoff + jitter on 5xx and GitHub
  *secondary* rate limits (``403``/``429`` with ``Retry-After`` or
  ``X-RateLimit-Remaining: 0``), retries once on a ``401`` after rotating the
  token, and emits a redaction-safe :class:`GitHubAuditEvent` per outcome.
- ``_paginate`` follows the ``Link: rel="next"`` header so large repos/PRs are
  never silently truncated.
- Git Data API primitives (:meth:`create_branch`, :meth:`push_files`) push a
  branch without a local clone, and :meth:`list_reviews` /
  :meth:`list_review_comments` / :meth:`close_pr` / :meth:`delete_branch` round
  out the live PR lifecycle used by the creds-gated integration lane.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
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

from .audit import AuditSink, GitHubAuditEvent
from .errors import GitHubError
from .github_auth import (
    API_VERSION,
    DEFAULT_BASE_URL,
    InstallationTokenProvider,
)
from .webhooks import parse_github_webhook

__all__ = [
    "GitHubClient",
    "RetryPolicy",
    "Review",
    "ReviewComment",
]


@dataclass(frozen=True)
class RetryPolicy:
    """Retry/backoff configuration for the hardened request path.

    ``retry_statuses`` are the transient server errors retried with exponential
    backoff; ``403``/``429`` secondary rate limits are handled separately (they
    honour ``Retry-After`` / ``X-RateLimit-Reset``). ``max_attempts == 1`` (the
    default for the legacy static-token client) disables retrying entirely.
    """

    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0
    jitter: bool = True
    retry_statuses: frozenset[int] = field(default_factory=lambda: frozenset({500, 502, 503, 504}))


#: Legacy/static-token clients do not retry (behaviour identical to pre-HARD-01).
_NO_RETRY = RetryPolicy(max_attempts=1)


@dataclass(frozen=True)
class ReviewComment:
    """A single PR review comment (normalised; not a frozen contract)."""

    id: int | None = None
    body: str | None = None
    path: str | None = None
    user: str | None = None


@dataclass(frozen=True)
class Review:
    """A single PR review (normalised; not a frozen contract)."""

    id: int | None = None
    state: str | None = None
    body: str | None = None
    user: str | None = None


_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _parse_next_link(link_header: str | None) -> str | None:
    """Return the ``rel="next"`` URL from a GitHub ``Link`` header, if any."""
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    return match.group(1) if match else None


def _hash_body(body: Any) -> str | None:
    """SHA-256 of a request body for audit (never the body itself)."""
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    else:
        raw = json.dumps(body, sort_keys=True, default=str, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


class GitHubClient:
    """A GitHub App client (fixture-backed in tests, live via :meth:`from_app`).

    Parameters
    ----------
    token:
        Static installation / app token (legacy path). Sent as
        ``Authorization: Bearer <token>``. Omit when using :meth:`from_app`.
    base_url:
        API root (override for GitHub Enterprise).
    transport:
        Optional ``httpx`` transport — tests pass an ``httpx.MockTransport``.
    timeout:
        Per-request timeout in seconds.
    retry:
        Retry/backoff policy. Defaults to no-retry for the static client;
        :meth:`from_app` supplies the resilient default.
    audit_sink / token_provider / invalidate / sleep / wall_clock / rng:
        Wired by :meth:`from_app`; not part of the public construction surface.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        retry: RetryPolicy | None = None,
        audit_sink: AuditSink | None = None,
        token_provider: Callable[[], str] | None = None,
        invalidate: Callable[[], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        wall_clock: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._token = token
        self._token_provider = token_provider
        self._invalidate = invalidate
        # Set by ``from_app`` — the owning provider, kept only so ``close`` can
        # release its client. ``None`` on the static-token path.
        self._token_provider_obj: InstallationTokenProvider | None = None
        self._retry = retry or _NO_RETRY
        self._audit_sink = audit_sink
        self._sleep = sleep or time.sleep
        self._wall_clock = wall_clock or time.time
        self._rng = rng or random.random
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        # Static token is attached at the client level; the App path injects a
        # fresh bearer per request instead (so it is never a durable header).
        if token and token_provider is None:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ #
    # construction                                                       #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_app(
        cls,
        *,
        app_id: str,
        private_key_pem: str,
        installation_id: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        retry: RetryPolicy | None = None,
        audit_sink: AuditSink | None = None,
        timeout: float = 10.0,
        sleep: Callable[[float], None] | None = None,
        wall_clock: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> GitHubClient:
        """Build a client that authenticates as a GitHub App installation.

        Wires an :class:`InstallationTokenProvider` (JWT mint + installation
        token exchange + caching) and the resilient default :class:`RetryPolicy`.
        The private key is used only to sign the App JWT in-memory; it is never
        stored on the returned client (AC7).
        """
        provider = InstallationTokenProvider(
            app_id=app_id,
            private_key_pem=private_key_pem,
            installation_id=installation_id,
            base_url=base_url,
            transport=transport,
            clock=wall_clock or time.time,
            timeout=timeout,
        )
        client = cls(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            retry=retry or RetryPolicy(),
            audit_sink=audit_sink,
            token_provider=provider.token,
            invalidate=provider.invalidate,
            sleep=sleep,
            wall_clock=wall_clock,
            rng=rng,
        )
        client._token_provider_obj = provider
        return client

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._client.close()
        provider = getattr(self, "_token_provider_obj", None)
        if provider is not None:
            provider.close()

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

    def _backoff(self, attempt: int, policy: RetryPolicy) -> float:
        """Exponential backoff (optionally jittered) for the ``attempt``-th try."""
        delay = min(policy.base_delay_s * (2 ** (attempt - 1)), policy.max_delay_s)
        if policy.jitter:
            # Full-jitter factor in [0.5, 1.5) keeps the mean at ``delay`` while
            # spreading concurrent clients.
            delay *= 0.5 + self._rng()
        return min(delay, policy.max_delay_s)

    def _retry_delay(self, resp: httpx.Response, attempt: int, policy: RetryPolicy) -> float | None:
        """Return a sleep duration if ``resp`` is retryable, else ``None``.

        Retries transient 5xx with backoff, and ``403``/``429`` *secondary* rate
        limits by honouring ``Retry-After`` (relative seconds) or
        ``X-RateLimit-Remaining: 0`` + ``X-RateLimit-Reset`` (absolute epoch).
        A plain ``403`` (permission denied — no rate-limit signal) is *not*
        retried.
        """
        code = resp.status_code
        if code in policy.retry_statuses:
            return self._backoff(attempt, policy)
        if code in (403, 429):
            retry_after = resp.headers.get("retry-after")
            if retry_after is not None:
                try:
                    return min(float(retry_after), policy.max_delay_s)
                except ValueError:
                    return self._backoff(attempt, policy)
            remaining = resp.headers.get("x-ratelimit-remaining")
            reset = resp.headers.get("x-ratelimit-reset")
            if remaining == "0" and reset is not None:
                try:
                    wait = float(reset) - self._wall_clock()
                except ValueError:
                    wait = self._backoff(attempt, policy)
                return max(0.0, min(wait, policy.max_delay_s))
        return None

    def _emit_audit(
        self,
        *,
        action: str | None,
        repo: str | None,
        status: str,
        status_code: int | None,
        started: float,
        payload_hash: str | None,
        detail: str | None,
    ) -> None:
        if self._audit_sink is None or action is None:
            return
        latency_ms = int((time.perf_counter() - started) * 1000)
        event = GitHubAuditEvent(
            action=action,
            repo=repo,
            status=status,
            status_code=status_code,
            latency_ms=latency_ms,
            payload_hash=payload_hash,
            detail=detail,
        )
        # Audit must never break the call path.
        with contextlib.suppress(Exception):
            self._audit_sink(event)

    def _request(
        self,
        method: str,
        path: str,
        *,
        action: str | None = None,
        repo: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue a request with token injection, retry/backoff, and audit.

        Legacy static-token clients (``_NO_RETRY``, no ``token_provider``) behave
        exactly as before: a single attempt, no reauth loop, no audit sink.
        """
        policy = self._retry
        extra_headers = kwargs.pop("headers", None)
        payload_hash = _hash_body(kwargs.get("json") or kwargs.get("content"))
        started = time.perf_counter()
        attempt = 0
        reauthed = False
        while True:
            attempt += 1
            headers = dict(extra_headers) if extra_headers else {}
            if self._token_provider is not None:
                headers["Authorization"] = f"Bearer {self._token_provider()}"
            try:
                resp = self._client.request(method, path, headers=headers or None, **kwargs)
            except httpx.HTTPError as exc:
                if attempt < policy.max_attempts:
                    self._sleep(self._backoff(attempt, policy))
                    continue
                self._emit_audit(
                    action=action,
                    repo=repo,
                    status="error",
                    status_code=None,
                    started=started,
                    payload_hash=payload_hash,
                    detail=str(exc),
                )
                raise GitHubError(str(exc)) from exc

            # A single 401 after minting means the token was rotated/revoked;
            # invalidate the cache and retry once with a fresh token.
            if (
                resp.status_code == 401
                and self._token_provider is not None
                and self._invalidate is not None
                and not reauthed
            ):
                reauthed = True
                self._invalidate()
                continue

            delay = self._retry_delay(resp, attempt, policy)
            if delay is not None and attempt < policy.max_attempts:
                self._sleep(delay)
                continue

            status = "ok" if resp.status_code < 400 else "error"
            self._emit_audit(
                action=action,
                repo=repo,
                status=status,
                status_code=resp.status_code,
                started=started,
                payload_hash=payload_hash,
                detail=None,
            )
            return resp

    def _paginate(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        items_key: str | None = None,
        action: str | None = None,
        repo: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across every page, following ``Link: rel="next"``.

        A single-page response makes exactly one request. ``items_key`` extracts
        a list from an envelope object; when ``None`` the response body is
        assumed to already be a JSON array.
        """
        next_url: str | None = path
        next_params = params
        while next_url is not None:
            resp = self._request(method, next_url, params=next_params, action=action, repo=repo)
            self._raise_for_status(resp)
            data = resp.json()
            if items_key is not None and isinstance(data, dict):
                items = data.get(items_key) or []
            else:
                items = data
            if isinstance(items, list):
                yield from items
            elif items:
                yield items
            # The next URL already encodes its own query string.
            next_url = _parse_next_link(resp.headers.get("link"))
            next_params = None

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
        resp = self._request(
            "POST", f"/repos/{repo}/pulls", json=payload, action="open_pr", repo=repo
        )
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
            action="request_reviews",
            repo=repo,
        )
        self._raise_for_status(resp)

    def sync_repo(self, connection: RepositoryConnection) -> RepoSyncResult:
        repo = self.owner_repo(connection.full_name)
        branch = connection.default_branch or "main"
        head_resp = self._request(
            "GET", f"/repos/{repo}/commits/{branch}", action="sync_repo", repo=repo
        )
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
            resp = self._request("GET", "/rate_limit", action="health")
        except GitHubError as exc:
            return HealthResult(healthy=False, status="error", message=str(exc), checked_at=_now())
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
    # Git Data API primitives (HARD-01) — branch push without a clone     #
    # ------------------------------------------------------------------ #

    def create_branch(self, repo: str, *, new_branch: str, from_ref: str = "main") -> str:
        """Create ``refs/heads/{new_branch}`` at the tip of ``from_ref``.

        Returns the base commit SHA the new branch points at.
        """
        r = self.owner_repo(repo)
        ref_resp = self._request(
            "GET",
            f"/repos/{r}/git/ref/heads/{from_ref}",
            action="create_branch",
            repo=r,
        )
        self._raise_for_status(ref_resp)
        base_sha = str((ref_resp.json().get("object") or {}).get("sha") or "")
        create_resp = self._request(
            "POST",
            f"/repos/{r}/git/refs",
            json={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
            action="create_branch",
            repo=r,
        )
        self._raise_for_status(create_resp)
        return base_sha

    def push_files(
        self,
        repo: str,
        *,
        branch: str,
        files: dict[str, str],
        message: str,
        base_ref: str = "main",
    ) -> str:
        """Commit ``files`` onto ``branch`` via the Git Data API (no local git).

        Sequence: resolve the branch tip → its base tree → create a blob per file
        → create a tree → create a commit → fast-forward the branch ref. Returns
        the new commit SHA. ``base_ref`` is the fallback base when ``branch`` does
        not yet resolve.
        """
        r = self.owner_repo(repo)
        # Parent commit: the current tip of the target branch (fall back to base).
        ref_resp = self._request(
            "GET", f"/repos/{r}/git/ref/heads/{branch}", action="push_files", repo=r
        )
        if ref_resp.status_code == 404:
            ref_resp = self._request(
                "GET",
                f"/repos/{r}/git/ref/heads/{base_ref}",
                action="push_files",
                repo=r,
            )
        self._raise_for_status(ref_resp)
        parent_sha = str((ref_resp.json().get("object") or {}).get("sha") or "")

        commit_resp = self._request(
            "GET", f"/repos/{r}/git/commits/{parent_sha}", action="push_files", repo=r
        )
        self._raise_for_status(commit_resp)
        base_tree_sha = str((commit_resp.json().get("tree") or {}).get("sha") or "")

        tree_entries: list[dict[str, Any]] = []
        for filepath, content in files.items():
            blob_resp = self._request(
                "POST",
                f"/repos/{r}/git/blobs",
                json={"content": content, "encoding": "utf-8"},
                action="push_files",
                repo=r,
            )
            self._raise_for_status(blob_resp)
            blob_sha = str(blob_resp.json().get("sha") or "")
            tree_entries.append(
                {"path": filepath, "mode": "100644", "type": "blob", "sha": blob_sha}
            )

        tree_resp = self._request(
            "POST",
            f"/repos/{r}/git/trees",
            json={"base_tree": base_tree_sha, "tree": tree_entries},
            action="push_files",
            repo=r,
        )
        self._raise_for_status(tree_resp)
        new_tree_sha = str(tree_resp.json().get("sha") or "")

        new_commit_resp = self._request(
            "POST",
            f"/repos/{r}/git/commits",
            json={"message": message, "tree": new_tree_sha, "parents": [parent_sha]},
            action="push_files",
            repo=r,
        )
        self._raise_for_status(new_commit_resp)
        new_commit_sha = str(new_commit_resp.json().get("sha") or "")

        update_resp = self._request(
            "PATCH",
            f"/repos/{r}/git/refs/heads/{branch}",
            json={"sha": new_commit_sha, "force": False},
            action="push_files",
            repo=r,
        )
        self._raise_for_status(update_resp)
        return new_commit_sha

    # ------------------------------------------------------------------ #
    # PR review reads + lifecycle (HARD-01)                              #
    # ------------------------------------------------------------------ #

    def list_review_comments(self, pr: PullRequest) -> list[ReviewComment]:
        """Paginated read of ``GET /repos/{r}/pulls/{n}/comments``."""
        r = self.owner_repo(pr.repo)
        return [
            ReviewComment(
                id=item.get("id"),
                body=item.get("body"),
                path=item.get("path"),
                user=(item.get("user") or {}).get("login"),
            )
            for item in self._paginate(
                "GET",
                f"/repos/{r}/pulls/{pr.number}/comments",
                params={"per_page": 100},
                action="list_review_comments",
                repo=r,
            )
        ]

    def list_reviews(self, pr: PullRequest) -> list[Review]:
        """Paginated read of ``GET /repos/{r}/pulls/{n}/reviews``."""
        r = self.owner_repo(pr.repo)
        return [
            Review(
                id=item.get("id"),
                state=item.get("state"),
                body=item.get("body"),
                user=(item.get("user") or {}).get("login"),
            )
            for item in self._paginate(
                "GET",
                f"/repos/{r}/pulls/{pr.number}/reviews",
                params={"per_page": 100},
                action="list_reviews",
                repo=r,
            )
        ]

    def list_pr_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Paginated read of ``GET /repos/{r}/pulls/{n}/files`` (raw file objects).

        Each item carries ``filename``/``status``/``patch`` — the unified-diff
        the Self-Eval Gate miner (F41) parses for added/changed test node ids.
        Returned as raw dicts so this SDK never depends on ``forge_eval``.
        """
        r = self.owner_repo(repo)
        return list(
            self._paginate(
                "GET",
                f"/repos/{r}/pulls/{number}/files",
                params={"per_page": 100},
                action="list_pr_files",
                repo=r,
            )
        )

    def pr_base_commit(self, repo: str, number: int) -> str:
        """Return a PR's base commit sha (``base.sha`` of ``GET .../pulls/{n}``).

        The "before" ref the Self-Eval Gate miner (F41) replays added tests
        against to confirm they fail prior to the merge.
        """
        return self._pr_ref_sha(repo, number, "base")

    def pr_head_commit(self, repo: str, number: int) -> str:
        """Return a PR's head commit sha (``head.sha`` of ``GET .../pulls/{n}``).

        The "after" ref (the merged change) the Self-Eval Gate miner (F41)
        replays added tests against to confirm they now pass.
        """
        return self._pr_ref_sha(repo, number, "head")

    def _pr_ref_sha(self, repo: str, number: int, side: str) -> str:
        r = self.owner_repo(repo)
        resp = self._request("GET", f"/repos/{r}/pulls/{number}", action="get_pr", repo=r)
        self._raise_for_status(resp)
        return str((resp.json().get(side) or {}).get("sha") or "")

    def close_pr(self, pr: PullRequest) -> PullRequest:
        """Close a PR (``PATCH .../pulls/{n}`` with ``state=closed``)."""
        r = self.owner_repo(pr.repo)
        resp = self._request(
            "PATCH",
            f"/repos/{r}/pulls/{pr.number}",
            json={"state": "closed"},
            action="close_pr",
            repo=r,
        )
        self._raise_for_status(resp)
        return self._parse_pr(pr.repo, resp.json())

    def delete_branch(self, repo: str, branch: str) -> None:
        """Delete ``refs/heads/{branch}`` (test cleanup + the J5 disconnect path)."""
        r = self.owner_repo(repo)
        resp = self._request(
            "DELETE",
            f"/repos/{r}/git/refs/heads/{branch}",
            action="delete_branch",
            repo=r,
        )
        # A already-deleted branch (422/404) is not an error for idempotent cleanup.
        if resp.status_code not in (204, 404, 422):
            self._raise_for_status(resp)

    # ------------------------------------------------------------------ #
    # internals                                                          #
    # ------------------------------------------------------------------ #

    def _full(self, display_repo: str, repo: str, head_sha: str) -> RepoSyncResult:
        blobs = 0
        for entry in self._paginate(
            "GET",
            f"/repos/{repo}/git/trees/{head_sha}",
            params={"recursive": "1"},
            items_key="tree",
            action="sync_repo",
            repo=repo,
        ):
            if entry.get("type") == "blob":
                blobs += 1
        return RepoSyncResult(
            repo=display_repo, head_sha=head_sha, files_changed=blobs, indexed=blobs
        )

    def _incremental(
        self, display_repo: str, repo: str, base_sha: str, head_sha: str
    ) -> RepoSyncResult:
        files = list(
            self._paginate(
                "GET",
                f"/repos/{repo}/compare/{base_sha}...{head_sha}",
                items_key="files",
                action="sync_repo",
                repo=repo,
            )
        )
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
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            json={"labels": labels},
            action="add_labels",
            repo=repo,
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
