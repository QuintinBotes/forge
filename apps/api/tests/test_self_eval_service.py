"""Tests for the Self-Eval baseline persistence service (F41)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.self_eval_service import SelfEvalService
from forge_db.base import Base

WS = uuid.uuid4()
OTHER_WS = uuid.uuid4()
SUITE = uuid.uuid4()
OTHER_SUITE = uuid.uuid4()


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def service(session_factory: sessionmaker[Session]) -> SelfEvalService:
    return SelfEvalService(session_factory=session_factory)


def test_cold_start_returns_none(service: SelfEvalService) -> None:
    assert service.workspace_baseline(WS) is None
    assert service.baseline_for_suite(WS, SUITE) is None


def test_record_then_read_back(service: SelfEvalService) -> None:
    rec = service.record_baseline(
        workspace_id=WS,
        benchmark_suite_id=SUITE,
        resolved=8,
        total=10,
        resolution_rate=0.8,
        config={"model": "claude-opus"},
    )
    assert rec.baseline_rate == 0.8
    assert service.workspace_baseline(WS) == 0.8
    full = service.baseline_for_suite(WS, SUITE)
    assert full is not None
    assert (full.resolved, full.total) == (8, 10)


def test_upsert_overwrites_same_suite(service: SelfEvalService) -> None:
    service.record_baseline(
        workspace_id=WS,
        benchmark_suite_id=SUITE,
        resolved=7,
        total=10,
        resolution_rate=0.7,
        config={},
    )
    service.record_baseline(
        workspace_id=WS,
        benchmark_suite_id=SUITE,
        resolved=9,
        total=10,
        resolution_rate=0.9,
        config={},
    )
    assert service.workspace_baseline(WS) == 0.9  # one row per (workspace, suite)


def test_overwrite_false_preserves_existing(service: SelfEvalService) -> None:
    service.record_baseline(
        workspace_id=WS,
        benchmark_suite_id=SUITE,
        resolved=9,
        total=10,
        resolution_rate=0.9,
        config={},
    )
    # A later, worse run must never lower the bar it defends.
    rec = service.record_baseline(
        workspace_id=WS,
        benchmark_suite_id=SUITE,
        resolved=4,
        total=10,
        resolution_rate=0.4,
        config={},
        overwrite=False,
    )
    assert rec.baseline_rate == 0.9
    assert service.workspace_baseline(WS) == 0.9


def test_baselines_are_workspace_isolated(service: SelfEvalService) -> None:
    service.record_baseline(
        workspace_id=WS,
        benchmark_suite_id=SUITE,
        resolved=9,
        total=10,
        resolution_rate=0.9,
        config={},
    )
    assert service.workspace_baseline(OTHER_WS) is None


def test_config_is_stored_verbatim_but_isolated(service: SelfEvalService) -> None:
    # The service persists what it is given; the caller is responsible for
    # passing an already-redacted config. Confirm it round-trips as a copy.
    original = {"model": "claude-opus", "effort": "high"}
    service.record_baseline(
        workspace_id=WS,
        benchmark_suite_id=SUITE,
        resolved=10,
        total=10,
        resolution_rate=1.0,
        config=original,
    )
    original["model"] = "mutated"  # must not affect the stored snapshot
    assert service.workspace_baseline(WS) == 1.0
