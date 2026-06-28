"""Seed a demo workspace for local development (idempotent).

Run via ``make seed`` / ``python -m forge_api.scripts.seed`` (host) or the
compose ``seed`` one-shot service. Re-running is safe: it creates the demo
workspace + admin user only when they are absent, so it can run on every
``docker compose up`` without duplicating rows.

The database URL is resolved from ``FORGE_DATABASE_URL`` (shared with the rest
of the workspace via ``forge_db.session``).
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from forge_db.models import User, Workspace
from forge_db.models.enums import UserRole
from forge_db.session import create_session_factory

logger = logging.getLogger("forge_api.seed")

#: Stable identifiers for the demo tenant so re-runs are idempotent.
DEMO_WORKSPACE_SLUG = "demo"
DEMO_WORKSPACE_NAME = "Demo Workspace"
DEMO_ADMIN_EMAIL = "admin@forge.local"
DEMO_ADMIN_NAME = "Demo Admin"


def seed() -> None:
    """Create the demo workspace + admin user if they do not already exist."""
    factory = create_session_factory()
    with factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == DEMO_WORKSPACE_SLUG)
        )
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
                role=UserRole.ADMIN,
            )
            session.add(admin)
            logger.info("created demo admin user email=%s", DEMO_ADMIN_EMAIL)
        else:
            logger.info("demo admin user already present email=%s", DEMO_ADMIN_EMAIL)

        session.commit()

    print(
        f"Seed complete: workspace slug={DEMO_WORKSPACE_SLUG!r} "
        f"admin={DEMO_ADMIN_EMAIL!r}"
    )


def main() -> None:
    """CLI entrypoint."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    seed()


if __name__ == "__main__":
    main()
