"""Database session dependency injection for the API.

The engine and session factory are created lazily (cached) from
:class:`~forge_api.settings.Settings`, so importing this module never opens a
connection — Phase-0 stub routes return before ever requesting a session, and
unit tests run without a live Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.settings import get_settings
from forge_db import create_db_engine, create_session_factory


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine built from settings."""
    settings = get_settings()
    return create_db_engine(settings.database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide ``sessionmaker`` bound to :func:`get_engine`."""
    return create_session_factory(get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session and always close it afterwards."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
