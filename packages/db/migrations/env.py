"""Alembic environment for forge-db.

Resolves the database URL from ``FORGE_DATABASE_URL`` (or the Alembic config),
targets ``forge_db.base.Base.metadata`` (with every model imported so the full
schema is visible to autogenerate), and renders pgvector types correctly.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import forge_db.models  # noqa: F401  (registers all models on Base.metadata)
from forge_db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    env_url = os.environ.get("FORGE_DATABASE_URL")
    if env_url:
        return env_url
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    raise RuntimeError("No database URL configured. Set FORGE_DATABASE_URL or sqlalchemy.url.")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
