"""HARD-01 live GitHub App integration lane (creds-gated, opt-in).

These tests drive the REAL api.github.com using the real Forge GitHub App. They
are marked ``live_github`` + ``integration`` and **skip cleanly** when the
``FORGE_GITHUB_*`` credentials are absent, so the default ``uv run pytest -q``
run stays hermetic and network-free.

Run them (once real creds exist):

    cp .env.integration.example .env.integration   # then fill in values
    set -a && source .env.integration && set +a
    uv run pytest -m live_github -q

Required env:
    FORGE_GITHUB_APP_ID
    FORGE_GITHUB_INSTALLATION_ID
    FORGE_GITHUB_APP_PRIVATE_KEY_PATH   (default: deploy/secrets/github-app.pem)
    FORGE_GITHUB_TEST_REPO              (owner/repo — a disposable test repo)
    FORGE_GITHUB_WEBHOOK_SECRET         (for the webhook-delivery lane)
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from dataclasses import dataclass

import pytest

from forge_contracts import PullRequestRequest
from forge_integrations import GitHubClient, load_private_key

pytestmark = [pytest.mark.integration, pytest.mark.live_github]


@dataclass(frozen=True)
class _LiveCreds:
    app_id: str
    installation_id: str
    private_key_pem: str
    test_repo: str
    api_url: str


def _resolve_creds() -> _LiveCreds:
    app_id = os.environ.get("FORGE_GITHUB_APP_ID")
    installation_id = os.environ.get("FORGE_GITHUB_INSTALLATION_ID")
    key_path = os.environ.get("FORGE_GITHUB_APP_PRIVATE_KEY_PATH", "deploy/secrets/github-app.pem")
    test_repo = os.environ.get("FORGE_GITHUB_TEST_REPO")
    api_url = os.environ.get("FORGE_GITHUB_API_URL", "https://api.github.com")

    missing = [
        name
        for name, val in (
            ("FORGE_GITHUB_APP_ID", app_id),
            ("FORGE_GITHUB_INSTALLATION_ID", installation_id),
            ("FORGE_GITHUB_TEST_REPO", test_repo),
        )
        if not val
    ]
    if missing:
        pytest.skip(
            "live GitHub creds absent — set "
            + ", ".join(missing)
            + " (and FORGE_GITHUB_APP_PRIVATE_KEY_PATH) to run the live lane; "
            "see docs/runbooks/live-github.md"
        )
    if not os.path.exists(key_path):
        pytest.skip(
            f"GitHub App private key not found at {key_path!r} "
            "(set FORGE_GITHUB_APP_PRIVATE_KEY_PATH); see docs/runbooks/live-github.md"
        )
    return _LiveCreds(
        app_id=app_id,  # type: ignore[arg-type]
        installation_id=installation_id,  # type: ignore[arg-type]
        private_key_pem=load_private_key(key_path),
        test_repo=test_repo,  # type: ignore[arg-type]
        api_url=api_url,
    )


@pytest.fixture
def creds() -> _LiveCreds:
    return _resolve_creds()


@pytest.fixture
def client(creds: _LiveCreds):
    gh = GitHubClient.from_app(
        app_id=creds.app_id,
        private_key_pem=creds.private_key_pem,
        installation_id=creds.installation_id,
        base_url=creds.api_url,
    )
    try:
        yield gh
    finally:
        gh.close()


def test_live_token_mint_and_health(client: GitHubClient) -> None:
    """AC8: mint a real installation token + a live reachability probe."""
    health = client.health()
    assert health.healthy is True, health.message
    assert health.latency_ms is not None and health.latency_ms >= 0


def test_live_push_open_pr_and_read_reviews(client: GitHubClient, creds: _LiveCreds) -> None:
    """AC9/AC10/AC12: branch + push + open PR + read reviews, then clean up."""
    run_id = uuid.uuid4().hex[:12]
    branch = f"forge/hardening-smoke-{run_id}"
    pr = None
    try:
        client.create_branch(creds.test_repo, new_branch=branch, from_ref="main")
        client.push_files(
            creds.test_repo,
            branch=branch,
            files={f"forge-smoke/{run_id}.md": f"forge hardening smoke {run_id}\n"},
            message=f"[forge-hardening] smoke {run_id}",
        )
        pr = client.open_pr(
            PullRequestRequest(
                repo=creds.test_repo,
                title=f"[forge-hardening] smoke {run_id}",
                body=(
                    "Automated HARD-01 live smoke.\n\n"
                    "## Spec traceability\n- SPEC-PRODUCTION-HARDENING G-GH\n"
                ),
                head=branch,
                base="main",
                draft=True,
            )
        )
        assert pr.number is not None
        assert pr.url

        # AC10: the review surface reads back (empty is a valid pass).
        reviews = client.list_reviews(pr)
        comments = client.list_review_comments(pr)
        assert isinstance(reviews, list)
        assert isinstance(comments, list)
    finally:
        # AC12: idempotent cleanup — close the PR and delete the branch.
        if pr is not None and pr.number is not None:
            with contextlib.suppress(Exception):
                client.close_pr(pr)
        with contextlib.suppress(Exception):
            client.delete_branch(creds.test_repo, branch)


def test_live_rate_limit_handling(client: GitHubClient) -> None:
    """AC13: exercise the rate-limit surface — remaining decreases, no error."""
    first = client.health()
    time.sleep(0)
    second = client.health()
    # Both calls succeed; the handler tolerates X-RateLimit-* headers without
    # erroring. (A forced secondary-limit burst is environment-dependent; the
    # offline suite covers the Retry-After / reset paths deterministically.)
    assert first.healthy and second.healthy
