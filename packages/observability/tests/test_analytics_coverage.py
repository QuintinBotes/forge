"""F40-OBS-ANALYTICS: coverage-over-time snapshot, recompute, and org rollup."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import Project, Workspace
from forge_obs.analytics.coverage import SqlCoverageRepository, compute_coverage_pct


def test_compute_coverage_pct_handles_zero_total() -> None:
    assert compute_coverage_pct(0, 0) == 0.0
    assert compute_coverage_pct(80, 100) == 80.0
    assert compute_coverage_pct(1, 3) == pytest.approx(33.33)


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def seeded(factory) -> dict:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        p1 = Project(workspace_id=ws.id, name="Forge", key=f"FRG{uuid.uuid4().hex[:4]}")
        p2 = Project(workspace_id=ws.id, name="Web", key=f"WEB{uuid.uuid4().hex[:4]}")
        session.add_all([p1, p2])
        session.commit()
        return {"ws": ws.id, "p1": p1.id, "p2": p2.id}


@pytest.mark.usefixtures("pg_engine")
def test_recompute_is_an_idempotent_daily_upsert(factory, seeded) -> None:
    repo = SqlCoverageRepository(factory)
    day = date(2026, 7, 1)
    first = repo.recompute(
        workspace_id=seeded["ws"],
        project_id=seeded["p1"],
        repo_id="org/forge",
        snapshot_date=day,
        lines_covered=80,
        lines_total=100,
    )
    assert first.coverage_pct == 80.0

    # CI reruns the same day with a corrected number -> same row, not a new one.
    corrected = repo.recompute(
        workspace_id=seeded["ws"],
        project_id=seeded["p1"],
        repo_id="org/forge",
        snapshot_date=day,
        lines_covered=85,
        lines_total=100,
    )
    assert corrected.id == first.id
    assert corrected.coverage_pct == 85.0

    trend = repo.trend(project_id=seeded["p1"], repo_id="org/forge")
    assert len(trend) == 1
    assert trend[0].coverage_pct == 85.0


@pytest.mark.usefixtures("pg_engine")
def test_trend_orders_by_date_and_org_rollup_weights_by_lines(factory, seeded) -> None:
    repo = SqlCoverageRepository(factory)
    day1, day2 = date(2026, 7, 1), date(2026, 7, 2)
    repo.recompute(
        workspace_id=seeded["ws"],
        project_id=seeded["p1"],
        repo_id="org/forge",
        snapshot_date=day2,
        lines_covered=90,
        lines_total=100,
    )
    repo.recompute(
        workspace_id=seeded["ws"],
        project_id=seeded["p1"],
        repo_id="org/forge",
        snapshot_date=day1,
        lines_covered=50,
        lines_total=100,
    )
    # A second, larger repo on the same day pulls the weighted rollup down.
    repo.recompute(
        workspace_id=seeded["ws"],
        project_id=seeded["p2"],
        repo_id="org/web",
        snapshot_date=day1,
        lines_covered=100,
        lines_total=1000,
    )

    trend = repo.trend(project_id=seeded["p1"], repo_id="org/forge")
    assert [t.snapshot_date for t in trend] == [day1, day2]

    rollup = repo.org_rollup(workspace_id=seeded["ws"])
    by_day = {point.snapshot_date: point for point in rollup}
    # day1: (50 + 100) covered / (100 + 1000) total = 13.64%
    assert by_day[day1].coverage_pct == pytest.approx(13.64)
    assert by_day[day2].coverage_pct == pytest.approx(90.0)
