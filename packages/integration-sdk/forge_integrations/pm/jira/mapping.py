"""Jira status/priority default maps + best-effort markdown<->ADF conversion.

Status mapping is by Jira ``statusCategory.key`` (``new`` / ``indeterminate`` /
``done``) because Jira status names are project-defined; the adapter resolves the
concrete transition whose target category matches at write time.
"""

from __future__ import annotations

from typing import Any

# forge StatusCategory -> Jira statusCategory.key (OUT)
STATUS_OUT: dict[str, str] = {
    "backlog": "new",
    "unstarted": "new",
    "started": "indeterminate",
    "completed": "done",
    "canceled": "done",
}

# Jira statusCategory.key -> forge StatusCategory (IN)
STATUS_IN: dict[str, str] = {
    "new": "backlog",
    "indeterminate": "started",
    "done": "completed",
}

# forge ForgePriority -> Jira priority name (OUT)
PRIORITY_OUT: dict[str, str] = {
    "none": "Medium",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "urgent": "Highest",
}

# Jira priority name -> forge ForgePriority (IN)
PRIORITY_IN: dict[str, str] = {
    "lowest": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "highest": "urgent",
}


def markdown_to_adf(markdown: str | None) -> dict[str, Any]:
    """Best-effort markdown -> Atlassian Document Format (paragraphs only).

    Unconvertible content is preserved verbatim as paragraph text; sync is never
    blocked on formatting fidelity (rich nodes are out of scope, see §12).
    """
    text = markdown or ""
    paragraphs = text.split("\n\n") if text else []
    content: list[dict[str, Any]] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        content.append(
            {"type": "paragraph", "content": [{"type": "text", "text": para}]}
        )
    if not content:
        content.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": content}


def adf_to_markdown(adf: Any) -> str:
    """Best-effort ADF -> markdown: concatenate text nodes, paragraph-separated."""
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""
    blocks: list[str] = []

    def walk_inline(node: dict[str, Any]) -> str:
        if node.get("type") == "text":
            return str(node.get("text", ""))
        return "".join(walk_inline(c) for c in node.get("content", []) or [])

    for block in adf.get("content", []) or []:
        blocks.append(walk_inline(block))
    return "\n\n".join(b for b in blocks if b)


__all__ = [
    "PRIORITY_IN",
    "PRIORITY_OUT",
    "STATUS_IN",
    "STATUS_OUT",
    "adf_to_markdown",
    "markdown_to_adf",
]
