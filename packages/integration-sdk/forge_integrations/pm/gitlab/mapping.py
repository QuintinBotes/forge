"""GitLab issues status/priority default maps.

GitLab issues have no native "status" or "priority" field beyond the binary
open/closed ``state`` — boards model workflow as *labels* ("labels/board-lists
-> status categories" per spec), so both status and priority are modeled as
labels here, matched by name off the issue's ``labels`` array (distinct name
pools so a status label is never mistaken for a priority label, or vice
versa). ``state``/``state_event`` is set as a secondary signal alongside the
status label (closed for ``completed``/``canceled``, reopened otherwise) so a
board that only looks at open/closed still gets a reasonable signal.
"""

from __future__ import annotations

# forge StatusCategory -> GitLab label name (OUT).
STATUS_OUT: dict[str, str] = {
    "backlog": "Backlog",
    "unstarted": "To Do",
    "started": "Doing",
    "completed": "Done",
    "canceled": "Closed",
}

# GitLab label name (case-insensitive) -> forge StatusCategory (IN).
STATUS_IN: dict[str, str] = {
    "backlog": "backlog",
    "to do": "unstarted",
    "todo": "unstarted",
    "doing": "started",
    "in progress": "started",
    "in review": "started",
    "review": "started",
    "done": "completed",
    "closed": "canceled",
    "cancelled": "canceled",
    "canceled": "canceled",
}

# forge ForgePriority -> GitLab label name (OUT).
PRIORITY_OUT: dict[str, str] = {
    "none": "Priority: Medium",
    "low": "Priority: Low",
    "medium": "Priority: Medium",
    "high": "Priority: High",
    "urgent": "Priority: Urgent",
}

# GitLab label name (case-insensitive) -> forge ForgePriority (IN).
PRIORITY_IN: dict[str, str] = {
    "priority: low": "low",
    "priority: medium": "medium",
    "priority: high": "high",
    "priority: urgent": "urgent",
}


__all__ = ["PRIORITY_IN", "PRIORITY_OUT", "STATUS_IN", "STATUS_OUT"]
