"""F40-OBS-ANALYTICS: DORA metrics (deploy freq, lead time, CFR, MTTR)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import Project, Workspace
from forge_db.models.deployment import (
    Deployment,
    Environment,
    EnvironmentPipeline,
)
from forge_obs.analytics.dora import SqlDoraReader, compute_dora_metrics

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@dataclass
class _Deployment:
    environment_name: str
    state: str
    kind: str
    requested_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


def test_deploy_frequency_and_lead_time_over_successful_deploys() -> None:
    deployments = [
        _Deployment("production", "succeeded", "promotion", NOW, NOW, NOW + timedelta(minutes=10)),
        _Deployment(
            "production",
            "succeeded",
            "promotion",
            NOW + timedelta(days=1),
            NOW + timedelta(days=1),
            NOW + timedelta(days=1, minutes=20),
        ),
    ]
    metrics = compute_dora_metrics(deployments, window_days=2)
    assert metrics.deployment_count == 2
    assert metrics.successful_count == 2
    assert metrics.deploy_frequency_per_day == pytest.approx(1.0)
    assert metrics.lead_time_seconds == pytest.approx((600 + 1200) / 2)
    assert metrics.change_failure_rate == pytest.approx(0.0)


def test_change_failure_rate_counts_failed_gate_rejected_and_rolled_back() -> None:
    deployments = [
        _Deployment("production", "succeeded", "promotion", NOW, NOW, NOW),
        _Deployment("production", "failed", "promotion", NOW, NOW, NOW),
        _Deployment("production", "gate_rejected", "promotion", NOW, NOW, NOW),
        _Deployment("production", "rolled_back", "promotion", NOW, NOW, NOW),
        # Rollback deployments themselves are excluded from the ratio.
        _Deployment("production", "succeeded", "rollback", NOW, NOW, NOW),
    ]
    metrics = compute_dora_metrics(deployments, window_days=1)
    assert metrics.deployment_count == 4
    assert metrics.change_failure_rate == pytest.approx(3 / 4)


def test_mttr_is_time_from_failure_to_next_success_in_same_environment() -> None:
    deployments = [
        _Deployment("production", "failed", "promotion", NOW, NOW, NOW),
        _Deployment("production", "succeeded", "promotion", NOW, NOW, NOW + timedelta(minutes=30)),
        # A different environment's failure has no bearing on production's MTTR.
        _Deployment("staging", "failed", "promotion", NOW, NOW, NOW),
    ]
    metrics = compute_dora_metrics(deployments, window_days=1)
    assert metrics.mttr_seconds == pytest.approx(1800)


def test_zero_window_days_yields_zero_frequency_not_zero_division() -> None:
    metrics = compute_dora_metrics([], window_days=0)
    assert metrics.deploy_frequency_per_day == 0.0


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.mark.usefixtures("pg_engine")
def test_sql_reader_scopes_by_workspace_and_window(factory) -> None:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        project = Project(workspace_id=ws.id, name="Forge", key=f"FRG{uuid.uuid4().hex[:4]}")
        session.add(project)
        session.flush()
        pipeline = EnvironmentPipeline(
            workspace_id=ws.id, project_id=project.id, repo_id="org/repo"
        )
        session.add(pipeline)
        session.flush()
        env = Environment(workspace_id=ws.id, pipeline_id=pipeline.id, name="production", rank=0)
        session.add(env)
        session.flush()

        deploy = Deployment(
            workspace_id=ws.id,
            project_id=project.id,
            pipeline_id=pipeline.id,
            environment_id=env.id,
            environment_name="production",
            repo_id="org/repo",
            commit_sha="a" * 40,
            state="succeeded",
            initiated_by="agent",
            requested_at=NOW,
            started_at=NOW,
            finished_at=NOW + timedelta(minutes=5),
        )
        session.add(deploy)
        session.commit()
        ws_id, project_id = ws.id, project.id

    reader = SqlDoraReader(factory)
    metrics = reader.dora_metrics(
        workspace_id=ws_id,
        project_id=project_id,
        frm=NOW - timedelta(hours=1),
        to=NOW + timedelta(hours=1),
    )
    assert metrics.deployment_count == 1
    assert metrics.successful_count == 1
    assert metrics.lead_time_seconds == pytest.approx(300)
