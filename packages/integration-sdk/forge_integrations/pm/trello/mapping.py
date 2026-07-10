"""Trello status/priority default maps.

Trello has no native "status" or "priority" field: status is the card's list
membership (the columns on a Trello board — "lists -> status categories" per
spec), and priority is conventionally a board label (there is no built-in
priority concept). Both are resolved by name lookup exactly like Asana's
section-membership status and label-based supplementary fields.
"""

from __future__ import annotations

# forge StatusCategory -> Trello list name (OUT) — the canonical name the
# adapter will create/look up on the board's lists.
STATUS_OUT: dict[str, str] = {
    "backlog": "Backlog",
    "unstarted": "To Do",
    "started": "Doing",
    "completed": "Done",
    "canceled": "Cancelled",
}

# Trello list name (case-insensitive) -> forge StatusCategory (IN).
STATUS_IN: dict[str, str] = {
    "backlog": "backlog",
    "to do": "unstarted",
    "todo": "unstarted",
    "not started": "unstarted",
    "doing": "started",
    "in progress": "started",
    "in review": "started",
    "review": "started",
    "done": "completed",
    "complete": "completed",
    "completed": "completed",
    "cancelled": "canceled",
    "canceled": "canceled",
}

# forge ForgePriority -> Trello label name (OUT).
PRIORITY_OUT: dict[str, str] = {
    "none": "Medium",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "urgent": "Urgent",
}

# Trello label name -> forge ForgePriority (IN).
PRIORITY_IN: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "urgent": "urgent",
}


__all__ = ["PRIORITY_IN", "PRIORITY_OUT", "STATUS_IN", "STATUS_OUT"]
