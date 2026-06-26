"""Declarative base, naming convention, and reusable column mixins.

Every Forge model inherits from :class:`Base` (consistent constraint naming so
Alembic autogenerate is deterministic) and composes the mixins here for UUID
primary keys, timestamps, and workspace (tenant) scoping.

Portability: JSON columns use ``JSONB`` on Postgres and generic ``JSON``
elsewhere so the full metadata creates on SQLite for fast unit tests.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    MetaData,
    Uuid,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

# Constraint naming convention — keeps Alembic migrations stable across runs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def json_type() -> TypeEngine[Any]:
    """JSON column type: ``JSONB`` on Postgres, generic ``JSON`` elsewhere."""
    return JSON().with_variant(JSONB(), "postgresql")


def enum_type[E: enum.Enum](enum_cls: type[E]) -> SAEnum:
    """String-backed enum column storing the member ``value`` (cross-dialect).

    ``native_enum=False`` renders as VARCHAR + CHECK (no Postgres native ENUM,
    keeping migrations simple); ``values_callable`` persists ``member.value`` so
    stored strings match the spec verbatim (e.g. ``agent-runner``).
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class Base(DeclarativeBase):
    """Declarative base shared by every Forge model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Adds a client-generated UUID primary key ``id``."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` timestamps (timezone-aware)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkspaceScopedMixin:
    """Adds the tenant-scoping ``workspace_id`` foreign key.

    Declared with ``declared_attr`` semantics via a plain annotated column so the
    FK resolves on every concrete subclass.
    """

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class ForgeModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Base for non-tenant models (UUID PK + timestamps)."""

    __abstract__ = True


class WorkspaceScopedModel(WorkspaceScopedMixin, ForgeModel):
    """Base for tenant models (UUID PK + timestamps + workspace scoping)."""

    __abstract__ = True
