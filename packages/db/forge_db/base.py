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
    DDL,
    JSON,
    DateTime,
    ForeignKey,
    MetaData,
    Table,
    Uuid,
    event,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.expression import FromClause
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


def enum_type[E: enum.Enum](enum_cls: type[E], *, length: int | None = None) -> SAEnum:
    """String-backed enum column storing the member ``value`` (cross-dialect).

    ``native_enum=False`` renders as VARCHAR + CHECK (no Postgres native ENUM,
    keeping migrations simple); ``values_callable`` persists ``member.value`` so
    stored strings match the spec verbatim (e.g. ``agent-runner``).

    ``length`` pins the rendered ``VARCHAR`` width. Left ``None`` (the default),
    SQLAlchemy sizes the column to the longest enum value; pass an explicit width
    when a migration has deliberately widened the DB column beyond the current
    vocabulary (e.g. F40's ``VARCHAR(32)`` headroom on the automation trigger
    columns) so the ORM metadata renders the same width and no drift is reported.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
        length=length,
    )


#: Name of the shared plpgsql function that raises on UPDATE/DELETE.
_IMMUTABILITY_FN = "forge_block_mutation"


def attach_immutability_trigger(table: FromClause) -> None:
    """Make ``table`` append-only on Postgres (BEFORE UPDATE/DELETE → raise).

    This is the reusable F39-audit-log helper that per-domain append-only tables
    opt into (here: ``automation_execution``). It is a no-op on non-Postgres
    dialects (SQLite unit tests), where the repository layer enforces
    append-only by exposing no update/delete path. Registered on the table's
    ``after_create`` so it applies via both ``create_all`` and Alembic.

    Callers pass ``Model.__table__``, which SQLAlchemy types as the wider
    ``FromClause``; it is always a concrete ``Table`` at runtime, so narrow it.
    """
    if not isinstance(table, Table):  # pragma: no cover - defensive, never hit
        raise TypeError("attach_immutability_trigger requires a mapped Table")

    create_fn = DDL(
        f"""
        CREATE OR REPLACE FUNCTION {_IMMUTABILITY_FN}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'table %% is append-only and cannot be modified',
                TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    trigger_name = f"{table.name}_immutable"
    create_trigger = DDL(
        f"""
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE OR DELETE ON {table.name}
        FOR EACH ROW EXECUTE FUNCTION {_IMMUTABILITY_FN}();
        """
    )
    drop_trigger = DDL(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table.name};")

    event.listen(table, "after_create", create_fn.execute_if(dialect="postgresql"))
    event.listen(table, "after_create", create_trigger.execute_if(dialect="postgresql"))
    event.listen(table, "before_drop", drop_trigger.execute_if(dialect="postgresql"))


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
