"""Engine / session factory and FastAPI-style session dependency.

The default database URL is read from ``FORGE_DATABASE_URL`` (falling back to a
local Postgres DSN). Engines are created lazily so importing this module never
opens a connection — important for unit tests that use their own SQLite engine.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://forge:forge@localhost:5432/forge"


def get_database_url() -> str:
    """Resolve the active database URL from the environment."""
    return os.environ.get("FORGE_DATABASE_URL", DEFAULT_DATABASE_URL)


def create_db_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Create a SQLAlchemy :class:`Engine` for ``url`` (or the configured URL)."""
    return create_engine(url or get_database_url(), **kwargs)


def create_session_factory(
    engine: Engine | None = None, **engine_kwargs: Any
) -> sessionmaker[Session]:
    """Build a ``sessionmaker`` bound to ``engine`` (created if not provided)."""
    bound = engine or create_db_engine(**engine_kwargs)
    return sessionmaker(bind=bound, expire_on_commit=False, class_=Session)


@lru_cache(maxsize=1)
def _default_session_factory() -> sessionmaker[Session]:
    return create_session_factory()


def get_session() -> Iterator[Session]:
    """Yield a session from the default factory; used as a DI dependency."""
    factory = _default_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
