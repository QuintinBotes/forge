"""Red-Team Gate persistence: the append-only ``red_team_record`` store.

Exposes an insert-only :class:`RedTeamRepository` and :func:`record_red_team_verdict`,
which inserts the verdict and — on a ``survived`` outcome — emits the chained
``redteam.survived`` audit event that feeds the Phase-1 attestation.
"""

from __future__ import annotations

from forge_db.redteam.repository import (
    REDTEAM_SURVIVED_ACTION,
    RedTeamRepository,
    record_red_team_verdict,
)

__all__ = [
    "REDTEAM_SURVIVED_ACTION",
    "RedTeamRepository",
    "record_red_team_verdict",
]
