"""Seed a demo workspace for local development (idempotent).

Run via ``make seed`` / ``python -m forge_api.scripts.seed`` (host) or the
compose ``seed`` one-shot service. Re-running is safe: it creates the demo
workspace + admin user only when they are absent, so it can run on every
``docker compose up`` without duplicating rows.

The database URL is resolved from ``FORGE_DATABASE_URL`` (shared with the rest
of the workspace via ``forge_db.session``).

**Bootstrap credential.** A fresh install has no way to authenticate: the web UI
has no password login, and ``POST /auth/login`` starts an OAuth handshake that
needs an IdP nobody has configured yet. With ``FORGE_SEED_ADMIN_KEY`` enabled
this script therefore mints an admin API key for the demo workspace and prints
it once, which is the credential the UI's *Connect* dialog and every ``curl``
example expect. It is **opt-in and dev-only**: the token is written to stdout,
which on a compose stack means the container log, so a production seed never
emits one unless an operator explicitly asks for it.
"""

from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import select

from forge_contracts.enums import APIKeyKind
from forge_contracts.enums import UserRole as ContractUserRole
from forge_db.models import Project, User, Workspace
from forge_db.models.enums import UserRole as DbUserRole
from forge_db.session import create_session_factory

logger = logging.getLogger("forge_api.seed")

#: Stable identifiers for the demo tenant so re-runs are idempotent.
DEMO_WORKSPACE_SLUG = "demo"
DEMO_WORKSPACE_NAME = "Demo Workspace"
DEMO_ADMIN_EMAIL = "admin@forge.local"
DEMO_ADMIN_NAME = "Demo Admin"
DEMO_PROJECT_KEY = "DEMO"
DEMO_PROJECT_NAME = "Demo Project"

#: Name carried by the key this script mints. Stable so a re-run can retire the
#: previous one instead of accumulating live admin credentials.
BOOTSTRAP_KEY_NAME = "seed-bootstrap"

#: Environment flag gating the mint. Any of these values enables it.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _mint_admin_key_enabled() -> bool:
    """True when ``FORGE_SEED_ADMIN_KEY`` opts this run into minting a key."""
    return os.environ.get("FORGE_SEED_ADMIN_KEY", "").strip().lower() in _TRUTHY


def warn_if_key_is_ephemeral() -> str | None:
    """Return a warning when the minted key will not survive a restart.

    ``FORGE_APIKEY_BACKEND`` defaults to ``memory``, which keeps minted keys in
    the API process. A key minted by *this* process under that setting is worse
    than useless: it never reaches the database, so the API never sees it and
    every request with it 401s. The compose files set ``db``; a hand-run seed
    against a stack that did not is worth saying out loud rather than handing
    back a token that silently does nothing.
    """
    from forge_api.settings import get_settings

    if get_settings().apikey_backend == "db":
        return None
    return (
        "FORGE_APIKEY_BACKEND is not 'db', so API keys live in a single process's "
        "memory. A key minted here will NOT be visible to the API and will not "
        "survive a restart. Set FORGE_APIKEY_BACKEND=db (the compose files do)."
    )


def mint_bootstrap_key(workspace_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Retire any previous bootstrap key, mint a fresh admin key, return the token.

    Revoking first keeps exactly one live ``seed-bootstrap`` credential no matter
    how many times ``up``/``make dev-seed`` re-runs the seed — the token itself is
    unrecoverable after the mint, so a re-run has to issue a new one and the old
    one would otherwise stay valid forever.
    """
    from forge_api.auth.service import AuthService

    service = AuthService()
    for existing in service.api_keys.list_keys(workspace_id):
        if existing.name == BOOTSTRAP_KEY_NAME and existing.is_active:
            service.api_keys.revoke(workspace_id, existing.id)
            logger.info("revoked previous bootstrap key id=%s", existing.id)

    _info, token = service.bootstrap_key(
        workspace_id=workspace_id,
        name=BOOTSTRAP_KEY_NAME,
        role=ContractUserRole.ADMIN,
        user_id=user_id,
        kind=APIKeyKind.SYSTEM,
    )
    return token


def _print_bootstrap_key(token: str) -> None:
    """Print the one-time token with the two things a reader needs next."""
    print(
        "\n"
        "  Admin API key (shown once — it is not recoverable):\n\n"
        f"    {token}\n\n"
        "  Paste it into the web UI's Connect dialog, or use it directly:\n\n"
        f'    curl -H "Authorization: Bearer {token}" http://localhost:8080/api/auth/me\n\n'
        "  This key grants full admin access to the demo workspace. It is a\n"
        "  local-development credential — never mint one this way in production.\n"
    )


def seed() -> None:
    """Create the demo workspace + admin user if they do not already exist."""
    factory = create_session_factory()
    with factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == DEMO_WORKSPACE_SLUG))
        if workspace is None:
            workspace = Workspace(name=DEMO_WORKSPACE_NAME, slug=DEMO_WORKSPACE_SLUG)
            session.add(workspace)
            session.flush()
            logger.info("created demo workspace id=%s slug=%s", workspace.id, workspace.slug)
        else:
            logger.info("demo workspace already present id=%s", workspace.id)

        admin = session.scalar(
            select(User).where(
                User.workspace_id == workspace.id,
                User.email == DEMO_ADMIN_EMAIL,
            )
        )
        if admin is None:
            admin = User(
                workspace_id=workspace.id,
                email=DEMO_ADMIN_EMAIL,
                name=DEMO_ADMIN_NAME,
                role=DbUserRole.ADMIN,
            )
            session.add(admin)
            session.flush()
            logger.info("created demo admin user email=%s", DEMO_ADMIN_EMAIL)
        else:
            logger.info("demo admin user already present email=%s", DEMO_ADMIN_EMAIL)

        # A task's project is a required FK, so a workspace with no project can
        # hold no work at all: the board's New-task dialog does not ask for a
        # project, and without one every create fails at the database. Seeding
        # one is what makes a fresh install usable.
        project = session.scalar(
            select(Project).where(
                Project.workspace_id == workspace.id,
                Project.key == DEMO_PROJECT_KEY,
            )
        )
        if project is None:
            project = Project(
                workspace_id=workspace.id,
                name=DEMO_PROJECT_NAME,
                key=DEMO_PROJECT_KEY,
                description="Default project for the demo workspace.",
            )
            session.add(project)
            session.flush()
            logger.info("created demo project id=%s key=%s", project.id, project.key)
        else:
            logger.info("demo project already present id=%s", project.id)

        workspace_id = workspace.id
        admin_id = admin.id
        session.commit()

    print(
        f"Seed complete: workspace slug={DEMO_WORKSPACE_SLUG!r} "
        f"admin={DEMO_ADMIN_EMAIL!r} project={DEMO_PROJECT_KEY!r}"
    )

    if not _mint_admin_key_enabled():
        print(
            "No API key minted. A fresh install has no other way to sign in — "
            "re-run with FORGE_SEED_ADMIN_KEY=1 to mint one (development only)."
        )
        return

    warning = warn_if_key_is_ephemeral()
    if warning:
        logger.warning("%s", warning)

    _print_bootstrap_key(mint_bootstrap_key(workspace_id, admin_id))
    if warning:
        print(f"  WARNING: {warning}\n")


def main() -> None:
    """CLI entrypoint."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    seed()


if __name__ == "__main__":
    main()
