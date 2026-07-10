"""F40-OBS-ANALYTICS: MTTA/MTTR/remediation-accept-rate aggregation."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import Incident, Project, Workspace
from forge_db.models.incidents import RemediationPlan
from forge_obs.analytics.incidents import (
    SqlIncidentReliabilityReader,
    compute_incident_reliability,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@dataclass
class _Incident:
    detected_at: datetime | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None


@dataclass
class _Plan:
    status: str


def test_mtta_mttr_average_only_over_complete_pairs() -> None:
    incidents = [
        _Incident(NOW, NOW + timedelta(minutes=5), NOW + timedelta(hours=1)),
        _Incident(NOW, NOW + timedelta(minutes=15), NOW + timedelta(hours=3)),
        # Still open: contributes to sample_size but not mttr.
        _Incident(NOW, NOW + timedelta(minutes=10), None),
    ]
    metrics = compute_incident_reliability(incidents)
    assert metrics.sample_size == 3
    assert metrics.mtta_seconds == pytest.approx((5 * 60 + 15 * 60 + 10 * 60) / 3)
    assert metrics.mttr_seconds == pytest.approx((3600 + 3 * 3600) / 2)


def test_remediation_accept_rate_excludes_undecided_plans() -> None:
    plans = [_Plan("approved"), _Plan("approved"), _Plan("rejected"), _Plan("proposed")]
    metrics = compute_incident_reliability([], plans)
    assert metrics.remediation_total == 3
    assert metrics.remediation_accepted == 2
    assert metrics.remediation_accept_rate == pytest.approx(2 / 3)


def test_empty_input_yields_none_rates_not_crash() -> None:
    metrics = compute_incident_reliability([])
    assert metrics.sample_size == 0
    assert metrics.mtta_seconds is None
    assert metrics.mttr_seconds is None
    assert metrics.remediation_accept_rate is None


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.mark.usefixtures("pg_engine")
def test_sql_reader_scopes_by_workspace_and_detected_window(factory) -> None:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        other_ws = Workspace(name="Rival", slug=f"rival-{uuid.uuid4().hex[:8]}")
        session.add_all([ws, other_ws])
        session.flush()
        project = Project(workspace_id=ws.id, name="Forge", key=f"FRG{uuid.uuid4().hex[:4]}")
        session.add(project)
        session.flush()

        in_scope = Incident(
            workspace_id=ws.id,
            project_id=project.id,
            key="INC-1",
            title="db down",
            detected_at=NOW,
            acknowledged_at=NOW + timedelta(minutes=10),
            resolved_at=NOW + timedelta(hours=2),
        )
        out_of_window = Incident(
            workspace_id=ws.id,
            project_id=project.id,
            key="INC-2",
            title="old one",
            detected_at=NOW - timedelta(days=30),
            acknowledged_at=NOW - timedelta(days=30) + timedelta(minutes=5),
            resolved_at=NOW - timedelta(days=30) + timedelta(hours=1),
        )
        other_workspace_incident = Incident(
            workspace_id=other_ws.id,
            project_id=project.id,
            key="INC-3",
            title="not ours",
            detected_at=NOW,
        )
        session.add_all([in_scope, out_of_window, other_workspace_incident])
        session.flush()

        plan = RemediationPlan(
            workspace_id=ws.id,
            incident_id=in_scope.id,
            attempt=1,
            status="approved",
            steps=[],
        )
        session.add(plan)
        session.commit()
        ws_id, project_id = ws.id, project.id

    reader = SqlIncidentReliabilityReader(factory)
    metrics = reader.reliability(
        workspace_id=ws_id,
        project_id=project_id,
        frm=NOW - timedelta(days=1),
        to=NOW + timedelta(days=1),
    )
    assert metrics.sample_size == 1
    assert metrics.mtta_seconds == pytest.approx(600)
    assert metrics.mttr_seconds == pytest.approx(7200)
    assert metrics.remediation_total == 1
    assert metrics.remediation_accepted == 1
    assert metrics.remediation_accept_rate == pytest.approx(1.0)
