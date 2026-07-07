"""Postgres integration tests for the F32 marketplace models (AC1/AC18).

Exercises the real Postgres code paths the SQLite unit tests cannot: the
GIN(tags) + full-text catalog-search indexes, and the F39 immutability trigger
that blocks UPDATE/DELETE on the append-only ``marketplace_audit_log`` table.
Uses the shared ``pg_engine`` fixture; skips (parked) without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import Workspace
from forge_db.models.marketplace import (
    MarketplaceAuditLog,
    MarketplaceListing,
    MarketplaceRegistry,
)

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _ws(session: Session) -> uuid.UUID:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    return ws.id


def test_catalog_search_indexes_exist(factory: sessionmaker[Session], pg_engine) -> None:
    """AC1: the GIN(tags) + full-text indexes are created on Postgres."""
    index_names = {ix["name"] for ix in inspect(pg_engine).get_indexes("marketplace_listing")}
    assert "ix_marketplace_listing_tags_gin" in index_names
    assert "ix_marketplace_listing_fts" in index_names


def test_registry_and_listing_roundtrip(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id = _ws(session)
        reg = MarketplaceRegistry(
            workspace_id=ws_id,
            slug="official",
            name="Official",
            type="http_index",
            url="https://official/index.json",
            trust_level="official",
            enabled=True,
        )
        session.add(reg)
        session.flush()
        listing = MarketplaceListing(
            workspace_id=ws_id,
            registry_id=reg.id,
            kind="skill_profile",
            slug="backend-tdd",
            name="Backend TDD",
            summary="hardened tdd profile",
            tags=["backend", "tdd"],
            latest_version="1.2.0",
            cached_at=datetime.now(UTC),
        )
        session.add(listing)
        session.commit()
        assert session.get(MarketplaceListing, listing.id).tags == ["backend", "tdd"]


def test_audit_log_is_immutable(factory: sessionmaker[Session]) -> None:
    """AC18: UPDATE/DELETE on marketplace_audit_log raises (trigger)."""
    with factory() as session:
        ws_id = _ws(session)
        row = MarketplaceAuditLog(
            workspace_id=ws_id, actor="user:x", operation="install", result_status="ok"
        )
        session.add(row)
        session.commit()
        row_id = row.id

    with factory() as session, pytest.raises((ProgrammingError, IntegrityError)):
        session.execute(
            update(MarketplaceAuditLog)
            .where(MarketplaceAuditLog.id == row_id)
            .values(result_status="tampered")
        )
        session.commit()

    with factory() as session, pytest.raises((ProgrammingError, IntegrityError)):
        session.execute(text("DELETE FROM marketplace_audit_log WHERE id = :i"), {"i": str(row_id)})
        session.commit()
