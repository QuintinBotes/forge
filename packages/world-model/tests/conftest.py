"""Shared fixtures for the forge_world tests (pure + SQLite + fake model)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

import forge_db.models  # noqa: F401  (registers all models)
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent
from forge_db.base import Base
from forge_db.models import Workspace
from forge_world import (
    FactData,
    InvalidatedReason,
    Layer,
    Mood,
    Precision,
    SourceKind,
    WorldModelService,
    WorldRepository,
)

WS = uuid.UUID("00000000-0000-0000-0000-00000000aa01")
WS2 = uuid.UUID("00000000-0000-0000-0000-00000000aa02")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """A manually advanced clock for deterministic record times."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def tick(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


class FakeModelClient:
    """A scripted forge_contracts.ModelClient (contents or exceptions)."""

    def __init__(self, script: list[str | Exception] | None = None) -> None:
        self._script = list(script or [])
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return ModelResponse(content=item)
        return ModelResponse(content='{"statements": []}')

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content)


def make_fact(
    *,
    subject: uuid.UUID,
    predicate: str | None = "based_in",
    phrase: str = "based in",
    object_id: uuid.UUID | None = None,
    value: str | None = "Berlin",
    valid_from: datetime | None = None,
    valid_from_precision: Precision = Precision.DAY,
    valid_to: datetime | None = None,
    valid_to_precision: Precision = Precision.DAY,
    end_derived: bool = False,
    end_after_period: bool = False,
    layer: Layer = Layer.TYPED,
    mood: Mood | None = None,
    confidence: float = 1.0,
    recorded_at: datetime = T0,
    invalidated_at: datetime | None = None,
    invalidated_reason: InvalidatedReason | None = None,
    fact_id: uuid.UUID | None = None,
    qualifiers: Mapping[str, Any] | None = None,
) -> FactData:
    """Build a FactData for pure-module tests (no database involved)."""
    return FactData(
        id=fact_id or uuid.uuid4(),
        subject_id=subject,
        phrase=phrase,
        predicate_key=predicate,
        object_id=object_id,
        value=value,
        layer=layer,
        mood=mood,
        qualifiers=dict(qualifiers or {}),
        confidence=confidence,
        valid_from=valid_from,
        valid_from_precision=(
            valid_from_precision if valid_from is not None else Precision.UNKNOWN
        ),
        valid_to=valid_to,
        valid_to_precision=valid_to_precision if valid_to is not None else Precision.UNKNOWN,
        end_derived=end_derived,
        end_after_period=end_after_period,
        source_kind=SourceKind.PERSON,
        actor="tester",
        proposal_id=None,
        supersedes_id=None,
        dedup_key=uuid.uuid4().hex,
        recorded_at=recorded_at,
        invalidated_at=invalidated_at,
        invalidated_reason=invalidated_reason,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add(Workspace(id=WS, name="Acme WS", slug="acme-ws"))
        session.add(Workspace(id=WS2, name="Other WS", slug="other-ws"))
        session.commit()
    yield sf
    engine.dispose()


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


@pytest.fixture
def service(session: Session, clock: FakeClock) -> WorldModelService:
    return WorldModelService(WorldRepository(session), clock=clock)
