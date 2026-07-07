"""Tests for alert normalization, recovery monitor, and action-item tasks (F17)."""

from __future__ import annotations

import asyncio
import uuid

from forge_board import InMemoryBoardService
from forge_board.incidents import (
    AlertNormalizer,
    ThresholdRecoveryMonitor,
    create_action_item_tasks,
    derive_dedup_key,
)
from forge_contracts import TaskKind
from forge_contracts.enums import IncidentSeverity
from forge_contracts.incident import (
    ActionItem,
    AlertProvider,
    ImpactAssessment,
    IncidentAlert,
    RecoveryStatus,
)


def test_derive_dedup_key_uses_explicit_key() -> None:
    alert = IncidentAlert(provider=AlertProvider.DATADOG, dedup_key="svc-x:cpu", title="CPU")
    assert derive_dedup_key(alert) == "svc-x:cpu"


def test_derive_dedup_key_is_deterministic_when_absent() -> None:
    a1 = IncidentAlert(provider=AlertProvider.SENTRY, dedup_key="", title="boom", service="api")
    a2 = IncidentAlert(provider=AlertProvider.SENTRY, dedup_key="", title="boom", service="api")
    assert derive_dedup_key(a1) == derive_dedup_key(a2)
    assert derive_dedup_key(a1).startswith("sentry:")


def test_normalizer_fills_dedup_and_severity() -> None:
    alert = IncidentAlert(provider=AlertProvider.GRAFANA, dedup_key="", title="x")
    out = AlertNormalizer().normalize(alert)
    assert out.dedup_key
    assert out.severity is IncidentSeverity.MEDIUM


def test_threshold_recovery_monitor_sequence() -> None:
    monitor = ThresholdRecoveryMonitor(
        [RecoveryStatus(recovered=False, degraded_signals=["p99"]), RecoveryStatus(recovered=True)]
    )
    inc = uuid.uuid4()
    assessment = ImpactAssessment()
    first = asyncio.run(monitor.check_recovery(incident_id=inc, assessment=assessment))
    second = asyncio.run(monitor.check_recovery(incident_id=inc, assessment=assessment))
    assert first.recovered is False
    assert second.recovered is True


def test_create_action_item_tasks_on_board() -> None:
    board = InMemoryBoardService()
    project_id = uuid.uuid4()
    items = [
        ActionItem(title="Fix root cause", kind="bug", priority="high"),
        ActionItem(title="Add alerting", kind="chore", priority="medium"),
    ]
    tasks = create_action_item_tasks(
        board, project_id=project_id, action_items=items, incident_key="CORE-INC1"
    )
    assert len(tasks) == 2
    assert tasks[0].kind is TaskKind.BUG
    assert tasks[1].kind is TaskKind.CHORE
    assert all(t.id is not None and t.key for t in tasks)
    assert "incident:CORE-INC1" in tasks[0].labels
    # Tasks really landed on the board.
    listed = board.list_tasks()
    assert len(listed) == 2
