"""Incident domain exceptions (F17)."""

from __future__ import annotations


class IncidentError(Exception):
    """Base class for incident-domain errors."""


class IncidentNotFound(IncidentError):
    """Raised when an incident (or related record) cannot be found."""

    def __init__(self, identifier: object) -> None:
        self.identifier = identifier
        super().__init__(f"incident not found: {identifier}")


class DuplicateAlert(IncidentError):
    """Raised when an alert is a duplicate of one already processed.

    Carries the existing incident id so the caller can respond idempotently
    (HTTP 200, attached) rather than creating a second incident.
    """

    def __init__(self, dedup_key: str, incident_id: object | None = None) -> None:
        self.dedup_key = dedup_key
        self.incident_id = incident_id
        super().__init__(f"duplicate alert for dedup_key={dedup_key!r}")


class BlastRadiusExceeded(IncidentError):
    """Raised when a runbook proposal violates the incident-response posture."""

    def __init__(self, offending_step_ids: list[str]) -> None:
        self.offending_step_ids = offending_step_ids
        super().__init__(
            f"runbook violates blast-radius/forbidden-action policy: {offending_step_ids}"
        )


class RunbookStepError(IncidentError):
    """Raised when a runbook step fails (or is blocked) at execution time."""

    def __init__(self, step_id: str, reason: str) -> None:
        self.step_id = step_id
        self.reason = reason
        super().__init__(f"runbook step {step_id!r} failed: {reason}")


__all__ = [
    "BlastRadiusExceeded",
    "DuplicateAlert",
    "IncidentError",
    "IncidentNotFound",
    "RunbookStepError",
]
