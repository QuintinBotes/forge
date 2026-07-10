"""ClickUp status/priority default maps.

A ClickUp list's statuses are list-defined free-text labels (like Jira status
names / Asana section names), read straight off ``task["status"]["status"]``.
Priority is a native field but ClickUp models it as an integer 1-4 with no
"none" value (``task["priority"]["priority"]`` carries the string label); the
default table below picks a reasonable label per :class:`ForgePriority`,
resolved case-insensitively like every other adapter.
"""

from __future__ import annotations

# forge StatusCategory -> ClickUp status label (OUT).
STATUS_OUT: dict[str, str] = {
    "backlog": "backlog",
    "unstarted": "to do",
    "started": "in progress",
    "completed": "complete",
    "canceled": "closed",
}

# ClickUp status label (case-insensitive) -> forge StatusCategory (IN).
STATUS_IN: dict[str, str] = {
    "backlog": "backlog",
    "to do": "unstarted",
    "open": "unstarted",
    "in progress": "started",
    "review": "started",
    "complete": "completed",
    "done": "completed",
    "closed": "canceled",
}

# forge ForgePriority -> ClickUp priority label (OUT).
PRIORITY_OUT: dict[str, str] = {
    "none": "normal",
    "low": "low",
    "medium": "normal",
    "high": "high",
    "urgent": "urgent",
}

# ClickUp priority label -> forge ForgePriority (IN).
PRIORITY_IN: dict[str, str] = {
    "low": "low",
    "normal": "medium",
    "high": "high",
    "urgent": "urgent",
}

# ClickUp's write API takes the priority as an integer 1 (urgent) - 4 (low);
# the read API returns the string label. This table is the write-side-only
# label -> id lookup (never surfaced through the pure map_priority Protocol
# method, which always deals in the human label).
PRIORITY_LABEL_TO_ID: dict[str, int] = {"urgent": 1, "high": 2, "normal": 3, "low": 4}


__all__ = [
    "PRIORITY_IN",
    "PRIORITY_LABEL_TO_ID",
    "PRIORITY_OUT",
    "STATUS_IN",
    "STATUS_OUT",
]
