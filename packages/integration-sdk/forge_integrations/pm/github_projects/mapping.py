"""GitHub Projects v2 status/priority default maps.

A Projects v2 board's Kanban column is a single-select field — conventionally
named "Status" (field name configurable via
``AdapterContext.config["status_field_name"]``) with the default template's
``Todo`` / ``In Progress`` / ``Done`` options. Priority is a second,
opt-in single-select field (default name ``"Priority"``,
``AdapterContext.config["priority_field_name"]``); boards without one simply
never resolve a priority (tolerated — see the adapter).
"""

from __future__ import annotations

DEFAULT_STATUS_FIELD_NAME = "Status"
DEFAULT_PRIORITY_FIELD_NAME = "Priority"

# forge StatusCategory -> GitHub Projects v2 "Status" option name (OUT).
STATUS_OUT: dict[str, str] = {
    "backlog": "Backlog",
    "unstarted": "Todo",
    "started": "In Progress",
    "completed": "Done",
    "canceled": "Cancelled",
}

# GitHub Projects v2 "Status" option name (case-insensitive) -> forge StatusCategory (IN).
STATUS_IN: dict[str, str] = {
    "backlog": "backlog",
    "todo": "unstarted",
    "to do": "unstarted",
    "in progress": "started",
    "done": "completed",
    "cancelled": "canceled",
    "canceled": "canceled",
}

# forge ForgePriority -> GitHub Projects v2 "Priority" option name (OUT).
PRIORITY_OUT: dict[str, str] = {
    "none": "Medium",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "urgent": "Urgent",
}

# GitHub Projects v2 "Priority" option name -> forge ForgePriority (IN).
PRIORITY_IN: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "urgent": "urgent",
}


__all__ = [
    "DEFAULT_PRIORITY_FIELD_NAME",
    "DEFAULT_STATUS_FIELD_NAME",
    "PRIORITY_IN",
    "PRIORITY_OUT",
    "STATUS_IN",
    "STATUS_OUT",
]
