"""Postgres integration tests for the F17 incident models (AC18).

Exercises the real Postgres code paths the SQLite unit tests cannot: the partial
unique indexes ``uq_incident_open_dedup`` and ``uq_incident_alert_delivery``, plus
a full incident roundtrip. Uses the shared ``pg_engine`` fixture (root
conftest.py); skips with a parked reason when no Postgres is reachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    Incident,
    IncidentAlert,
    IncidentEvent,
    Postmortem,
    PostmortemActionItem,
    Project,
    RemediationPlan,
    Workspace,
)

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed_project(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"CORE{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    return ws.id, project.id


def test_incident_full_roundtrip(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id = _seed_project(session)
        incident = Incident(
            workspace_id=ws_id,
            project_id=project_id,
            key="CORE-INC1",
            title="Checkout latency",
            source="datadog",
            dedup_key="checkout:latency",
            lifecycle_state="context_gathering",
        )
        session.add(incident)
        session.flush()

        session.add_all(
            [
                IncidentAlert(
                    workspace_id=ws_id,
                    incident_id=incident.id,
                    provider="datadog",
                    delivery_id="dd-1",
                    dedup_key="checkout:latency",
                    severity="high",
                    title="latency",
                    payload_hash="abc",
                    status="created_incident",
                ),
                IncidentEvent(
                    workspace_id=ws_id,
                    incident_id=incident.id,
                    sequence=1,
                    kind="state_change",
                    actor="system",
                    summary="alert_received -> incident_created",
                ),
                RemediationPlan(
                    workspace_id=ws_id,
                    incident_id=incident.id,
                    attempt=1,
                    max_blast_radius="low",
                    status="proposed",
                    steps=[{"id": "s1", "action": "read_logs"}],
                ),
            ]
        )
        session.flush()
        pm = Postmortem(
            workspace_id=ws_id,
            incident_id=incident.id,
            content_md="# pm",
            content_hash="h",
            data={"root_cause": "x"},
        )
        session.add(pm)
        session.flush()
        session.add(
            PostmortemActionItem(
                workspace_id=ws_id,
                postmortem_id=pm.id,
                title="Fix it",
                kind="bug",
                priority="high",
            )
        )
        session.commit()

        assert session.get(Incident, incident.id) is not None
        assert session.query(IncidentEvent).count() == 1
        assert session.query(PostmortemActionItem).count() == 1


def test_open_dedup_partial_unique_index(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id = _seed_project(session)
        first = Incident(
            workspace_id=ws_id,
            project_id=project_id,
            key="CORE-INC1",
            title="A",
            dedup_key="dup",
            lifecycle_state="context_gathering",
        )
        session.add(first)
        session.commit()

        # A second OPEN incident with the same dedup key is rejected.
        with pytest.raises(IntegrityError):
            session.add(
                Incident(
                    workspace_id=ws_id,
                    project_id=project_id,
                    key="CORE-INC2",
                    title="B",
                    dedup_key="dup",
                    lifecycle_state="impact_assessed",
                )
            )
            session.commit()
        session.rollback()

    # Once the first is closed, a new open incident with the same key is allowed.
    with factory() as session:
        ws_id, project_id = _seed_project(session)
        closed = Incident(
            workspace_id=ws_id,
            project_id=project_id,
            key="CORE-INC1",
            title="A",
            dedup_key="dup2",
            lifecycle_state="closed",
        )
        session.add(closed)
        session.commit()
        session.add(
            Incident(
                workspace_id=ws_id,
                project_id=project_id,
                key="CORE-INC2",
                title="B",
                dedup_key="dup2",
                lifecycle_state="alert_received",
            )
        )
        session.commit()  # no error
        assert session.query(Incident).filter_by(dedup_key="dup2").count() == 2


def test_alert_delivery_partial_unique_index(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed_project(session)
        session.add(
            IncidentAlert(
                workspace_id=ws_id,
                provider="pagerduty",
                delivery_id="d1",
                dedup_key="k",
                severity="high",
                title="t",
            )
        )
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(
                IncidentAlert(
                    workspace_id=ws_id,
                    provider="pagerduty",
                    delivery_id="d1",
                    dedup_key="k2",
                    severity="high",
                    title="t2",
                )
            )
            session.commit()
        session.rollback()

    # A NULL delivery_id is exempt from the partial unique index.
    with factory() as session:
        ws_id, _ = _seed_project(session)
        for _ in range(2):
            session.add(
                IncidentAlert(
                    workspace_id=ws_id,
                    provider="manual",
                    delivery_id=None,
                    dedup_key="k",
                    severity="low",
                    title="t",
                )
            )
        session.commit()
        assert session.query(IncidentAlert).filter_by(provider="manual").count() == 2
