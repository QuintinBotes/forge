"""Recovery monitoring (F17) — deterministic V1, no network.

``ThresholdRecoveryMonitor`` implements the :class:`RecoveryMonitor` Protocol
over a scripted set of healthy/degraded signal predicates so recovery decisions
are deterministic in tests (and offline builds). The LLM/telemetry-backed monitor
plugs in behind the same Protocol later.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from forge_contracts.incident import ImpactAssessment, RecoveryStatus

__all__ = ["ThresholdRecoveryMonitor"]


class ThresholdRecoveryMonitor:
    """A deterministic :class:`RecoveryMonitor`.

    Configured with a sequence of :class:`RecoveryStatus` readings consumed one
    per ``check_recovery`` call (the last reading repeats once exhausted), or a
    single fixed reading.
    """

    def __init__(
        self,
        readings: Sequence[RecoveryStatus] | RecoveryStatus | None = None,
    ) -> None:
        if readings is None:
            self._readings: list[RecoveryStatus] = [RecoveryStatus(recovered=True)]
        elif isinstance(readings, RecoveryStatus):
            self._readings = [readings]
        else:
            self._readings = list(readings) or [RecoveryStatus(recovered=True)]
        self._index = 0

    async def check_recovery(
        self, *, incident_id: uuid.UUID, assessment: ImpactAssessment
    ) -> RecoveryStatus:
        reading = self._readings[min(self._index, len(self._readings) - 1)]
        self._index += 1
        return reading
