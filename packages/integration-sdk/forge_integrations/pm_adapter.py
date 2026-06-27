"""External project-management adapter surface (plan Task 1.13 / spec contract).

``BasePMAdapter`` implements the frozen ``forge_contracts.PMAdapter`` Protocol
with sensible default status/priority/field mappings and is meant to be
subclassed for a concrete system (Jira, Linear, ... — V2). ``GenericPMAdapter``
is the directly-usable identity-style adapter used in tests and examples.
"""

from __future__ import annotations

from datetime import UTC, datetime

from forge_contracts import (
    Direction,
    ExternalTask,
    ForgeTask,
    HealthResult,
    Priority,
    TaskDTO,
    TaskStatus,
    WebhookEvent,
)

# external (lower-cased) -> forge TaskStatus value
_STATUS_IN: dict[str, str] = {
    "backlog": "backlog",
    "todo": "backlog",
    "to do": "backlog",
    "open": "backlog",
    "new": "backlog",
    "ready": "ready",
    "ready for dev": "ready",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "doing": "in_progress",
    "started": "in_progress",
    "wip": "in_progress",
    "in review": "in_review",
    "in_review": "in_review",
    "review": "in_review",
    "code review": "in_review",
    "blocked": "blocked",
    "on hold": "blocked",
    "done": "done",
    "closed": "done",
    "complete": "done",
    "completed": "done",
    "resolved": "done",
    "merged": "done",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "wont do": "cancelled",
    "won't do": "cancelled",
    "duplicate": "cancelled",
}

# forge TaskStatus value -> external display vocabulary
_STATUS_OUT: dict[str, str] = {
    "backlog": "To Do",
    "ready": "To Do",
    "ready_for_agent": "To Do",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "blocked": "Blocked",
    "done": "Done",
    "cancelled": "Cancelled",
}

_PRIORITY_IN: dict[str, str] = {
    "lowest": "low",
    "low": "low",
    "minor": "low",
    "trivial": "low",
    "medium": "medium",
    "normal": "medium",
    "major": "medium",
    "default": "medium",
    "high": "high",
    "important": "high",
    "urgent": "urgent",
    "critical": "urgent",
    "highest": "urgent",
    "blocker": "urgent",
}

_PRIORITY_OUT: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "urgent": "Urgent",
}

# external field name -> forge field name
_FIELD_MAP: dict[str, str] = {
    "summary": "title",
    "assignee": "assignee_name",
    "description": "description",
    "status": "status",
    "priority": "priority",
}


class BasePMAdapter:
    """Reusable base implementing the ``PMAdapter`` Protocol surface."""

    system: str = "generic"
    status_in: dict[str, str] = _STATUS_IN
    status_out: dict[str, str] = _STATUS_OUT
    priority_in: dict[str, str] = _PRIORITY_IN
    priority_out: dict[str, str] = _PRIORITY_OUT
    field_map: dict[str, str] = _FIELD_MAP
    default_forge_status: str = TaskStatus.BACKLOG.value
    default_external_status: str = "To Do"
    default_forge_priority: str = Priority.MEDIUM.value
    default_external_priority: str = "Medium"

    def __init__(self) -> None:
        self._subscriptions: list[WebhookEvent] = []

    # ------------------------------------------------------------------ #
    # mapping                                                            #
    # ------------------------------------------------------------------ #

    def map_status(self, external: str, direction: Direction) -> str:
        key = (external or "").lower().strip()
        if direction == Direction.IN:
            return self.status_in.get(key, self.default_forge_status)
        return self.status_out.get(key, self.default_external_status)

    def map_priority(self, external: str, direction: Direction) -> str:
        key = (external or "").lower().strip()
        if direction == Direction.IN:
            return self.priority_in.get(key, self.default_forge_priority)
        return self.priority_out.get(key, self.default_external_priority)

    def map_fields(
        self, external: dict[str, object], direction: Direction
    ) -> dict[str, object]:
        if direction == Direction.IN:
            mapping = self.field_map
        else:
            mapping = {v: k for k, v in self.field_map.items()}
        return {mapping.get(key, key): value for key, value in external.items()}

    # ------------------------------------------------------------------ #
    # sync                                                               #
    # ------------------------------------------------------------------ #

    def sync_in(self, external_task: ExternalTask) -> ForgeTask:
        status = (
            TaskStatus(self.map_status(external_task.status, Direction.IN))
            if external_task.status
            else TaskStatus.BACKLOG
        )
        priority = (
            Priority(self.map_priority(external_task.priority, Direction.IN))
            if external_task.priority
            else Priority.MEDIUM
        )
        mapped = self.map_fields(external_task.fields, Direction.IN)
        description = mapped.get("description")
        return TaskDTO(
            key=external_task.external_id,
            title=external_task.title or external_task.external_id,
            status=status,
            priority=priority,
            description=description if isinstance(description, str) else None,
        )

    def sync_out(self, forge_task: ForgeTask) -> ExternalTask:
        external_id = forge_task.key or (
            str(forge_task.id) if forge_task.id is not None else ""
        )
        return ExternalTask(
            external_id=external_id,
            system=self.system,
            title=forge_task.title,
            status=self.map_status(forge_task.status.value, Direction.OUT),
            priority=self.map_priority(forge_task.priority.value, Direction.OUT),
        )

    # ------------------------------------------------------------------ #
    # webhooks + health                                                  #
    # ------------------------------------------------------------------ #

    def subscribe(self, webhook_event: WebhookEvent) -> None:
        self._subscriptions.append(webhook_event)

    @property
    def subscriptions(self) -> list[WebhookEvent]:
        return list(self._subscriptions)

    def get_connection_health(self) -> HealthResult:
        return HealthResult(healthy=True, status="ok", checked_at=datetime.now(UTC))


class GenericPMAdapter(BasePMAdapter):
    """A ready-to-use generic adapter (the default ``PMAdapter`` implementation)."""

    system = "generic"


__all__ = ["BasePMAdapter", "GenericPMAdapter"]
