"""Linear workflow-state-type <-> SCF category (1:1) + priority int<->token.

Linear's workflow-state ``type`` aligns one-to-one with SCF's
``StatusCategory`` so status mapping is identity (overridable per connection).
Linear priority is an int 0..4 (0 = no priority, 1 = urgent ... 4 = low).
"""

from __future__ import annotations

# SCF StatusCategory -> Linear workflow-state type (OUT) — identity.
STATUS_OUT: dict[str, str] = {
    "backlog": "backlog",
    "unstarted": "unstarted",
    "started": "started",
    "completed": "completed",
    "canceled": "canceled",
}

# Linear workflow-state type -> SCF StatusCategory (IN) — identity.
STATUS_IN: dict[str, str] = {v: k for k, v in STATUS_OUT.items()}

# SCF ScfPriority -> Linear priority int (OUT), as a string.
PRIORITY_OUT: dict[str, str] = {
    "none": "0",
    "low": "4",
    "medium": "3",
    "high": "2",
    "urgent": "1",
}

# Linear priority int (str) -> SCF ScfPriority (IN).
PRIORITY_IN: dict[str, str] = {v: k for k, v in PRIORITY_OUT.items()}


def markdown_passthrough(markdown: str | None) -> str:
    """Linear stores markdown natively, so this is an identity passthrough."""
    return markdown or ""


__all__ = [
    "PRIORITY_IN",
    "PRIORITY_OUT",
    "STATUS_IN",
    "STATUS_OUT",
    "markdown_passthrough",
]
