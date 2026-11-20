"""A workspace's GitHub App: its credentials and the scoped tokens minted from them (#186).

The credentials come from the workspace's own vault entry. Every token names one
repository and the step's permissions, is minted fresh and is audited before use.
The worker's run steps and the deploy gate (#335) share them; they live here, in
the API that owns the vault, so both can.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_contracts.audit import AuditEvent
from scf_db.audit.writer import SqlAuditWriter
from scf_db.models import RepositoryConnection
from scf_db.session import create_session_factory
from scf_integrations.github import DEFAULT_BASE_URL, GitHubClient
from scf_integrations.github_auth import WORKSPACE_SECRET_NAME, InstallationTokenProvider

__all__ = [
    "COMMIT_READ_PERMISSIONS",
    "GitHubAppCredentials",
    "SessionFactory",
    "mint_token",
    "resolve_commit",
    "workspace_credentials",
]

#: A deploy's commit lookup reads the repository's commits and nothing else (#335).
COMMIT_READ_PERMISSIONS: dict[str, str] = {"contents": "read"}

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


def resolve_commit(
    workspace_id: uuid.UUID,
    repo_id: str,
    ref: str,
    *,
    session_factory: SessionFactory | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """What GitHub says ``ref`` names in ``repo_id``, asked as the workspace's own App (#335).

    ``None`` when the workspace has no credentials for the repository. The token
    reads commits of that repository alone. GitHub's errors propagate: the deploy
    gate counts them, and any answer that is not a full hash, as unresolved.
    """
    repo = GitHubClient.owner_repo(repo_id)
    credentials = workspace_credentials(workspace_id, repo, session_factory=session_factory)
    if credentials is None:
        return None
    token = mint_token(
        credentials,
        repo,
        COMMIT_READ_PERMISSIONS,
        workspace_id=workspace_id,
        transport=transport,
        session_factory=session_factory,
    )
    with GitHubClient(
        token=token, base_url=credentials.api_base_url, transport=transport
    ) as client:
        return client.commit_sha(repo, ref)
