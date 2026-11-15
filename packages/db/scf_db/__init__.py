"""SQLAlchemy 2.x data model and Alembic migrations for SCF.

This is the shared data-model substrate every other SCF package consumes.
Importing :mod:`scf_db.models` registers all models on ``Base.metadata``.
"""

from __future__ import annotations

from scf_db.base import (
    Base,
    ScfModel,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    WorkspaceScopedMixin,
    WorkspaceScopedModel,
)
from scf_db.session import (
    create_db_engine,
    create_session_factory,
    get_database_url,
    get_session,
)

__version__ = "0.1.0"

__all__ = [
    "Base",
    "ScfModel",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "WorkspaceScopedMixin",
    "WorkspaceScopedModel",
    "__version__",
    "create_db_engine",
    "create_session_factory",
    "get_database_url",
    "get_session",
]
