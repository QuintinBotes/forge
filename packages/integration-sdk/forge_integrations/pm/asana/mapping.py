"""Asana status/priority default maps.

Asana has no built-in "status" field: a task's board position is its
membership in a project *section* (the columns on an Asana board). Section
names are workspace-defined, so — exactly like Jira status names — the default
table is a best-effort table of common section names and the adapter's
:func:`~forge_integrations.pm.base.resolve_value` (case-insensitive, override-
first, never-silently-drop) does the rest.

Priority is likewise not a native Asana field; it is conventionally a custom
enum field named "Priority" (configurable via
``AdapterContext.config["priority_field_name"]``, default ``"Priority"``).
"""

from __future__ import annotations

# forge StatusCategory -> Asana section name (OUT) — the canonical name the
# adapter will create/look up on the project's sections.
STATUS_OUT: dict[str, str] = {
    "backlog": "Backlog",
    "unstarted": "To Do",
    "started": "In Progress",
    "completed": "Done",
    "canceled": "Cancelled",
}

# Asana section name (case-insensitive) -> forge StatusCategory (IN).
STATUS_IN: dict[str, str] = {
    "backlog": "backlog",
    "to do": "unstarted",
    "todo": "unstarted",
    "not started": "unstarted",
    "in progress": "started",
    "doing": "started",
    "in review": "started",
    "review": "started",
    "done": "completed",
    "complete": "completed",
    "completed": "completed",
    "cancelled": "canceled",
    "canceled": "canceled",
}

DEFAULT_PRIORITY_FIELD_NAME = "Priority"

# forge ForgePriority -> Asana "Priority" enum option name (OUT).
PRIORITY_OUT: dict[str, str] = {
    "none": "Medium",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "urgent": "Urgent",
}

# Asana "Priority" enum option name -> forge ForgePriority (IN).
PRIORITY_IN: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "urgent": "urgent",
}


__all__ = [
    "DEFAULT_PRIORITY_FIELD_NAME",
    "PRIORITY_IN",
    "PRIORITY_OUT",
    "STATUS_IN",
    "STATUS_OUT",
]
