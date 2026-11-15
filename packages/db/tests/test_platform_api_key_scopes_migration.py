"""Migration 0046: ``platform_api_key.scopes`` and ``surface`` (spec L §6a)."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, create_engine, inspect, text, update
from sqlalchemy.orm import Session, sessionmaker

import forge_db.models  # noqa: F401  (registers all models)
from forge_db.base import Base
from forge_db.models import PlatformAPIKey, Workspace
from forge_db.models.enums import PlatformKeyKind, UserRole

DB_ROOT = Path(__file__).resolve().parent.parent
TABLE = "platform_api_key"


def _load_migration() -> ModuleType:
    path = DB_ROOT / "migrations" / "versions" / "0046_platform_api_key_scopes.py"
    spec = importlib.util.spec_from_file_location("migration_0046", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: Engine, step: str) -> None:
    module = _load_migration()
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


def _columns(engine: Engine) -> dict[str, dict]:
    return {c["name"]: c for c in inspect(engine).get_columns(TABLE)}


def test_migration_0046_chain_on_sqlite(tmp_path: Path) -> None:
    cfg = Config(str(DB_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_ROOT / "migrations"))
    url = f"sqlite:///{tmp_path / 'forge_test.db'}"
    cfg.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)
    try:
        command.upgrade(cfg, "head")
        assert {"scopes", "surface"} <= set(_columns(engine))
        command.downgrade(cfg, "0045_workflow_lines")
        assert not {"scopes", "surface"} & set(_columns(engine))
        command.upgrade(cfg, "head")
        assert {"scopes", "surface"} <= set(_columns(engine))
    finally:
        engine.dispose()


@pytest.fixture
def pg_schema(pg_engine: Engine) -> Iterator[Engine]:
    Base.metadata.create_all(pg_engine)
    try:
        yield pg_engine
    finally:
        Base.metadata.drop_all(pg_engine)


def _key(ws_id: uuid.UUID, scopes: list[str] | None) -> PlatformAPIKey:
    return PlatformAPIKey(
        workspace_id=ws_id,
        name="laptop",
        key_id=uuid.uuid4().hex[:16],
        key_hash="0" * 64,
        key_prefix="forge_sy",
        kind=PlatformKeyKind.SERVICE,
        role=UserRole.MEMBER,
        scopes=scopes,
        surface="tervin" if scopes else None,
    )


def test_migration_0046_down_and_up_on_postgres(pg_schema: Engine) -> None:
    module = _load_migration()
    assert module.revision == "0046_platform_api_key_scopes"
    assert module.down_revision == "0045_workflow_lines"

    factory = sessionmaker(bind=pg_schema, expire_on_commit=False, class_=Session)
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        session.add(_key(ws.id, None))  # a pre-0046 key: no scopes, full role
        session.commit()

    _run(pg_schema, "downgrade")
    assert not {"scopes", "surface"} & set(_columns(pg_schema))

    _run(pg_schema, "upgrade")
    columns = _columns(pg_schema)
    assert str(columns["scopes"]["type"]) == "JSONB"
    assert str(columns["surface"]["type"]) == "VARCHAR(32)"
    assert columns["scopes"]["nullable"] is True and columns["surface"]["nullable"] is True

    with factory() as session:
        session.add(_key(ws.id, ["approvals:decide", "read"]))
        session.commit()
        rows = {tuple(r.scopes or ()): r.surface for r in session.query(PlatformAPIKey)}
    assert rows == {(): None, ("approvals:decide", "read"): "tervin"}

    # An unrevoked narrowed key would widen to its full role without the column.
    with pytest.raises(RuntimeError, match=r"1 unrevoked platform_api_key row\(s\) carry scopes"):
        _run(pg_schema, "downgrade")
    assert {"scopes", "surface"} <= set(_columns(pg_schema))

    with factory() as session:
        session.execute(update(PlatformAPIKey).values(revoked_at=datetime.now(UTC)))
        session.commit()
    _run(pg_schema, "downgrade")
    assert not {"scopes", "surface"} & set(_columns(pg_schema))
    _run(pg_schema, "upgrade")  # leave the schema whole for drop_all


def test_an_unscoped_key_stores_sql_null_on_postgres(pg_schema: Engine) -> None:
    """``None`` scopes are SQL NULL like every pre-0046 row, never a JSON ``null``."""
    factory = sessionmaker(bind=pg_schema, expire_on_commit=False, class_=Session)
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        session.add_all([_key(ws.id, None), _key(ws.id, ["read"])])
        session.commit()
        rows = session.execute(
            text(
                "SELECT scopes IS NULL, jsonb_typeof(scopes) FROM platform_api_key"
                " WHERE workspace_id = :ws"
            ),
            {"ws": ws.id},
        ).all()
    assert sorted(rows) == [(False, "array"), (True, None)]
