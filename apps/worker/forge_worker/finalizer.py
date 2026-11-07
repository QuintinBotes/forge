"""Run finalizer: push the run branch and open its PR (spec G §4).

Runs after the agent loop, outside the sandbox, never as an agent tool
(ADR-0014). The GitHub App mints an installation token for this one call,
narrowed to the run's repository with ``contents:write`` and
``pull_requests:write`` only (ADR-0008). The token reaches git through
environment configuration, never through command-line arguments, and repository
hooks are disabled. Opening a PR never merges it: the ``pr`` approval gate and a
human decide that.
"""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from forge_contracts import PullRequest, PullRequestRequest
from forge_integrations.github import DEFAULT_BASE_URL, GitHubClient
from forge_integrations.github_auth import InstallationTokenProvider

#: The only permissions a run token carries.
RUN_TOKEN_PERMISSIONS: dict[str, str] = {"contents": "write", "pull_requests": "write"}

Pusher = Callable[[Path, str, str, str], None]


@dataclass(frozen=True, repr=False)
class GitHubAppCredentials:
    app_id: str
    installation_id: str
    private_key_pem: str
    api_base_url: str = DEFAULT_BASE_URL
    git_base_url: str = "https://github.com"

    def __repr__(self) -> str:  # never print the key
        return f"GitHubAppCredentials(app={self.app_id!r}, installation={self.installation_id!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubAppCredentials | None:
        """Credentials from the ``FORGE_GITHUB_*`` variables, or ``None`` when unset."""
        e = os.environ if env is None else env
        app_id = e.get("FORGE_GITHUB_APP_ID")
        installation_id = e.get("FORGE_GITHUB_INSTALLATION_ID")
        key_path = e.get("FORGE_GITHUB_APP_PRIVATE_KEY_PATH")
        if not (app_id and installation_id and key_path and Path(key_path).is_file()):
            return None
        return cls(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=Path(key_path).read_text(),
        )


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
    transport: httpx.BaseTransport | None = None,
    push: Pusher = git_push,
) -> PullRequest:
    """Push the run branch and open a PR with a token scoped to this repository."""
    owner, _, name = repo_full_name.partition("/")
    if not owner or not name:
        raise ValueError(f"repo must be 'owner/name', got {repo_full_name!r}")
    provider = InstallationTokenProvider(
        app_id=credentials.app_id,
        private_key_pem=credentials.private_key_pem,
        installation_id=credentials.installation_id,
        base_url=credentials.api_base_url,
        transport=transport,
        repositories=[name],
        permissions=RUN_TOKEN_PERMISSIONS,
    )
    try:
        token = provider.token()
        push(repo_path, branch, f"{credentials.git_base_url}/{owner}/{name}.git", token)
        client = GitHubClient(token=token, base_url=credentials.api_base_url, transport=transport)
        try:
            return client.open_pr(
                PullRequestRequest(
                    repo=repo_full_name, title=title, body=body, head=branch, base=base_branch
                )
            )
        finally:
            client.close()
    finally:
        provider.close()


__all__ = [
    "RUN_TOKEN_PERMISSIONS",
    "GitHubAppCredentials",
    "git_push",
    "push_and_open_pr",
]
