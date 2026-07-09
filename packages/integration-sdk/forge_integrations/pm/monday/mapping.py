"""Monday.com status/priority default maps.

A monday.com board's Kanban column is a "status" column (type ``color``) whose
current value is a free-text label (board-defined, like Jira status names and
Asana section names). The column id is configurable via
``AdapterContext.config["status_column_id"]`` (default ``"status"``); priority
likewise lives in a "priority" column (``AdapterContext.config
["priority_column_id"]``, default ``"priority"``).
"""

from __future__ import annotations

DEFAULT_STATUS_COLUMN_ID = "status"
DEFAULT_PRIORITY_COLUMN_ID = "priority"

# forge StatusCategory -> monday status-column label (OUT).
STATUS_OUT: dict[str, str] = {
    "backlog": "Backlog",
    "unstarted": "Not Started",
    "started": "Working on it",
    "completed": "Done",
    "canceled": "Cancelled",
}

# monday status-column label (case-insensitive) -> forge StatusCategory (IN).
STATUS_IN: dict[str, str] = {
    "backlog": "backlog",
    "not started": "unstarted",
    "": "unstarted",
    "working on it": "started",
    "stuck": "started",
    "in progress": "started",
    "done": "completed",
    "cancelled": "canceled",
    "canceled": "canceled",
}

# forge ForgePriority -> monday priority-column label (OUT).
PRIORITY_OUT: dict[str, str] = {
    "none": "Medium",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "urgent": "Critical",
}

# monday priority-column label -> forge ForgePriority (IN).
PRIORITY_IN: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "urgent",
}


__all__ = [
    "DEFAULT_PRIORITY_COLUMN_ID",
    "DEFAULT_STATUS_COLUMN_ID",
    "PRIORITY_IN",
    "PRIORITY_OUT",
    "STATUS_IN",
    "STATUS_OUT",
]
