"""Run finalizer: push the run branch and open its PR (spec G §4).

Runs after the agent loop, outside the sandbox, never as an agent tool
(ADR-0014). The GitHub App mints an installation token for this one call,
narrowed to the run's repository with ``contents:write`` and
``pull_requests:write`` only (ADR-0008), from the credentials of the run's own
workspace (#186), and each token issued is audited. The token reaches git through
environment configuration, never through command-line arguments, and repository
hooks are disabled. Opening a PR never merges it: the ``pr`` approval gate and a
human decide that.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_contracts import PullRequest, PullRequestRequest
from scf_contracts.audit import AuditEvent
from scf_db.audit.writer import SqlAuditWriter
from scf_db.models import RepositoryConnection
from scf_db.session import create_session_factory
from scf_integrations.github import DEFAULT_BASE_URL, GitHubClient
from scf_integrations.github_auth import WORKSPACE_SECRET_NAME, InstallationTokenProvider

#: The only permissions a run token carries.
RUN_TOKEN_PERMISSIONS: dict[str, str] = {"contents": "write", "pull_requests": "write"}

Pusher = Callable[[Path, str, str, str], None]
SessionFactory = Callable[[], Session]

#: An installation id becomes a URL path segment, so it is digits and nothing else.
_INSTALLATION_ID = re.compile(r"[0-9]{1,20}")


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


def _workspace_vault() -> Any:
    """The workspace secret vault the API writes (``FORGE_SECRET_BACKEND``)."""
    from scf_api.auth.service import get_auth_service

    return get_auth_service().vault


def _development() -> bool:
    from scf_api.settings import get_settings

    return get_settings().environment == "development"


def workspace_credentials(
    workspace_id: uuid.UUID,
    repo_full_name: str | None = None,
    *,
    session_factory: SessionFactory | None = None,
) -> GitHubAppCredentials | None:
    """``workspace_id``'s GitHub App credentials from its vault, or ``None`` (#186).

    The installation is the one the workspace's active connection of
    ``repo_full_name`` names, else the stored one. Only development falls back to
    the process-wide ``FORGE_GITHUB_*`` variables; anywhere else a workspace that
    stored no credentials has none.
    """
    vault = _workspace_vault()
    stored = next(
        (s for s in vault.list_secrets(workspace_id) if s.name == WORKSPACE_SECRET_NAME), None
    )
    if stored is None:
        return GitHubAppCredentials.from_env() if _development() else None
    try:
        data = json.loads(vault.get_secret(workspace_id, stored.id))
        app_id, key = str(data["app_id"]), str(data["private_key_pem"])
    except (KeyError, TypeError, ValueError):  # expired or malformed: unusable, never echoed
        return None
    installation = None
    if repo_full_name:
        with (session_factory or create_session_factory())() as session:
            installation = session.scalar(
                select(RepositoryConnection.installation_id)
                .where(
                    RepositoryConnection.workspace_id == workspace_id,
                    RepositoryConnection.is_active.is_(True),
                    RepositoryConnection.installation_id.is_not(None),
                    (RepositoryConnection.repo_id == repo_full_name)
                    | (RepositoryConnection.name == repo_full_name),
                )
                .limit(1)
            )
    installation = str(installation or data.get("installation_id") or "")
    if not _INSTALLATION_ID.fullmatch(installation):
        return None
    return GitHubAppCredentials(app_id=app_id, installation_id=installation, private_key_pem=key)


def mint_token(
    credentials: GitHubAppCredentials,
    repo_full_name: str,
    permissions: dict[str, str],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    transport: httpx.BaseTransport | None = None,
    session_factory: SessionFactory | None = None,
) -> str:
    """A token for ``repo_full_name`` alone with ``permissions``, audited before use.

    Every call mints afresh and keeps no cache, so no other run or workspace ever
    reuses the token. The audit record names the workspace, run, repository,
    permissions and expiry; never the token, the App JWT or the key.
    """
    _, _, name = repo_full_name.partition("/")
    provider = InstallationTokenProvider(
        app_id=credentials.app_id,
        private_key_pem=credentials.private_key_pem,
        installation_id=credentials.installation_id,
        base_url=credentials.api_base_url,
        transport=transport,
        repositories=[name],
        permissions=permissions,
    )
    try:
        token = provider.token()
        expires_at = datetime.fromtimestamp(provider.expires_at, UTC).isoformat()
    finally:
        provider.close()
    with (session_factory or create_session_factory())() as session:
        SqlAuditWriter(session).emit(
            AuditEvent(
                workspace_id=workspace_id,
                action="github.token_issued",
                actor_type="system",
                target_type="repository",
                details={
                    "run_id": str(run_id) if run_id else None,
                    "repository": repo_full_name,
                    "installation_id": credentials.installation_id,
                    "permissions": dict(permissions),
                    "expires_at": expires_at,
                },
            )
        )
        session.commit()
    return token


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
