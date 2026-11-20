"""Run finalizer: push the run branch and open its PR (spec G §4).

Runs after the agent loop, outside the sandbox, never as an agent tool
(ADR-0014). The GitHub App mints an installation token for this one call,
narrowed to the run's repository with ``contents:write`` and
``pull_requests:write`` only (ADR-0008), from the credentials of the run's own
workspace (``scf_api.services.github_app``, #186), and each token issued is
audited. The token reaches git through environment configuration, never through
command-line arguments, and repository hooks are disabled. Opening a PR never
merges it: the ``pr`` approval gate and a human decide that.
"""

from __future__ import annotations

import base64
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx

from scf_api.services.github_app import (
    GitHubAppCredentials,
    SessionFactory,
    mint_token,
    workspace_credentials,
)
from scf_contracts import PullRequest, PullRequestRequest
from scf_integrations.github import GitHubClient

#: The only permissions a run token carries.
RUN_TOKEN_PERMISSIONS: dict[str, str] = {"contents": "write", "pull_requests": "write"}

Pusher = Callable[[Path, str, str, str], None]


def git_push(repo_path: Path, branch: str, remote_url: str, token: str) -> None:
    """Push ``branch`` to ``remote_url`` authenticating with ``token`` via env config."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            "--no-verify",
            remote_url,
            f"refs/heads/{branch}:refs/heads/{branch}",
        ],
        cwd=repo_path,
        env=env,
        check=True,
        capture_output=True,
    )


def push_and_open_pr(
    *,
    repo_path: Path,
    branch: str,
    repo_full_name: str,
    base_branch: str,
    title: str,
    body: str,
    credentials: GitHubAppCredentials,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    session_factory: SessionFactory | None = None,
    transport: httpx.BaseTransport | None = None,
    push: Pusher = git_push,
    reuse_existing: bool = False,
) -> PullRequest:
    """Push the run branch and open a PR with a token scoped to this repository.

    ``reuse_existing`` first looks up a pull request for the branch and returns it
    instead of opening a second one, so a retried caller opens exactly one.
    """
    owner, _, name = repo_full_name.partition("/")
    if not owner or not name:
        raise ValueError(f"repo must be 'owner/name', got {repo_full_name!r}")
    token = mint_token(
        credentials,
        repo_full_name,
        RUN_TOKEN_PERMISSIONS,
        workspace_id=workspace_id,
        run_id=run_id,
        transport=transport,
        session_factory=session_factory,
    )
    push(repo_path, branch, f"{credentials.git_base_url}/{owner}/{name}.git", token)
    client = GitHubClient(token=token, base_url=credentials.api_base_url, transport=transport)
    try:
        existing = client.find_pr_for_head(repo_full_name, branch) if reuse_existing else None
        if existing is not None:
            return existing
        return client.open_pr(
            PullRequestRequest(
                repo=repo_full_name, title=title, body=body, head=branch, base=base_branch
            )
        )
    finally:
        client.close()


__all__ = [
    "RUN_TOKEN_PERMISSIONS",
    "GitHubAppCredentials",
    "git_push",
    "mint_token",
    "push_and_open_pr",
    "workspace_credentials",
]
