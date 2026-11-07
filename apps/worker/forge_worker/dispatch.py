"""Dispatcher: board task -> agent objective, over a fresh repository mirror (spec G §4a).

``ensure_mirror`` keeps one bare clone per connected repository under
``FORGE_REPO_MIRRORS_DIR/<owner>/<name>`` and fetches it before a run with an
installation token scoped to that repository and ``contents:read`` only, passed
to git through environment configuration (never argv). ``objective_for_task``
maps a ``task`` row onto the objective the worker runs; runs never share a
worktree, only the mirror's object store.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_contracts import AgentObjective
from forge_db.models import Task
from forge_integrations.github_auth import InstallationTokenProvider
from forge_worker.finalizer import GitHubAppCredentials

#: The only permission a mirror-fetch token carries.
MIRROR_TOKEN_PERMISSIONS: dict[str, str] = {"contents": "read"}

TokenMinter = Callable[[str], str]


def mirrors_dir() -> Path:
    return Path(os.environ.get("FORGE_REPO_MIRRORS_DIR", "/var/lib/forge/mirrors"))


_REPO_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}")


def split_repo_name(repo_full_name: str) -> tuple[str, str]:
    """``owner/name`` with GitHub's character set; anything path-like is refused."""
    owner, sep, name = repo_full_name.partition("/")
    for part in (owner, name):
        if not sep or not _REPO_PART.fullmatch(part) or part in {".", ".."}:
            raise ValueError(f"repo must be 'owner/name', got {repo_full_name!r}")
    return owner, name


def workspace_mirrors(workspace_id: uuid.UUID) -> Path:
    """Mirrors are namespaced per workspace, so a run can only ever reach its own tenant's."""
    return mirrors_dir() / str(workspace_id)


class RepositoryUnavailable(RuntimeError):
    """The run names a repository the worker cannot provide."""


def resolve_run_repository(
    objective: AgentObjective,
    *,
    credentials: GitHubAppCredentials | None = None,
    minter: Callable[[GitHubAppCredentials], TokenMinter] | None = None,
) -> AgentObjective:
    """Point ``context["repo_path"]`` at the workspace's mirror of ``repo_full_name``.

    ``repo_full_name`` and ``workspace_id`` are server-owned context keys (the API strips
    callers' values and checks the repository is connected to the workspace). The path is
    always derived here, never taken from the caller. With GitHub App credentials the
    mirror is cloned or fetched with a read-only one-repo token; without them an existing
    mirror is used as is, and a missing one refuses.
    """
    context = objective.context
    full_name = context.get("repo_full_name")
    if not full_name:
        return objective
    workspace_id = uuid.UUID(str(context["workspace_id"]))
    try:
        owner, name = split_repo_name(str(full_name))
    except ValueError as exc:
        raise RepositoryUnavailable(str(exc)) from exc
    root = workspace_mirrors(workspace_id)
    path = root / owner / name
    creds = credentials if credentials is not None else GitHubAppCredentials.from_env()
    if creds is not None:
        token = (minter or read_token_minter)(creds)(name)
        path = ensure_mirror(
            str(full_name),
            root=root,
            remote_url=f"{creds.git_base_url}/{owner}/{name}.git",
            token=token,
        )
    elif not (path / "HEAD").is_file():
        raise RepositoryUnavailable(
            f"no mirror of {full_name} for this workspace and no GitHub App credentials to fetch it"
        )
    return objective.model_copy(update={"context": {**context, "repo_path": str(path)}})


def read_token_minter(credentials: GitHubAppCredentials) -> TokenMinter:
    """Mint a ``contents:read`` token for one repository name."""

    def mint(repo_name: str) -> str:
        provider = InstallationTokenProvider(
            app_id=credentials.app_id,
            private_key_pem=credentials.private_key_pem,
            installation_id=credentials.installation_id,
            base_url=credentials.api_base_url,
            repositories=[repo_name],
            permissions=MIRROR_TOKEN_PERMISSIONS,
        )
        try:
            return provider.token()
        finally:
            provider.close()

    return mint


def _git_env(token: str | None) -> dict[str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }
    return env


def ensure_mirror(
    repo_full_name: str,
    *,
    root: Path,
    remote_url: str,
    token: str | None = None,
) -> Path:
    """Bare-clone ``repo_full_name`` under ``root`` if absent, else fetch every branch.

    Bare, so fetching never collides with a checked-out branch; worktrees are
    added from it per run. No ``--prune``: run branches that were never pushed
    live only here and must not be deleted by a fetch.
    """
    owner, name = split_repo_name(repo_full_name)
    path = root / owner / name
    git = ["git", "-c", "core.hooksPath=/dev/null"]
    if not (path / "HEAD").is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [*git, "clone", "--bare", "--quiet", remote_url, str(path)],
            env=_git_env(token),
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            [*git, "fetch", "--quiet", remote_url, "+refs/heads/*:refs/heads/*"],
            cwd=path,
            env=_git_env(token),
            check=True,
            capture_output=True,
        )
    return path


def objective_for_task(
    task: Task,
    *,
    repo_path: Path,
    agent_run_id: uuid.UUID,
    repo_full_name: str,
) -> AgentObjective:
    """The objective for one board task, acting on ``repo_path``."""
    raw: dict[str, Any] = {
        "task_id": task.id,
        "key": task.key,
        "objective": task.title,
        "description": task.description,
        "execution_mode": task.execution_mode.value,
        "repo_targets": task.repo_targets,
        "acceptance_criteria": task.acceptance_criteria,
        "allowed_actions": task.allowed_actions,
        "restricted_actions": task.restricted_actions,
        "context": {
            "repo_path": str(repo_path),
            "repo_full_name": repo_full_name,
            "workspace_id": str(task.workspace_id),
            "agent_run_id": str(agent_run_id),
        },
    }
    return AgentObjective.model_validate(raw)


__all__ = [
    "MIRROR_TOKEN_PERMISSIONS",
    "RepositoryUnavailable",
    "ensure_mirror",
    "mirrors_dir",
    "objective_for_task",
    "read_token_minter",
    "resolve_run_repository",
    "split_repo_name",
    "workspace_mirrors",
]
