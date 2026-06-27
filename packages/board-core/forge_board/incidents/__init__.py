"""Incident domain (F17): alert ingest/dedup, runbook policy, recovery, postmortem.

This subpackage extends the board domain with the incident-specific pieces of the
F17 slice. It is deliberately storage-light: the deterministic V1 helpers
(runbook blast-radius policy, alert normalization, threshold recovery monitor,
template postmortem composer, follow-up task generation) are pure/in-memory so
the unit suite runs without Postgres, mirroring the rest of board-core.
"""

from __future__ import annotations

from forge_board.incidents.actions import create_action_item_tasks
from forge_board.incidents.alert import AlertNormalizer, derive_dedup_key
from forge_board.incidents.errors import (
    BlastRadiusExceeded,
    DuplicateAlert,
    IncidentError,
    IncidentNotFound,
    RunbookStepError,
)
from forge_board.incidents.postmortem import (
    TemplatePostmortemComposer,
    content_hash,
    render_postmortem_md,
)
from forge_board.incidents.recovery import ThresholdRecoveryMonitor
from forge_board.incidents.runbook import (
    BLAST_ORDER,
    assert_runbook_within_policy,
    runbook_max_blast_radius,
)

__all__ = [
    "BLAST_ORDER",
    "AlertNormalizer",
    "BlastRadiusExceeded",
    "DuplicateAlert",
    "IncidentError",
    "IncidentNotFound",
    "RunbookStepError",
    "TemplatePostmortemComposer",
    "ThresholdRecoveryMonitor",
    "assert_runbook_within_policy",
    "content_hash",
    "create_action_item_tasks",
    "derive_dedup_key",
    "render_postmortem_md",
    "runbook_max_blast_radius",
]
