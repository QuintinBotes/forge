"""Forge-to-GitHub defaults; AdapterContext maps override these in both directions."""

STATUS_OUT = {
    "backlog": "status:backlog",
    "unstarted": "open",
    "started": "status:in-progress",
    "completed": "closed",
    "canceled": "status:canceled",
}
STATUS_IN = {value: key for key, value in STATUS_OUT.items()}

PRIORITY_OUT = {
    "none": "priority:none",
    "low": "priority:low",
    "medium": "priority:medium",
    "high": "priority:high",
    "urgent": "priority:urgent",
}
PRIORITY_IN = {value: key for key, value in PRIORITY_OUT.items()}
