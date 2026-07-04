"""SQLAlchemy 2.x data model and Alembic migrations for Forge.

This is the shared data-model substrate every other Forge package consumes.
Importing :mod:`forge_db.models` registers all models on ``Base.metadata``.
"""

from __future__ import annotations

from forge_db.base import (
    Base,
    ForgeModel,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    WorkspaceScopedMixin,
    WorkspaceScopedModel,
)
from forge_db.session import (
    create_db_engine,
    create_session_factory,
    get_database_url,
    get_session,
)

__version__ = "0.1.0"

__all__ = [
    "Base",
    "ForgeModel",
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
